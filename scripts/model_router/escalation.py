import math
import time
from collections.abc import Mapping as RuntimeMapping
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Callable, Mapping, Optional, Tuple

from .contracts import (
    Effort,
    FailureClass,
    Route,
    RouteAssessment,
    RouteDecision,
    TaskRequest,
)


class BudgetError(ValueError):
    pass


class ProgressError(ValueError):
    pass


class EscalationError(ValueError):
    pass


DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MAX_SECONDS = 3600.0
DEFAULT_PROGRESS_DIRECTIONS = MappingProxyType(
    {"required_checks_passed": "max"}
)
EFFORT_LADDER = (
    Effort.LOW,
    Effort.MEDIUM,
    Effort.HIGH,
    Effort.XHIGH,
    Effort.MAX,
)
_EFFORT_INDEX = {effort: index for index, effort in enumerate(EFFORT_LADDER)}
ESCALATION_REASON_CODES = (
    "external-dependency",
    "single-transient-retry",
    "increase-depth",
    "parallel-coverage",
    "increase-capacity-or-safety",
    "next-safe-route",
    "no-untried-route-with-plausible-gain",
)


def _finite_number(value, path: str, positive: bool = False) -> float:
    if type(value) not in (int, float):
        raise ValueError("%s must be a number" % path)
    if not math.isfinite(value):
        raise ValueError("%s must be finite" % path)
    if positive and value <= 0:
        raise ValueError("%s must be positive" % path)
    return float(value)


