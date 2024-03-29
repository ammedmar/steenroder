import time
from functools import lru_cache
import psutil

import numba as nb
import numpy as np
from numba.cpython.unsafe.tuple import tuple_setitem
from numba.np.unsafe.ndarray import to_fixed_tuple

list_of_int64_typ = nb.types.List(nb.int64)
int64_2d_array_typ = nb.types.Array(nb.int64, 2, "C")

# Determine the number of available physical cores
N_PHYSICAL_CORES = psutil.cpu_count(logical=False)


def sort_filtration_by_dim(filtration, maxdim=None):
    """Organize an input simplex-wise filtration by dimension.

    Parameters
    ----------
    filtration : sequence of list-like of int
        Represents a simplex-wise filtration. Entry ``i`` is a list/tuple/set
        containing the integer indices of the vertices defining the ``i``th
        simplex in the filtration.

    maxdim : int or None, optional, default: None
        Maximum simplex dimension to be included. ``None`` means that all
        simplices are included.

    Returns
    -------
    filtration_by_dim : list of list of ndarray
        For each dimension ``d``, a list of 2 aligned int arrays: the first is
        a 1D array containing the (ordered) positional indices of all
        ``d``-dimensional simplices in `filtration`; the second is a 2D array
        whose ``i``-th row is the (sorted) collection of vertices defining the
        ``i``-th ``d``-dimensional simplex.

    """
    if maxdim is None:
        maxdim = max(map(len, filtration)) - 1

    filtration_by_dim = [[] for _ in range(maxdim + 1)]
    for i, spx in enumerate(filtration):
        spx_tup = tuple(sorted(spx))
        dim = len(spx_tup) - 1
        if dim <= maxdim:
            filtration_by_dim[dim].append([i, spx_tup])

    for dim, filtr in enumerate(filtration_by_dim):
        filtration_by_dim[dim] = [np.asarray(x, dtype=np.int64)
                                  for x in zip(*filtr)]

    return filtration_by_dim


@nb.njit
def _twist_reduction(coboundary, triangular, pivots_lookup):
    """Core of the persistent relative cohomology reduction algorithm using the
    clearing optimization."""
    n = len(coboundary)

    rel_idxs_to_clear = []
    for j in range(n - 1, -1, -1):
        highest_one = coboundary[j][0] if coboundary[j] else -1
        pivot_col = pivots_lookup[highest_one]
        while (highest_one != -1) and (pivot_col != -1):
            coboundary[j] = _symm_diff(coboundary[j][1:],
                                       coboundary[pivot_col][1:])
            triangular[j] = _symm_diff(triangular[j],
                                       triangular[pivot_col])
            highest_one = coboundary[j][0] if coboundary[j] else -1
            pivot_col = pivots_lookup[highest_one]
        if highest_one != -1:
            pivots_lookup[highest_one] = j
            rel_idxs_to_clear.append(highest_one)

    return np.asarray(rel_idxs_to_clear, dtype=np.int64)


@lru_cache
def _reduce_single_dim(dim):
    len_tups_dim = dim + 1
    tuple_typ_dim = nb.types.UniTuple(nb.int64, len_tups_dim)
    len_tups_next_dim = dim + 2

    @nb.njit
    def _inner_reduce_single_dim(idxs_dim, tups_dim, rel_idxs_to_clear,
                                 idxs_next_dim=None, tups_next_dim=None):
        """R = MV"""
        # 1) Construct sp2idx_dim as a dict simplex: relative (i.e.
        # in-dimension) index
        # 2) Initialize type of reduced_dim (needed for type inference)
        # 3) Construct triangular_dim, with entries denoting relative (i.e.
        # in-dimension) indices
        spx2idx_dim = nb.typed.Dict.empty(tuple_typ_dim, nb.int64)
        reduced_dim = nb.typed.List.empty_list(list_of_int64_typ)
        triangular_dim = nb.typed.List.empty_list(list_of_int64_typ)
        for i in range(len(idxs_dim)):
            spx = to_fixed_tuple(tups_dim[i], len_tups_dim)
            spx2idx_dim[spx] = i
            reduced_dim.append([nb.int64(x) for x in range(0)])
            triangular_dim.append([i])

        # Populate reduced_dim as the coboundary matrix and apply clearing
        # WARNING: Column entries denote relative (i.e. in-dimension) indices!
        if idxs_next_dim is not None:
            for j in range(len(idxs_next_dim)):
                spx = to_fixed_tuple(tups_next_dim[j], len_tups_next_dim)
                for face in _drop_elements(spx):
                    reduced_dim[spx2idx_dim[face]].append(j)

            for rel_idx in rel_idxs_to_clear:
                reduced_dim[rel_idx] = [nb.int64(x) for x in range(0)]

            pivots_lookup = np.full(len(idxs_next_dim), -1, dtype=np.int64)

            rel_idxs_to_clear = _twist_reduction(reduced_dim, triangular_dim,
                                                 pivots_lookup)
        else:
            pivots_lookup = np.empty(0, dtype=np.int64)

        return (spx2idx_dim, reduced_dim, triangular_dim,
                rel_idxs_to_clear, pivots_lookup)

    return _inner_reduce_single_dim


