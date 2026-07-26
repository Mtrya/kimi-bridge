"""Shared scheduling rules for limited editable messages."""

from __future__ import annotations


FAST_EDIT_COUNT = 15
EDIT_INTERVAL_RATIO = 2


def adaptive_interval_units(edit_limit: int) -> int:
    """Return the geometric units assigned after the fast-edit phase."""

    adaptive_edit_count = max(0, edit_limit - FAST_EDIT_COUNT)
    return (EDIT_INTERVAL_RATIO**adaptive_edit_count - 1) // (EDIT_INTERVAL_RATIO - 1)


def minimum_output_seconds(edit_throttle_seconds: float, edit_limit: int) -> float:
    """Return the shortest feasible output window for one edit budget."""

    fast_edit_count = min(edit_limit, FAST_EDIT_COUNT)
    return edit_throttle_seconds * (
        fast_edit_count + adaptive_interval_units(edit_limit)
    )


def edit_interval(
    *,
    edit_throttle_seconds: float,
    max_output_seconds: float,
    edit_number: int,
    edit_limit: int,
) -> float:
    """Return the interval before one edit in a feasible budget."""

    if edit_number <= FAST_EDIT_COUNT or edit_limit <= FAST_EDIT_COUNT:
        return edit_throttle_seconds
    unit = (
        max_output_seconds - FAST_EDIT_COUNT * edit_throttle_seconds
    ) / adaptive_interval_units(edit_limit)
    return unit * EDIT_INTERVAL_RATIO ** (edit_number - FAST_EDIT_COUNT - 1)