class BudgetLedger:
    def __init__(
        self,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if type(max_attempts) is not int or max_attempts <= 0:
            raise BudgetError("max_attempts must be a positive integer")
        if max_attempts > DEFAULT_MAX_ATTEMPTS:
            raise BudgetError(
                "max_attempts must not exceed the global attempt limit"
            )
        try:
            parsed_seconds = _finite_number(
                max_seconds, "max_seconds", positive=True
            )
        except ValueError as error:
            raise BudgetError(str(error)) from None
        if parsed_seconds > DEFAULT_MAX_SECONDS:
            raise BudgetError(
                "max_seconds must not exceed the global time limit"
            )
        if not callable(clock):
            raise BudgetError("clock must be callable")
        try:
            started = _finite_number(clock(), "clock")
        except Exception:
            raise BudgetError("clock must return a finite number") from None
        self.max_attempts = max_attempts
        self.max_seconds = parsed_seconds
        self.clock = clock
        self.started_at = started
        self._last_seen = started
        self._attempts = 0
        self._clock_failed = False
        self._lock = RLock()

    @property
    def attempts(self) -> int:
        with self._lock:
            return self._attempts

    def _elapsed_locked(self) -> float:
        if self._clock_failed:
            return self.max_seconds
        try:
            current = _finite_number(self.clock(), "clock")
        except Exception:
            self._clock_failed = True
            return self.max_seconds
        if current < self._last_seen or current < self.started_at:
            self._clock_failed = True
            return self.max_seconds
        self._last_seen = current
        return current - self.started_at

    def elapsed_seconds(self) -> float:
        with self._lock:
            return min(self.max_seconds, self._elapsed_locked())

    def remaining_seconds(self) -> float:
        with self._lock:
            return max(0.0, self.max_seconds - self._elapsed_locked())

    def can_start(self) -> bool:
        with self._lock:
            if self._attempts >= self.max_attempts:
                return False
            return self._elapsed_locked() < self.max_seconds

    def record_attempt(self) -> None:
        with self._lock:
            if (
                self._attempts >= self.max_attempts
                or self._elapsed_locked() >= self.max_seconds
            ):
                raise RuntimeError("execution budget exhausted")
            self._attempts += 1

    def stop_reason(self) -> Optional[str]:
        with self._lock:
            if self._attempts >= self.max_attempts:
                return "attempt-limit"
            if self._elapsed_locked() >= self.max_seconds:
                return "time-limit"
            return None


def _copy_directions(value) -> Mapping[str, str]:
    if not isinstance(value, RuntimeMapping):
        raise ProgressError("directions must be a mapping")
    try:
        copied = dict(value)
    except Exception:
        raise ProgressError("directions could not be read") from None
    if not copied:
        raise ProgressError("directions must not be empty")
    for name in copied:
        if type(name) is not str or name.strip() == "":
            raise ProgressError("directions keys must be non-blank strings")
    parsed = {}
    for name, direction in sorted(copied.items()):
        if type(direction) is not str or direction not in ("max", "min"):
            raise ProgressError("directions.%s must be max or min" % name)
        parsed[name] = direction
    return MappingProxyType(parsed)


def _copy_metrics(value, directions: Mapping[str, str]) -> Mapping[str, float]:
    if not isinstance(value, RuntimeMapping):
        raise ProgressError("metrics must be a mapping")
    try:
        copied = dict(value)
    except Exception:
        raise ProgressError("metrics could not be read") from None
    for name in copied:
        if type(name) is not str or name.strip() == "":
            raise ProgressError("metrics keys must be non-blank strings")
    extras = tuple(sorted(set(copied).difference(directions)))
    if extras:
        raise ProgressError("metrics contains undeclared metric %s" % extras[0])
    parsed = {}
    for name in directions:
        if name not in copied:
            continue
        try:
            parsed[name] = _finite_number(copied[name], "metrics.%s" % name)
        except ValueError as error:
            raise ProgressError(str(error)) from None
    return MappingProxyType(parsed)


class ProgressTracker:
    def __init__(self, directions=None, noisy: bool = False):
        selected = DEFAULT_PROGRESS_DIRECTIONS if directions is None else directions
        self.directions = _copy_directions(selected)
        if type(noisy) is not bool:
            raise ProgressError("noisy must be a boolean")
        self.noisy = noisy
        self.previous = None  # type: Optional[Mapping[str, float]]
        self.non_improvements = 0
        self._lock = RLock()

    def _improves(
        self,
        current: Mapping[str, float],
        previous: Mapping[str, float],
    ) -> bool:
        if any(name not in current or name not in previous for name in self.directions):
            return False
        comparisons = []
        for name, direction in self.directions.items():
            left = current[name]
            right = previous[name]
            if direction == "max":
                comparisons.append((left >= right, left > right))
            else:
                comparisons.append((left <= right, left < right))
        return all(item[0] for item in comparisons) and any(
            item[1] for item in comparisons
        )

    def record(self, metrics) -> None:
        current = _copy_metrics(metrics, self.directions)
        with self._lock:
            if self.previous is not None:
                if self._improves(current, self.previous):
                    self.non_improvements = 0
                else:
                    self.non_improvements += 1
            self.previous = current

    @property
    def stagnated(self) -> bool:
        with self._lock:
            threshold = 2 if self.noisy else 1
            return self.non_improvements >= threshold


def _route_tuple(value, path: str, reject_duplicates: bool) -> Tuple[Route, ...]:
    if type(value) is not tuple:
        raise EscalationError("%s must be a tuple" % path)
    seen = set()
    for index, route in enumerate(value):
        if type(route) is not Route:
            raise EscalationError("%s[%d] must be Route" % (path, index))
        if reject_duplicates and route.key in seen:
            raise EscalationError("%s[%d] is a duplicate route" % (path, index))
        seen.add(route.key)
    return tuple(value)


def _frontier_tuple(value) -> Tuple[RouteAssessment, ...]:
    if type(value) is not tuple:
        raise EscalationError("frontier must be a tuple")
    seen = set()
    for index, item in enumerate(value):
        if type(item) is not RouteAssessment:
            raise EscalationError("frontier[%d] must be RouteAssessment" % index)
        numeric_fields = (
            "evidence_grade",
            "quality_signal",
            "expected_cost",
            "expected_latency",
            "residual_risk",
        )
        for field_name in numeric_fields:
            field_value = getattr(item, field_name)
            if field_value is None:
                continue
            if type(field_value) not in (int, float) or not math.isfinite(
                field_value
            ):
                raise EscalationError(
                    "frontier[%d].%s must be finite" % (index, field_name)
                )
        if item.route.key in seen:
            raise EscalationError("frontier[%d] is a duplicate route" % index)
        seen.add(item.route.key)
    return tuple(value)


def _same_partition(items: Tuple[RouteAssessment, ...]) -> bool:
    if not items:
        return False
    partition = frozenset(items[0].comparable_groups)
    return bool(partition) and all(
        frozenset(item.comparable_groups) == partition for item in items[1:]
    )


def _comparable_safety_key(item: RouteAssessment):
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


def _structural_safety_key(route: Route, assessment: Optional[RouteAssessment]):
    if assessment is None:
        return (1, 0, True, True, True, route.key)
    return (
        0,
        -assessment.evidence_grade,
        assessment.quality_signal is None,
        assessment.residual_risk is None,
        assessment.expected_cost is None,
        route.key,
    )


def safest_route(
    routes: Tuple[Route, ...],
    frontier: Tuple[RouteAssessment, ...],
) -> Route:
    candidates = _route_tuple(routes, "routes", reject_duplicates=True)
    if not candidates:
        raise EscalationError("routes must not be empty")
    assessments = _frontier_tuple(frontier)
    by_key = {item.route.key: item for item in assessments}
    known = tuple(
        by_key[route.key] for route in candidates if route.key in by_key
    )
    if len(known) == len(candidates) and _same_partition(known):
        return min(known, key=_comparable_safety_key).route
    return min(
        candidates,
        key=lambda route: _structural_safety_key(route, by_key.get(route.key)),
    )


@dataclass(frozen=True)
class EscalationDecision:
    route: Optional[Route]
    reason: str
    retry_same_route: bool = False

    def __post_init__(self):
        if self.route is not None and type(self.route) is not Route:
            raise EscalationError("escalation.route must be Route or null")
        if type(self.reason) is not str or self.reason not in ESCALATION_REASON_CODES:
            raise EscalationError("escalation.reason must be a known reason code")
        if type(self.retry_same_route) is not bool:
            raise EscalationError("escalation.retry_same_route must be a boolean")
        if self.route is None and self.retry_same_route:
            raise EscalationError("blocked escalation cannot retry a route")
        if self.retry_same_route and self.reason != "single-transient-retry":
            raise EscalationError("route retry requires transient retry reason")

    @classmethod
    def blocked(cls, reason: str):
        return cls(None, reason, False)

    @classmethod
    def retry(cls, route: Route, reason: str):
        return cls(route, reason, True)

    @classmethod
    def move(cls, route: Route, reason: str):
        return cls(route, reason, False)


def choose_next_route(
    failure: FailureClass,
    current: Route,
    viable: Tuple[Route, ...],
    route_decision: RouteDecision,
    history: Tuple[Route, ...],
    request: TaskRequest,
) -> EscalationDecision:
    if type(current) is not Route:
        raise EscalationError("current must be Route")
    routes = _route_tuple(viable, "viable", reject_duplicates=True)
    attempted = _route_tuple(history, "history", reject_duplicates=False)
    if type(route_decision) is not RouteDecision:
        raise EscalationError("route_decision must be RouteDecision")
    if type(request) is not TaskRequest:
        raise EscalationError("request must be TaskRequest")
    if type(failure) is not FailureClass:
        return EscalationDecision.blocked(
            "no-untried-route-with-plausible-gain"
        )

    viable_keys = frozenset(route.key for route in routes)
    used = frozenset(route.key for route in attempted)
    if failure is FailureClass.EXTERNAL_BLOCK:
        return EscalationDecision.blocked("external-dependency")
    if failure is FailureClass.TRANSIENT:
        occurrences = sum(route == current for route in attempted)
        if current.key in viable_keys and occurrences <= 1:
            return EscalationDecision.retry(
                current,
                "single-transient-retry",
            )
        return EscalationDecision.blocked(
            "no-untried-route-with-plausible-gain"
        )

    available = tuple(
        route
        for route in routes
        if route.key not in used and route != current
    )
    if failure is not FailureClass.COVERAGE:
        available = tuple(
            route for route in available if route.effort is not Effort.ULTRA
        )

    if failure is FailureClass.DEPTH and current.effort in _EFFORT_INDEX:
        current_index = _EFFORT_INDEX[current.effort]
        deeper = tuple(
            sorted(
                (
                    route
                    for route in available
                    if route.model == current.model
                    and route.effort in _EFFORT_INDEX
                    and _EFFORT_INDEX[route.effort] > current_index
                ),
                key=lambda route: (_EFFORT_INDEX[route.effort], route.key),
            )
        )
        if deeper:
            return EscalationDecision.move(deeper[0], "increase-depth")

    if failure is FailureClass.COVERAGE:
        ultra_allowed = (
            request.decomposability == "high"
            and request.independent_fronts >= 2
            and (not request.parallel_writes or request.worktree_isolated)
        )
        if ultra_allowed:
            ultra = tuple(
                route
                for route in available
                if route.effort is Effort.ULTRA
                and route.topology.value == "multi"
            )
            if ultra:
                return EscalationDecision.move(
                    safest_route(ultra, route_decision.frontier),
                    "parallel-coverage",
                )
        available = tuple(
            route for route in available if route.effort is not Effort.ULTRA
        )

    if failure is FailureClass.CAPACITY:
        different_model = tuple(
            route for route in available if route.model != current.model
        )
        if different_model:
            maximum = route_decision.maximum_safety
            selected = (
                maximum
                if maximum in different_model
                else safest_route(different_model, route_decision.frontier)
            )
            return EscalationDecision.move(
                selected,
                "increase-capacity-or-safety",
            )
        return EscalationDecision.blocked(
            "no-untried-route-with-plausible-gain"
        )

    if failure in (
        FailureClass.DEPTH,
        FailureClass.CAPACITY,
        FailureClass.COVERAGE,
        FailureClass.RISK,
    ):
        maximum = route_decision.maximum_safety
        if maximum in available:
            return EscalationDecision.move(
                maximum,
                "increase-capacity-or-safety",
            )
        if available:
            return EscalationDecision.move(
                safest_route(available, route_decision.frontier),
                "next-safe-route",
            )
    return EscalationDecision.blocked(
        "no-untried-route-with-plausible-gain"
    )