@nb.njit
def _fix_triangular_after_clearing(triangular, reduced_prev_dim,
                                   rel_idxs_to_clear, pivots_lookup_prev_dim):
    """Massage the V matrix to maintain the R = DV decomposition after clearing,
    as described in https://arxiv.org/abs/1908.02518, Sec. 3.2."""
    for rel_idx in rel_idxs_to_clear:
        triangular[rel_idx] = reduced_prev_dim[pivots_lookup_prev_dim[rel_idx]]


def get_reduced_triangular(filtration_by_dim):
    """Find a full-rank upper-triangular matrix V such that R = DV is reduced,
    where D is the anti-transpose of the filtration boundary matrix. Return both
    R and V.

    Parameters
    ----------
    filtration_by_dim : list of list of ndarray
        For each dimension ``d``, a list of 2 aligned int arrays: the first is
        a 1D array containing the (ordered) positional indices of all
        ``d``-dimensional simplices in `filtration`; the second is a 2D array
        whose ``i``-th row is the (sorted) collection of vertices defining the
        ``i``-th ``d``-dimensional simplex.

    Returns
    -------
    spx2idx : tuple of ``numba.typed.Dict``
        One dictionary per simplex dimension. The dimension-``d`` dictionary has
        the filtration ``d``-simplices (tuples of ints) as keys; the
        corresponding values are the positional indices of those simplices
        relative to the ``d``-dimensional portion of the filtration.

    idxs : tuple of ndarray
        For each dimension ``d``, this is the same as
        ``filtration_by_dim[d][0]`` and is returned for convenience.

    reduced : tuple of ``numba.typed.List``
        One list of int per simplex dimension. ``reduced[d]`` is the
        ``d``-dimensional part of the "R" matrix in R = DV. In the computation,
        ``reduced[d][i]`` is initialized as the coboundary of the ``i``th input
        simplex in ``filtration_by_dim[d][1]``, i.e. as the sorted list of
        positional indices (relative to the ``(d+1)``-dimensional portion of the
        filtration) of that simplex's cofacets.

    triangular : tuple of ``numba.typed.List``
        On list of int per simplex dimension. ``triangular[d]`` is the
        ``d``-dimensional part of the "V" matrix in R = DV. In the computation,
        ``triangular[d][i]`` is initialized as the singleton list ``[i]``.

    """
    maxdim = len(filtration_by_dim) - 1
    spx2idx_idxs_reduced_triangular = []
    # Initialize relative (i.e. in-dimension) indices to clear, as an empty
    # int array in dim 0
    rel_idxs_to_clear = np.empty(0, dtype=np.int64)
    reduced_prev_dim = nb.typed.List.empty_list(list_of_int64_typ)
    pivots_lookup_prev_dim = np.empty(0, dtype=np.int64)
    for dim in range(maxdim):
        reduction_dim = _reduce_single_dim(dim)
        idxs_dim, tups_dim = filtration_by_dim[dim]
        idxs_next_dim, tups_next_dim = filtration_by_dim[dim + 1]
        (spx2idx_dim, reduced_dim, triangular_dim,
         rel_idxs_to_clear_next_dim, pivots_lookup) = \
            reduction_dim(idxs_dim,
                          tups_dim,
                          rel_idxs_to_clear,
                          idxs_next_dim=idxs_next_dim,
                          tups_next_dim=tups_next_dim)
        _fix_triangular_after_clearing(triangular_dim,
                                       reduced_prev_dim,
                                       rel_idxs_to_clear,
                                       pivots_lookup_prev_dim)
        spx2idx_idxs_reduced_triangular.append((spx2idx_dim,
                                                idxs_dim,
                                                reduced_dim,
                                                triangular_dim))
        rel_idxs_to_clear = rel_idxs_to_clear_next_dim
        reduced_prev_dim = reduced_dim
        pivots_lookup_prev_dim = pivots_lookup

    reduction_dim = _reduce_single_dim(maxdim)
    idxs_dim, tups_dim = filtration_by_dim[maxdim]
    spx2idx_dim, reduced_dim, triangular_dim, _, _ = \
        reduction_dim(idxs_dim, tups_dim, rel_idxs_to_clear)
    _fix_triangular_after_clearing(triangular_dim,
                                   reduced_prev_dim,
                                   rel_idxs_to_clear,
                                   pivots_lookup_prev_dim)
    spx2idx_idxs_reduced_triangular.append((spx2idx_dim,
                                            idxs_dim,
                                            reduced_dim,
                                            triangular_dim))

    return tuple(zip(*spx2idx_idxs_reduced_triangular))


