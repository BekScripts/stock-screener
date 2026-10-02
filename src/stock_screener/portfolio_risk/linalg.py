"""Positive-semidefinite validation, in pure Python.

P1.1 validated that a covariance matrix was symmetric, finite and had a
non-negative diagonal, and said plainly that this does not prove positive
semidefiniteness. It does not: a symmetric matrix can satisfy all three and still
have a negative eigenvalue, which means some portfolio has negative variance.
P1.1's `variance_of` then clamped that to zero and reported a volatility of zero —
the single worst way to be wrong about risk, because it reports perfect safety.

P1.2 closes it. The test is an `LDLᵀ` factorisation with explicit zero-pivot
handling, which is the textbook semidefiniteness check and needs no numerical
dependency — keeping this package's imports to the standard library plus
`structlog` is a boundary worth more than the convenience of an eigenvalue call.

The factorisation reads `A = L·D·Lᵀ` with `L` unit lower-triangular. Three cases
per pivot:

- `d > tolerance` — ordinary, carry on.
- `d < -tolerance` — some vector gives negative variance. **Indefinite.**
- `|d| <= tolerance` — a genuine zero pivot, which a singular-but-valid matrix
  has. The matrix stays semidefinite only if the rest of that column vanishes
  too; a zero pivot beside a non-zero off-diagonal entry is a `[[0, b], [b, c]]`
  minor with determinant `-b²`, which is indefinite.

Singular is not invalid. Perfectly correlated holdings, a duplicated factor and a
zero-volatility cash-like instrument all produce a legitimately singular matrix,
and rejecting those would refuse correct inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["LDL_METHOD_ID", "PsdVerdict", "is_positive_semidefinite"]

#: Which validation produced a verdict, recorded on the snapshot.
LDL_METHOD_ID = "SYMMETRIC_LDL_WITH_ZERO_PIVOT_HANDLING_V1"


class PsdVerdict:
    """The outcome of a semidefiniteness test.

    Attributes:
        definite: Whether every pivot was strictly positive.
        semidefinite: Whether the matrix is PSD within tolerance.
        rank: How many strictly positive pivots there were.
        minimum_pivot: The smallest pivot seen, which locates the failure.
        failure: Why it is not semidefinite, or None.
        failure_index: Which row exposed the failure, or None.
    """

    def __init__(
        self,
        *,
        semidefinite: bool,
        definite: bool,
        rank: int,
        minimum_pivot: float,
        failure: str | None = None,
        failure_index: int | None = None,
    ) -> None:
        """Record a verdict.

        Args:
            semidefinite: Whether the matrix is PSD within tolerance.
            definite: Whether every pivot was strictly positive.
            rank: The number of strictly positive pivots.
            minimum_pivot: The smallest pivot encountered.
            failure: Why it failed, if it did.
            failure_index: Where it failed, if it did.
        """
        self.semidefinite = semidefinite
        self.definite = definite
        self.rank = rank
        self.minimum_pivot = minimum_pivot
        self.failure = failure
        self.failure_index = failure_index

    def as_json(self) -> dict[str, Any]:
        """Return the verdict as a JSON-safe mapping."""
        return {
            "method": LDL_METHOD_ID,
            "semidefinite": self.semidefinite,
            "definite": self.definite,
            "rank": self.rank,
            "minimum_pivot": self.minimum_pivot,
            "failure": self.failure,
            "failure_index": self.failure_index,
            "singular_is_valid": True,
        }


def is_positive_semidefinite(matrix: Sequence[Sequence[float]], *, tolerance: float) -> PsdVerdict:
    """Test a symmetric matrix for positive semidefiniteness.

    Nothing is repaired. A matrix that fails is reported as failing, at the row
    that exposed it, because an indefinite covariance is a fault in whatever
    estimated it and projecting it onto the PSD cone would hide that fault behind
    a number that looks reasonable.

    Args:
        matrix: A square symmetric matrix. Symmetry and finiteness are the
            caller's to establish first; this routine assumes them.
        tolerance: How far a pivot may fall below zero before the matrix is
            indefinite, and how close to zero a pivot must be to count as a
            genuine zero. The same covariance tolerance used elsewhere, never a
            value chosen to make a particular matrix pass.

    Returns:
        The verdict, including the minimum pivot so a near-miss is visible.
    """
    size = len(matrix)
    lower: list[list[float]] = [[0.0] * size for _ in range(size)]
    pivots: list[float] = [0.0] * size
    minimum = float("inf")
    rank = 0

    for column in range(size):
        pivot = matrix[column][column] - sum(
            lower[column][k] * lower[column][k] * pivots[k] for k in range(column)
        )
        minimum = min(minimum, pivot)
        if pivot < -tolerance:
            return PsdVerdict(
                semidefinite=False,
                definite=False,
                rank=rank,
                minimum_pivot=pivot,
                failure="NEGATIVE_PIVOT",
                failure_index=column,
            )
        if abs(pivot) <= tolerance:
            # A genuine zero pivot. Legitimate for a singular PSD matrix, but only
            # if the remainder of the column eliminates to zero as well: a zero on
            # the diagonal beside a non-zero off-diagonal is an indefinite minor.
            pivots[column] = 0.0
            for row in range(column + 1, size):
                residue = matrix[row][column] - sum(
                    lower[row][k] * lower[column][k] * pivots[k] for k in range(column)
                )
                if abs(residue) > tolerance:
                    return PsdVerdict(
                        semidefinite=False,
                        definite=False,
                        rank=rank,
                        minimum_pivot=pivot,
                        failure="ZERO_PIVOT_WITH_NONZERO_COLUMN",
                        failure_index=column,
                    )
                lower[row][column] = 0.0
            continue
        pivots[column] = pivot
        rank += 1
        for row in range(column + 1, size):
            lower[row][column] = (
                matrix[row][column]
                - sum(lower[row][k] * lower[column][k] * pivots[k] for k in range(column))
            ) / pivot

    return PsdVerdict(
        semidefinite=True,
        definite=rank == size,
        rank=rank,
        minimum_pivot=0.0 if minimum == float("inf") else minimum,
    )
