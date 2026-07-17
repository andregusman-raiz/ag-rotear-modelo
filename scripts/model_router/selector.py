import math
from dataclasses import replace
from typing import Tuple

from .contracts import (
    EliminatedRoute,
    RouteAssessment,
    RouteDecision,
)
from .profiles import Profile


class SelectionError(ValueError):
    pass


_AXES = (
    ("quality_signal", "max"),
    ("expected_cost", "min"),
    ("expected_latency", "min"),
    ("residual_risk", "min"),
)


def _finite_number(value, path: str, minimum=None, maximum=None) -> None:
    if type(value) not in (int, float):
        raise SelectionError("%s must be a number" % path)
    if not math.isfinite(value):
        raise SelectionError("%s must be finite" % path)
    if minimum is not None and value < minimum:
        raise SelectionError("%s must be at least %s" % (path, minimum))
    if maximum is not None and value > maximum:
        raise SelectionError("%s must be at most %s" % (path, maximum))


def _validate_assessment(item: RouteAssessment, path: str) -> None:
    if item.quality_signal is not None:
        _finite_number(item.quality_signal, path + ".quality_signal", 0.0, 1.0)
    if item.expected_cost is not None:
        _finite_number(item.expected_cost, path + ".expected_cost", 0.0)
    if item.expected_latency is not None:
        _finite_number(item.expected_latency, path + ".expected_latency", 0.0)
    if item.residual_risk is not None:
        _finite_number(item.residual_risk, path + ".residual_risk", 0.0, 1.0)
    _finite_number(item.evidence_grade, path + ".evidence_grade", 0.0, 5.0)
    if len(set(item.comparable_groups)) != len(item.comparable_groups):
        raise SelectionError("%s.comparable_groups contains a duplicate" % path)


def _assessment_tuple(value) -> Tuple[RouteAssessment, ...]:
    if type(value) is not tuple:
        raise SelectionError("assessments must be a tuple")
    seen = set()
    copied = tuple(value)
    for index, item in enumerate(copied):
        if type(item) is not RouteAssessment:
            raise SelectionError(
                "assessments[%d] must be RouteAssessment" % index
            )
        _validate_assessment(item, "assessments[%d]" % index)
        if item.route.key in seen:
            raise SelectionError("assessments[%d] is a duplicate route" % index)
        seen.add(item.route.key)
    return tuple(sorted(copied, key=lambda item: item.route.key))


def _eliminated_tuple(value) -> Tuple[EliminatedRoute, ...]:
    if type(value) is not tuple:
        raise SelectionError("eliminated must be a tuple")
    copied = tuple(value)
    seen = set()
    for index, item in enumerate(copied):
        if type(item) is not EliminatedRoute:
            raise SelectionError(
                "eliminated[%d] must be EliminatedRoute" % index
            )
        if item.route.key in seen:
            raise SelectionError("eliminated[%d] is a duplicate route" % index)
        seen.add(item.route.key)
    return copied


def _dominates(left: RouteAssessment, right: RouteAssessment) -> bool:
    if not _same_partition((left, right)):
        return False
    pairs = tuple(
        (getattr(left, field), getattr(right, field), direction)
        for field, direction in _AXES
    )
    if any(left_value is None or right_value is None for left_value, right_value, _ in pairs):
        return False
    no_worse = all(
        left_value >= right_value
        if direction == "max"
        else left_value <= right_value
        for left_value, right_value, direction in pairs
    )
    better = any(
        left_value > right_value
        if direction == "max"
        else left_value < right_value
        for left_value, right_value, direction in pairs
    )
    return no_worse and better


def pareto_frontier(
    assessments: Tuple[RouteAssessment, ...],
) -> Tuple[RouteAssessment, ...]:
    items = _assessment_tuple(assessments)
    return tuple(
        item
        for item in items
        if not any(
            other.route.key != item.route.key and _dominates(other, item)
            for other in items
        )
    )


def _normalized_regret(
    item: RouteAssessment,
    frontier: Tuple[RouteAssessment, ...],
) -> float:
    if not _same_partition(frontier):
        return math.inf
    losses = []
    for field, direction in _AXES:
        values = tuple(getattr(candidate, field) for candidate in frontier)
        if any(value is None for value in values):
            continue
        low = min(values)
        high = max(values)
        if high == low:
            losses.append(0.0)
            continue
        value = getattr(item, field)
        normalized = (value - low) / (high - low)
        benefit = normalized if direction == "max" else 1.0 - normalized
        losses.append((1.0 - benefit) ** 2)
    if not losses:
        return math.inf
    return math.sqrt(sum(losses) / len(losses))