@nb.njit
def get_barcode_and_coho_reps(idxs, reduced, triangular,
                              filtration_values=None):
    """Extract the ordinary persistent relative cohomology barcode as well as
    one persistent relative cohomology representative per bar.

    Parameters
    ----------
    idxs : tuple of ndarray
        For each dimension ``d``, a 1D int array containing the (ordered)
        positional indices of all ``d``-dimensional simplices in the filtration.

    reduced : tuple of ``numba.typed.List``
        One list of int per simplex dimension, representing the
        ``d``-dimensional part of the "R" matrix in R = DV. In the same format
        as returned by `get_reduced_triangular`.

    triangular : tuple of ``numba.typed.List``
        One list of int per simplex dimension, representing the
        ``d``-dimensional part of the "V" matrix in R = DV. In the same format
        as returned by `get_reduced_triangular`.

    filtration_values : ndarray or None, optional, default: None
        Optionally, a single 1D array of filtration values for each simplex in
        the filtration and in all dimensions contained in `idxs`, `reduced` and
        `triangular`. Bars with equal birth and death filtration values are
        discarded.

    Returns
    -------
    barcode : list of ndarray
        For each dimension ``d``, a 2D int array of shape ``(n_bars, 2)``
        containing the birth (entry 1) and death (entry 0) indices of persistent
        relative homology classes in degree ``d``. See `filtration_values`.
        Essential bars are represented by pairs with death equal to ``-1``. Bars
        are sorted in order of decreasing birth indices.

    coho_reps : list of ``numba.typed.List``
        For each dimension ``d``, a list of representatives of persistent
        relative cohomology classes in degree ``d``. Each such representative is
        represented as a list of positional indices relative to the
        ``d``-dimensional portion of the filtration. ``coho_reps[d][j]``
        corresponds to ``barcode[d][j]``.

    """
    barcode = []
    coho_reps = []

    if filtration_values is None:
        pairs_0 = []
        coho_reps_0 = []
        for i in range(len(idxs[0])):
            if not reduced[0][i]:
                pairs_0.append([-1, idxs[0][i]])
                coho_reps_0.append(triangular[0][i])
        pairs_0 = np.asarray(pairs_0)
        lexsrt = _lexsort_barcode(pairs_0)
        barcode.append(pairs_0[lexsrt])
        coho_reps.append(nb.typed.List([coho_reps_0[k] for k in lexsrt]))

        for dim in range(1, len(idxs)):
            all_birth_indices = set()
            pairs_dim = []
            coho_reps_dim = []
            for i in range(len(idxs[dim - 1])):
                if reduced[dim - 1][i]:
                    b = idxs[dim][reduced[dim - 1][i][0]]
                    d = idxs[dim - 1][i]
                    pairs_dim.append([d, b])
                    coho_reps_dim.append(reduced[dim - 1][i])
                    all_birth_indices.add(b)

            for i in range(len(idxs[dim])):
                if idxs[dim][i] not in all_birth_indices:
                    if not reduced[dim][i]:
                        pairs_dim.append([-1, idxs[dim][i]])
                        coho_reps_dim.append(triangular[dim][i])

            if not len(pairs_dim):
                pairs_dim = np.empty((0, 2), dtype=np.int64)
            else:
                pairs_dim = np.asarray(pairs_dim)
            lexsrt = _lexsort_barcode(pairs_dim)
            barcode.append(pairs_dim[lexsrt])
            coho_reps.append(nb.typed.List([coho_reps_dim[k] for k in lexsrt]))

    else:
        pairs_0 = []
        coho_reps_0 = []
        for i in range(len(idxs[0])):
            if not reduced[0][i]:
                pairs_0.append([-1, idxs[0][i]])
                coho_reps_0.append(triangular[0][i])
        pairs_0 = np.asarray(pairs_0)
        lexsrt = _lexsort_barcode(pairs_0)
        barcode.append(pairs_0[lexsrt])
        coho_reps.append(nb.typed.List([coho_reps_0[k] for k in lexsrt]))

        for dim in range(1, len(idxs)):
            all_birth_indices = set()
            pairs_dim = []
            coho_reps_dim = []
            for i in range(len(idxs[dim - 1])):
                if reduced[dim - 1][i]:
                    b = idxs[dim][reduced[dim - 1][i][0]]
                    d = idxs[dim - 1][i]
                    if filtration_values[b] != filtration_values[d]:
                        pairs_dim.append([d, b])
                        coho_reps_dim.append(reduced[dim - 1][i])
                    all_birth_indices.add(b)

            for i in range(len(idxs[dim])):
                if idxs[dim][i] not in all_birth_indices:
                    if not reduced[dim][i]:
                        pairs_dim.append([-1, idxs[dim][i]])
                        coho_reps_dim.append(triangular[dim][i])

            if not len(pairs_dim):
                pairs_dim = np.empty((0, 2), dtype=np.int64)
            else:
                pairs_dim = np.asarray(pairs_dim)
            lexsrt = _lexsort_barcode(pairs_dim)
            barcode.append(pairs_dim[lexsrt])
            coho_reps.append(nb.typed.List([coho_reps_dim[k] for k in lexsrt]))

    return barcode, coho_reps


