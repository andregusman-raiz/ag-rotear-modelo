from collections.abc import Mapping as RuntimeMapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional, Tuple

from .contracts import Effort, Route, TaskRequest
from .model_registry import CatalogSnapshot
from .profiles import Profile


def _copy_routes(value) -> Tuple[Route, ...]:
    try:
        routes = tuple(value)
    except Exception as error:
        raise ValueError("gate_result.viable must be an iterable") from error
    for index, route in enumerate(routes):
        if type(route) is not Route:
            raise ValueError("gate_result.viable[%d] must be Route" % index)
    return routes


def _copy_strings(value, path: str) -> Tuple[str, ...]:
    if type(value) in (str, bytes):
        raise ValueError("%s must be an iterable of strings" % path)
    try:
        items = tuple(value)
    except Exception as error:
        raise ValueError("%s must be an iterable" % path) from error
    for index, item in enumerate(items):
        if type(item) is not str or item.strip() == "":
            raise ValueError("%s[%d] must be a non-blank string" % (path, index))
    return items


def _freeze_string_mapping(value, path: str) -> Mapping[str, str]:
    if not isinstance(value, RuntimeMapping):
        raise ValueError("%s must be a mapping" % path)
    try:
        copied = dict(value)
    except Exception as error:
        raise ValueError("%s could not be read" % path) from error
    for key, item in copied.items():
        if type(key) is not str or key.strip() == "":
            raise ValueError("%s keys must be non-blank strings" % path)
        if type(item) is not str or item.strip() == "":
            raise ValueError("%s.%s must be a non-blank string" % (path, key))
    return MappingProxyType(copied)


@dataclass(frozen=True)
class GateResult:
    viable: Tuple[Route, ...]
    eliminated: Mapping[str, str]
    global_blockers: Tuple[str, ...]
    unknown_evidence: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        viable = _copy_routes(self.viable)
        eliminated = _freeze_string_mapping(
            self.eliminated,
            "gate_result.eliminated",
        )
        blockers = _copy_strings(
            self.global_blockers,
            "gate_result.global_blockers",
        )
        unknown = _freeze_string_mapping(
            self.unknown_evidence,
            "gate_result.unknown_evidence",
        )
        viable_keys = {route.key for route in viable}
        overlap = viable_keys.intersection(eliminated)
        if overlap:
            raise ValueError("gate_result route cannot be viable and eliminated")
        if not set(unknown).issubset(viable_keys):
            raise ValueError("gate_result unknown evidence must belong to viable routes")
        object.__setattr__(self, "viable", viable)
        object.__setattr__(self, "eliminated", eliminated)
        object.__setattr__(self, "global_blockers", blockers)
        object.__setattr__(self, "unknown_evidence", unknown)


def apply_gates(
    request: TaskRequest,
    candidates: Tuple[Route, ...],
    profile: Profile,
    budget_open: bool,
    catalog: Optional[CatalogSnapshot] = None,
) -> GateResult:
    if not budget_open:
        return GateResult((), {}, ("budget-exhausted",))
    if request.external_effects and not request.external_effects_authorized:
        return GateResult((), {}, ("external-effects-not-authorized",))
    if request.impact == "critical" and not request.validation_checks:
        return GateResult((), {}, ("critical-task-without-validator",))

    models = {} if catalog is None else {model.slug: model for model in catalog.models}
    viable = []
    eliminated = {}
    unknown_evidence = {}
    for route in tuple(candidates):
        if route.effort is Effort.ULTRA:
            if request.decomposability != "high" or request.independent_fronts < 2:
                eliminated[route.key] = "ultra-without-useful-decomposition"
                continue
            if request.parallel_writes and not request.worktree_isolated:
                eliminated[route.key] = "parallel-write-without-worktree"
                continue

        if request.required_tools:
            model = models.get(route.model)
            if model is None or not model.supported_tools_known:
                unknown_evidence[route.key] = "supported-tools-not-observed"
            else:
                missing = tuple(
                    tool
                    for tool in request.required_tools
                    if tool not in model.supported_tools
                )
                if missing:
                    eliminated[route.key] = "required-tools-unsupported:%s" % ",".join(
                        missing
                    )
                    continue
        viable.append(route)
    return GateResult(
        viable,
        eliminated,
        (),
        unknown_evidence,
    )