def _same_partition(items: Tuple[RouteAssessment, ...]) -> bool:
    if not items:
        return False
    identity = frozenset(items[0].comparable_groups)
    return bool(identity) and all(
        frozenset(item.comparable_groups) == identity
        for item in items[1:]
    )


def _economic_key(item: RouteAssessment):
    return (
        item.expected_cost is None,
        item.expected_cost if item.expected_cost is not None else math.inf,
        item.expected_latency is None,
        item.expected_latency if item.expected_latency is not None else math.inf,
        item.quality_signal is None,
        -(item.quality_signal if item.quality_signal is not None else -math.inf),
        item.route.key,
    )


def _safety_key(item: RouteAssessment):
    return (
        -item.evidence_grade,
        item.quality_signal is None,
        -(item.quality_signal if item.quality_signal is not None else -math.inf),
        item.residual_risk is None,
        item.residual_risk if item.residual_risk is not None else math.inf,
        item.expected_cost is None,
        item.expected_cost if item.expected_cost is not None else math.inf,
        item.route.key,
    )


def _economic_unpaired_key(item: RouteAssessment):
    return (
        item.expected_cost is None,
        -item.evidence_grade,
        item.route.key,
    )


def _safety_unpaired_key(item: RouteAssessment):
    return (
        -item.evidence_grade,
        item.quality_signal is None,
        item.residual_risk is None,
        item.expected_cost is None,
        item.route.key,
    )


def _official_role(
    frontier: Tuple[RouteAssessment, ...],
    role: str,
) -> RouteAssessment:
    matches = tuple(item for item in frontier if role in item.prior_roles)
    if len(matches) != 1:
        raise SelectionError(
            "official prior role %s must identify exactly one route" % role
        )
    return matches[0]


def _replace_frontier_item(
    frontier: Tuple[RouteAssessment, ...],
    changed: RouteAssessment,
) -> Tuple[RouteAssessment, ...]:
    return tuple(
        changed if item.route == changed.route else item
        for item in frontier
    )


def select_routes(
    assessments: Tuple[RouteAssessment, ...],
    profile: Profile,
    eliminated: Tuple[EliminatedRoute, ...] = (),
) -> RouteDecision:
    if type(profile) is not Profile:
        raise SelectionError("profile must be Profile")
    items = _assessment_tuple(assessments)
    removed = _eliminated_tuple(eliminated)
    viable_keys = {item.route.key for item in items}
    if viable_keys.intersection(item.route.key for item in removed):
        raise SelectionError("route cannot be both viable and eliminated")
    frontier = pareto_frontier(items)
    if not frontier:
        raise SelectionError("no viable frontier")

    if all(item.quality_basis == "official-prior" for item in frontier):
        economic = _official_role(frontier, "economic")
        ideal = _official_role(frontier, "ideal")
        maximum_safety = _official_role(frontier, "maximum-safety")
        return RouteDecision.from_assessments(
            frontier,
            economic,
            ideal,
            maximum_safety,
            eliminated=removed,
            rationale={
                "economic": "official price prior; provisional until local validation",
                "ideal": "structural cold-start prior; provisional until local validation",
                "maximum_safety": "official capability prior at highest available safety effort",
            },
        )

    eligible = tuple(
        item
        for item in frontier
        if item.meets_quality_floor(profile.quality_floor)
    )
    economic_pool = eligible or frontier
    economic = min(
        economic_pool,
        key=_economic_key if _same_partition(economic_pool) else _economic_unpaired_key,
    )
    if not eligible and not economic.provisional:
        economic = replace(economic, provisional=True)
        frontier = _replace_frontier_item(frontier, economic)
    ideal = min(
        frontier,
        key=lambda item: (_normalized_regret(item, frontier), item.route.key),
    )
    maximum_safety = min(
        frontier,
        key=_safety_key if _same_partition(frontier) else _safety_unpaired_key,
    )
    return RouteDecision.from_assessments(
        frontier,
        economic,
        ideal,
        maximum_safety,
        eliminated=removed,
        rationale={
            "economic": "lowest known total cost meeting the capability floor",
            "ideal": "minimum normalized regret on comparable axes",
            "maximum_safety": "highest evidence and quality with lowest residual risk",
        },
    )