@nb.njit
def _initialize_steenrod_matrix(num_dimensions):
    return [nb.typed.List.empty_list(list_of_int64_typ)
            for _ in range(num_dimensions)]


@lru_cache
def _populate_steenrod_matrix_single_dim(dim_plus_k):
    length = dim_plus_k + 1

    @nb.njit(parallel=True)
    def _inner(coho_reps_dim, tups_dim, spx2idx_dim_plus_k, n_jobs=-1):
        steenrod_matrix_dim_plus_k = \
            nb.typed.List([[nb.int64(0) for _ in range(0)]
                           for _ in coho_reps_dim])
        
        if n_jobs == -1:
            n_jobs = N_PHYSICAL_CORES

        for job_idx in nb.prange(n_jobs):
            for coho_reps_dim_idx in range(job_idx, len(coho_reps_dim), n_jobs):
                rep = coho_reps_dim[coho_reps_dim_idx]
                cocycle = tups_dim[np.asarray(rep)]

                # STSQ
                cochain = set(
                    [to_fixed_tuple(np.empty(length, dtype=np.int64), length)
                     for _ in range(0)]
                    )
                for i in range(len(cocycle)):
                    for j in range(i + 1, len(cocycle)):
                        a, b = set(cocycle[i]), set(cocycle[j])
                        u = a.union(b)
                        if len(u) == length:
                            u_tuple = to_fixed_tuple(np.asarray(sorted(u)),
                                                     length)
                            if u_tuple in spx2idx_dim_plus_k:
                                a_bar, b_bar = a.difference(b), b.difference(a)
                                u_bar = sorted(a_bar.union(b_bar))
                                index = {}
                                for v in a_bar.union(b_bar):
                                    pos = u_tuple.index(v)
                                    pos_bar = u_bar.index(v)
                                    index[v] = (pos + pos_bar) % 2
                                index_a = set()
                                index_b = set()
                                for v in a_bar:
                                    index_a.add(index[v])
                                for w in b_bar:
                                    index_b.add(index[w])
                                if (index_a == set([0])
                                    and index_b == set([1])) \
                                        or (index_a == set([1])
                                            and index_b == set([0])):
                                    cochain ^= {u_tuple}

                steenrod_matrix_dim_plus_k[coho_reps_dim_idx] = \
                    sorted([spx2idx_dim_plus_k[spx] for spx in cochain])

        return steenrod_matrix_dim_plus_k

    return _inner


