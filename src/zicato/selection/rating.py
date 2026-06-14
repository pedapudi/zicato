"""Bradley--Terry rating from pairwise duel outcomes.

The rating backbone described in ``docs/design/SELECTION-THEORY.md`` §7.1
and ``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md`` §5. Each contestant
``i`` has a latent strength ``theta_i``; the probability that ``i`` beats
``j`` is the logistic ``sigma(theta_i - theta_j)``. The strengths are the
maximum-likelihood fit over all observed (and replicated) duels — a convex
problem with a single global optimum, solved here by a small pure-Python
Newton / iteratively-reweighted step. The fit yields a standard error per
contestant (from the Fisher information), which is the operational payoff:
overlapping strength intervals are the duels worth replicating.

This module is **pure** — no IO, no strategy state, no external numerical
dependency. It takes an opaque sequence of (winner, loser) duel outcomes
and returns ``{generation_id: (theta, se)}``. It is **opt-in**: nothing in
the default selection path imports it unless ``params["rating"]`` selects
it. The gate is never involved; a rating only ever *proposes* an ordering.

Replication is absorbed natively: feed each replicate of a duel as its own
``(winner, loser)`` outcome and the likelihood weights it automatically.
Partial / star schedules (elim, racing) are fine too — not every pair need
be played.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

#: One pairwise outcome: ``(winner_id, loser_id)``. A tie is not a valid
#: Bradley--Terry observation (the continuous loss makes exact ties
#: measure-zero); callers resolve any tie to a definite winner before
#: feeding it here.
DuelOutcome = tuple[str, str]


def _logistic(x: float) -> float:
    """Numerically-stable logistic ``sigma(x) = 1 / (1 + e^-x)``."""
    if x >= 0.0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def fit_bradley_terry(
    matches: Iterable[DuelOutcome],
    *,
    prior: float = 1.0,
    max_iter: int = 100,
    tol: float = 1e-9,
) -> dict[str, tuple[float, float]]:
    """Fit Bradley--Terry strengths from pairwise outcomes.

    Parameters
    ----------
    matches:
        An iterable of ``(winner_id, loser_id)`` outcomes. Each element is
        one Bernoulli win — a replicate of the same pairing is a separate
        element, which is how replication sharpens the fit. Self-matches
        (``winner == loser``) are ignored.
    prior:
        A small L2 (ridge) prior weight on each ``theta`` toward zero. This
        is what keeps the otherwise-translation-invariant likelihood
        identifiable (Bradley--Terry strengths are only defined up to an
        additive constant) AND keeps a contestant with a perfect or empty
        record at a finite strength rather than diverging to ±inf. The
        default ``1.0`` is a gentle regulariser; raise it to shrink harder
        toward the field mean. Must be ``> 0``.
    max_iter, tol:
        Newton iteration controls. The problem is convex so a handful of
        iterations converge; ``tol`` is the max-coordinate update at which
        we stop.

    Returns
    -------
    A mapping ``{generation_id: (theta, se)}``. ``theta`` is the latent
    strength (higher = stronger); ``se`` is its standard error from the
    inverse Fisher information (the ridge prior guarantees the information
    matrix is positive-definite, so the SE is always finite). An empty
    input yields an empty mapping. A contestant who appears only as a
    ``winner`` or only as a ``loser`` still gets a finite strength because
    of the prior.

    Notes
    -----
    The fit is centered so the strengths sum to zero (the natural gauge for
    the translation-invariant model), which makes ``theta`` comparable
    across calls of the same field. The SEs are *unaffected* by the
    centering.
    """
    if prior <= 0.0:
        raise ValueError(f"prior must be positive, got {prior!r}")

    # Tally wins per ordered pair: wins[(a, b)] = times a beat b.
    ids: list[str] = []
    seen: set[str] = set()
    wins: dict[tuple[str, str], int] = {}
    for winner, loser in matches:
        if winner == loser:
            continue
        for gid in (winner, loser):
            if gid not in seen:
                seen.add(gid)
                ids.append(gid)
        key = (winner, loser)
        wins[key] = wins.get(key, 0) + 1

    n = len(ids)
    if n == 0:
        return {}

    index = {gid: i for i, gid in enumerate(ids)}
    # Aggregate per unordered pair: total games and a's wins, so each pair
    # contributes once to the gradient/Hessian.
    # pair_games[(i, j)] with i < j = total duels between them;
    # pair_wins_i = wins by the lower-index contestant i.
    pair_games: dict[tuple[int, int], int] = {}
    pair_wins_low: dict[tuple[int, int], int] = {}
    for (a, b), w in wins.items():
        ia, ib = index[a], index[b]
        lo, hi = (ia, ib) if ia < ib else (ib, ia)
        pair_games[(lo, hi)] = pair_games.get((lo, hi), 0) + w
        if ia < ib:
            pair_wins_low[(lo, hi)] = pair_wins_low.get((lo, hi), 0) + w
        else:
            pair_wins_low.setdefault((lo, hi), 0)

    theta = [0.0] * n

    for _ in range(max_iter):
        grad = [0.0] * n
        # Diagonal + the (small) cross terms of the Hessian. We assemble a
        # dense n x n Hessian — n is single-digit in zicato's regime, so an
        # O(n^3) solve is trivial.
        hess = [[0.0] * n for _ in range(n)]
        # Ridge prior: -prior * theta in the gradient, +prior on the diagonal.
        for i in range(n):
            grad[i] -= prior * theta[i]
            hess[i][i] += prior
        for (i, j), games in pair_games.items():
            wins_i = pair_wins_low.get((i, j), 0)
            p = _logistic(theta[i] - theta[j])  # P(i beats j)
            # Log-likelihood gradient w.r.t. theta_i from this pair:
            #   wins_i - games * p
            g = wins_i - games * p
            grad[i] += g
            grad[j] -= g
            # Fisher information for this pair: games * p * (1 - p).
            info = games * p * (1.0 - p)
            hess[i][i] += info
            hess[j][j] += info
            hess[i][j] -= info
            hess[j][i] -= info
        # Newton step: solve hess * delta = grad, theta += delta.
        delta = _solve(hess, grad)
        max_step = 0.0
        for i in range(n):
            theta[i] += delta[i]
            if abs(delta[i]) > max_step:
                max_step = abs(delta[i])
        if max_step < tol:
            break

    # Standard errors from the inverse of the Fisher information at the fit
    # (the same Hessian, re-evaluated at the converged theta). The ridge
    # prior keeps it positive-definite, so the inverse always exists.
    info_matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        info_matrix[i][i] += prior
    for (i, j), games in pair_games.items():
        p = _logistic(theta[i] - theta[j])
        info = games * p * (1.0 - p)
        info_matrix[i][i] += info
        info_matrix[j][j] += info
        info_matrix[i][j] -= info
        info_matrix[j][i] -= info
    cov = _invert(info_matrix)

    # Center the strengths to the zero-sum gauge for cross-call comparability.
    mean_theta = sum(theta) / n
    out: dict[str, tuple[float, float]] = {}
    for gid in ids:
        i = index[gid]
        var = cov[i][i]
        se = math.sqrt(var) if var > 0.0 else 0.0
        out[gid] = (theta[i] - mean_theta, se)
    return out


def prob_stronger(
    theta_a: float,
    se_a: float,
    theta_b: float,
    se_b: float,
) -> float:
    """``P(theta_a > theta_b)`` under independent normal strength estimates.

    Treats the two fitted strengths as independent normals with the given
    standard errors; the difference is normal with mean ``theta_a -
    theta_b`` and variance ``se_a^2 + se_b^2``. Returns the probability the
    difference is positive. When both SEs are zero the answer is a hard
    ``1.0`` / ``0.0`` / ``0.5`` (degenerate point estimates).

    This is the quantity the opt-in uncertainty pre-gate guard
    (FUNCTIONALITY-RECOMMENDATIONS.md §5) thresholds: promote only if the
    child's strength is above the parent's with enough confidence.
    """
    diff = theta_a - theta_b
    var = se_a * se_a + se_b * se_b
    if var <= 0.0:
        if diff > 0.0:
            return 1.0
        if diff < 0.0:
            return 0.0
        return 0.5
    z = diff / math.sqrt(var)
    # Standard-normal CDF via the error function.
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def theta_rank(rating: Mapping[str, tuple[float, float]]) -> list[str]:
    """Return generation ids best-first by fitted strength (higher theta first).

    Ties on ``theta`` break by id for determinism. This is the drop-in
    replacement for the Copeland / scalar standings sort when the operator
    selects ``rating="bradley_terry"`` — it orders the field by latent
    strength rather than raw win-count or mean scalar.
    """
    return sorted(rating, key=lambda gid: (-rating[gid][0], gid))


# ---------------------------------------------------------------------------
# Tiny dense linear algebra (pure Python; fields are single-digit).
# ---------------------------------------------------------------------------


def _solve(matrix: Sequence[Sequence[float]], rhs: Sequence[float]) -> list[float]:
    """Solve ``matrix @ x = rhs`` by Gaussian elimination with partial pivot.

    The matrix is the (small, symmetric positive-definite) Newton Hessian;
    the ridge prior guarantees it is invertible. Returns the solution
    vector. Used only on single-digit ``n``, so the cubic cost is moot.
    """
    n = len(rhs)
    # Augmented copy.
    aug = [list(matrix[i]) + [rhs[i]] for i in range(n)]
    for col in range(n):
        # Partial pivot.
        pivot = col
        best = abs(aug[col][col])
        for r in range(col + 1, n):
            v = abs(aug[r][col])
            if v > best:
                best = v
                pivot = r
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]
        diag = aug[col][col]
        if diag == 0.0:
            # Singular despite the prior (should not happen) — treat the
            # update as zero for this coordinate rather than dividing by 0.
            continue
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col] / diag
            if factor == 0.0:
                continue
            for c in range(col, n + 1):
                aug[r][c] -= factor * aug[col][c]
    out = [0.0] * n
    for i in range(n):
        diag = aug[i][i]
        out[i] = aug[i][n] / diag if diag != 0.0 else 0.0
    return out


def _invert(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    """Invert a small SPD matrix via Gauss--Jordan on an augmented identity."""
    n = len(matrix)
    aug = [list(matrix[i]) + [1.0 if j == i else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        pivot = col
        best = abs(aug[col][col])
        for r in range(col + 1, n):
            v = abs(aug[r][col])
            if v > best:
                best = v
                pivot = r
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]
        diag = aug[col][col]
        if diag == 0.0:
            continue
        inv_diag = 1.0 / diag
        for c in range(2 * n):
            aug[col][c] *= inv_diag
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor == 0.0:
                continue
            for c in range(2 * n):
                aug[r][c] -= factor * aug[col][c]
    return [[aug[i][n + j] for j in range(n)] for i in range(n)]


__all__ = [
    "DuelOutcome",
    "fit_bradley_terry",
    "prob_stronger",
    "theta_rank",
]