def get_steenrod_matrix(k, coho_reps, filtration_by_dim, spx2idx, n_jobs=-1):
    """Compute the Steenrod matrices in each dimension.

    Parameters
    ----------
    k : int
        Positive integer defining the cohomology operation Sq^k to be performed.

    coho_reps : list of ``numba.typed.List``
        For each dimension ``d``, a list of representatives of persistent
        relative cohomology classes in degree ``d``. In the same format as
        returned by `get_barcode_and_coho_reps`.

    filtration_by_dim : list of list of ndarray
        For each dimension ``d``, a list of 2 aligned int arrays: the first is
        a 1D array containing the (ordered) positional indices of all
        ``d``-dimensional simplices in `filtration`; the second is a 2D array
        whose ``i``-th row is the (sorted) collection of vertices defining the
        ``i``-th ``d``-dimensional simplex.

    spx2idx : tuple of ``numba.typed.Dict``
        One dictionary per simplex dimension. The dimension-``d`` dictionary has
        the filtration ``d``-simplices (tuples of ints) as keys; the
        corresponding values are the positional indices of those simplices
        relative to the ``d``-dimensional portion of the filtration.

    n_jobs : int, optional, default: ``-1``
        [Experimental] Controls the number of threads to be used during parallel
        computation of the Steenrod squares. ``-1`` means using all available
        physical cores.

    Returns
    -------
    steenrod_matrix : list of ``numba.typed.List``
        One list per simplex dimension. ``steenrod_matrix[d][j]`` is the result
        of computing the Steenrod square of ``coho_reps[d][j]``.

    """
    steenrod_matrix = _initialize_steenrod_matrix(k)

    for dim, coho_reps_dim in enumerate(coho_reps[:-k]):
        dim_plus_k = dim + k
        tups_dim = filtration_by_dim[dim][1]
        spx2idx_dim_plus_k = spx2idx[dim + k]
        populate_steenrod_matrix_single_dim = \
            _populate_steenrod_matrix_single_dim(dim_plus_k)
        steenrod_matrix_dim_plus_k = populate_steenrod_matrix_single_dim(
            coho_reps_dim, tups_dim, spx2idx_dim_plus_k, n_jobs=n_jobs
            )
        steenrod_matrix.append(steenrod_matrix_dim_plus_k)
        
    return steenrod_matrix


@nb.njit
def _steenrod_barcode_single_dim(steenrod_matrix_dim, n_idxs_dim, idxs_prev_dim,
                                 reduced_prev_dim, births_dim):
    # Construct augmented matrix
    augmented = []
    for i in range(len(reduced_prev_dim)):
        augmented.append([nb.int64(x) for x in reduced_prev_dim[i]])
    for i in range(len(steenrod_matrix_dim)):
        augmented.append([nb.int64(x) for x in steenrod_matrix_dim[i]])

    pivots_lookup = np.full(n_idxs_dim, -1, dtype=np.int64)
    alive = np.ones(len(births_dim), dtype=np.bool_)
    n = len(idxs_prev_dim)
    st_barcode_dim = []

    j = 0
    for i, idx in enumerate(idxs_prev_dim[::-1]):
        if augmented[n - 1 - i]:
            pivots_lookup[augmented[n - 1 - i][0]] = n - 1 - i
        if births_dim[j] == idx:
            j += 1

        pivot_column_idxs_from_steenrod = []
        for ii in range(n, n + j):
            highest_one = augmented[ii][0] if augmented[ii] else -1
            pivot_col = pivots_lookup[highest_one]
            while (highest_one != -1) and (pivot_col != -1):
                augmented[ii] = _symm_diff(augmented[ii][1:],
                                           augmented[pivot_col][1:])
                highest_one = augmented[ii][0] if augmented[ii] else -1
                pivot_col = pivots_lookup[highest_one]
            if highest_one != -1:
                pivots_lookup[highest_one] = ii
                # Record pivot indices coming from Steenrod part of augmented
                pivot_column_idxs_from_steenrod.append(highest_one)
            elif alive[ii - n]:
                alive[ii - n] = False
                if idx < births_dim[ii - n]:
                    st_barcode_dim.append([idx, births_dim[ii - n]])

        # Reset pivots_lookup for next iteration
        for col_idx in pivot_column_idxs_from_steenrod:
            pivots_lookup[col_idx] = -1

    for i in range(len(alive)):
        if alive[i]:
            st_barcode_dim.append([-1, births_dim[i]])

    return st_barcode_dim


def get_steenrod_barcode(k, steenrod_matrix, idxs, reduced, barcode,
                         filtration_values=None):
    """Compute the (relative) Steenrod barcodes.

    Parameters
    ----------
    k : int
        Positive integer defining the cohomology operation Sq^k to be performed.

    steenrod_matrix : list of ``numba.typed.List``
        One list per simplex dimension. ``steenrod_matrix[d][j]`` is the result
        of computing the Steenrod square of the ``j``th latest (by birth)
        persistent relative cohomology representative in degree ``d``` (and this
        representative must represent bar ``barcode[d][j]``). See
        `get_steenrod_matrix`.

    idxs : tuple of ndarray
        For each dimension ``d``, a 1D int array containing the (ordered)
        positional indices of all ``d``-dimensional simplices in the filtration.

    reduced : tuple of ``numba.typed.List``
        One list of int per simplex dimension, representing the
        ``d``-dimensional part of the "R" matrix in R = DV. In the same format
        as returned by `get_reduced_triangular`.

    barcode : list of ndarray
        For each dimension ``d``, a 2D int array of shape ``(n_bars, 2)``
        containing the birth (entry 1) and death (entry 0) indices of persistent
        relative homology classes in degree ``d``. Essential bars must be
        represented by pairs with death equal to ``-1``. Bars must be sorted in
        order of decreasing birth indices.

    filtration_values : ndarray or None, optional, default: None
        Optionally, a single 1D array of filtration values for each simplex in
        the filtration and in all dimensions contained in `idxs` and `reduced`.
        Steenrod bars with equal birth and death filtration values are
        discarded.

    Returns
    -------
    st_barcode : list of ndarray
        The (relative) Sq^k-barcode. For each dimension ``d``, a 2D int array
        of shape ``(n_bars, 2)`` containing the birth (entry 1) and death (entry
        0) indices of Steenrod bars. Essential Steenrod bars are represented by
        pairs with death equal to ``-1``.

    """
    def nontrivial_bars(barcode_dim):
        infinite_bars = barcode_dim[:, 0] == -1
        return np.logical_or(
            infinite_bars,
            np.logical_and(np.logical_not(infinite_bars),
                           (filtration_values[barcode_dim[:, 0]] !=
                            filtration_values[barcode_dim[:, 1]]))
            )

    st_barcode = [np.empty((0, 2), dtype=np.int64) for _ in range(k)]
    for dim in range(k, len(steenrod_matrix)):
        births_dim = barcode[dim - k][:, 1]
        idxs_dim = idxs[dim]
        idxs_prev_dim = idxs[dim - 1]
        reduced_prev_dim = reduced[dim - 1]
        st_barcode_dim = _steenrod_barcode_single_dim(steenrod_matrix[dim],
                                                      len(idxs_dim),
                                                      idxs_prev_dim,
                                                      reduced_prev_dim,
                                                      births_dim)
        # NB: Conversion to array must happen outside jitted code due to
        # https://github.com/numba/numba/issues/3579
        st_barcode_dim = \
            np.asarray(st_barcode_dim, dtype=np.int64).reshape((-1, 2))
        if filtration_values is not None:
            nontrivial_mask = nontrivial_bars(st_barcode_dim)
            st_barcode_dim = st_barcode_dim[nontrivial_mask]
        st_barcode.append(st_barcode_dim)

    return st_barcode


def barcodes(
        k, filtration, absolute=False, filtration_values=None,
        return_filtration_values=False, maxdim=None, verbose=False,
        n_jobs=1
        ):
    """Given a filtration, compute ordinary persistent (relative or absolute)
    (co)homology barcodes and relative Steenrod barcodes.

    Parameters
    ----------
    k : int
        Positive integer defining the cohomology operation Sq^k to be performed.

    filtration : sequence of list-like of int
        Represents a simplex-wise filtration. Entry ``i`` is a list/tuple/set
        containing the integer indices of the vertices defining the ``i``th
        simplex in the filtration.

    absolute : bool, optional, default: ``False``
        If ``True``, return the ordinary persistent absolute homology barcode,
        and move inessential relative Steenrod bars to one degree lower while
        keeping essential bars in their degree. If ``False``, return the
        ordinary persistent relative cohomology barcode and relative Steenrod
        barcode.

    filtration_values : ndarray or None, optional, default: None
        Optionally, a single 1D array of filtration values for each simplex in
        the filtration. Ordinary and Steenrod bars with equal birth and death
        filtration values are discarded by the computation.

    return_filtration_values : bool, optional, default: ``False``
        If ``True``, birth and deaths will be expressed as filtration values
        instead of filtration indices. Ignored if `filtration_values` is
        ``None``.

    maxdim : int or None, optional, default: None
        Maximum simplex dimension to be included. ``None`` means that all
        simplices are included.

    verbose : bool, optional, default: ``False``
        Whether to print timings for the intermediate steps in the computation.

    n_jobs : int, optional, default: ``1``
        [Experimental] Controls the number of threads to be used during parallel
        computation of the Steenrod squares. ``-1`` means using all available
        physical cores.

    Returns
    -------
    barcode : list of ndarray
        For each dimension ``d``, a 2D int array of shape ``(n_bars, 2)``
        containing the births and deaths of persistent relative homology classes
        in degree ``d``. If `absolute` is ``False``, the birth of a bar is in
        entry 1 and the death in entry 0; otherwise, the positions are reversed.
        Births and death are expressed either as global filtration indices or as
        filtration values depending on `filtration_values` and
        `return_filtration_values`. If they are expressed as indices, essential
        bars have death equal to ``-1``; otherwise, essential bars have death
        equal to ``numpy.inf``.

    st_barcode : list of ndarray
        The (relative) Sq^k-barcode. For each dimension ``d``, a 2D int array
        of shape ``(n_bars, 2)`` containing the birth (entry 1) and death (entry
        0) indices of Steenrod bars. The same conventions as for `barcode` are
        used for birth and death values.

    """
    if verbose:
        tic = time.time()
    filtration_by_dim = sort_filtration_by_dim(filtration, maxdim=maxdim)
    spx2idx, idxs, reduced, triangular = \
        get_reduced_triangular(filtration_by_dim)
    barcode, coho_reps = \
        get_barcode_and_coho_reps(idxs, reduced, triangular,
                                  filtration_values=filtration_values)
    if verbose:
        toc = time.time()
        print(f"Usual barcode computed, time taken: {toc - tic}")
        tic = time.time()
    steenrod_matrix = get_steenrod_matrix(k, coho_reps, filtration_by_dim,
                                          spx2idx, n_jobs=n_jobs)
    if verbose:
        toc = time.time()
        print(f"Steenrod matrix computed, time taken: {toc - tic}")
        tic = time.time()
    st_barcode = get_steenrod_barcode(k, steenrod_matrix, idxs, reduced,
                                      barcode,
                                      filtration_values=filtration_values)
    if verbose:
        toc = time.time()
        print(f"Steenrod barcode computed, time taken: {toc - tic}")

    if absolute:
        barcode = _to_absolute_barcode(
            barcode, filtration_values=filtration_values,
            return_filtration_values=return_filtration_values
            )
        st_barcode = _to_absolute_barcode(
            st_barcode, filtration_values=filtration_values,
            return_filtration_values=return_filtration_values
            )

        return barcode, st_barcode

    elif return_filtration_values and (filtration_values is not None):
        barcode = _to_values_barcode(barcode, filtration_values)
        st_barcode = _to_values_barcode(st_barcode, filtration_values)

    return barcode, st_barcode


def _to_absolute_barcode(rel_barcode, filtration_values=None,
                         return_filtration_values=True):
    abs_barcode = []

    if (not return_filtration_values) or (filtration_values is None):
        dtype = np.int64
        for dim, rel_barcode_dim in enumerate(rel_barcode):
            abs_barcode_dim = []
            for pair in rel_barcode_dim:
                if pair[0] == -1:
                    abs_barcode_dim.append((pair[1], -1))
                else:
                    abs_barcode[dim - 1].append((pair[0], pair[1]))
            abs_barcode.append(abs_barcode_dim)
    else:
        dtype = filtration_values.dtype
        for dim, rel_barcode_dim in enumerate(rel_barcode):
            abs_barcode_dim = []
            for pair in rel_barcode_dim:
                if pair[0] == -1:
                    abs_barcode_dim.append(
                        (filtration_values[pair[1]], np.inf)
                        )
                else:
                    abs_barcode[dim - 1].append(
                        (filtration_values[pair[0]], filtration_values[pair[1]])
                        )
            abs_barcode.append(abs_barcode_dim)

    return [np.array(abs_barcode_dim, dtype=dtype).reshape(-1, 2)
            for abs_barcode_dim in abs_barcode]


def _to_values_barcode(barcode, filtration_values):
    values_barcode = []
    for dim, barcode_dim in enumerate(barcode):
        values_barcode_dim = []
        for pair in barcode_dim:
            if pair[0] == -1:
                values_barcode_dim.append(
                    (-np.inf, filtration_values[pair[1]])
                )
            else:
                values_barcode_dim.append(
                    (filtration_values[pair[0]], filtration_values[pair[1]])
                )
        values_barcode.append(
            np.array(values_barcode_dim,
                     dtype=filtration_values.dtype).reshape(-1, 2)
            )

    return values_barcode


def check_agreement_with_gudhi(gudhi_barcode, barcode):
    max_dimension_gudhi = max([pers_info[0] for pers_info in gudhi_barcode])
    assert max_dimension_gudhi <= len(barcode) - 1

    for dim, barcode_dim in enumerate(barcode):
        gudhi_barcode_dim = sorted([
            pers_info[1] for pers_info in gudhi_barcode if pers_info[0] == dim
            ])
        assert gudhi_barcode_dim == sorted(barcode_dim), \
            f"Disagreement in degree {dim}"


@nb.njit
def _symm_diff(x, y):
    n = len(x)
    m = len(y)
    result = []
    i = 0
    j = 0
    while (i < n) and (j < m):
        if x[i] < y[j]:
            result.append(x[i])
            i += 1
        elif y[j] < x[i]:
            result.append(y[j])
            j += 1
        else:
            i += 1
            j += 1

    while i < n:
        result.append(x[i])
        i += 1

    while j < m:
        result.append(y[j])
        j += 1

    return result


@nb.njit
def _lexsort_barcode(arr):
    return np.argsort(arr[:, 1])[::-1]


@nb.njit
def _drop_elements(tup: tuple):
    for x in range(len(tup)):
        empty = tup[:-1]  # Not empty, but the right size and will be mutated
        idx = 0
        for i in range(len(tup)):
            if i != x:
                empty = tuple_setitem(empty, idx, tup[i])
                idx += 1
        yield empty
