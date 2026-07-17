from dataclasses import dataclass
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple


class ContractError(ValueError):
    pass


def _fail(path: str, message: str):
    raise ContractError("%s %s" % (path, message))


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(path, "must be a mapping")
    try:
        return dict(value)
    except Exception as error:
        raise ContractError("%s could not be read as a mapping" % path) from error


def _validate_mapping_keys(
    payload: Mapping[str, Any],
    path: str,
) -> Mapping[str, Any]:
    for key in payload:
        if type(key) is not str:
            _fail(path + ".<key>", "must be a string")
    return payload


def _validate_object(
    value: Any,
    path: str,
    required: Tuple[str, ...],
    allowed: Optional[Tuple[str, ...]] = None,
) -> Mapping[str, Any]:
    payload = _validate_mapping_keys(_require_mapping(value, path), path)
    allowed_fields = frozenset(allowed or required)
    missing = [field_name for field_name in required if field_name not in payload]
    if missing:
        _fail("%s.%s" % (path, missing[0]), "is required")
    extras = [field_name for field_name in payload if field_name not in allowed_fields]
    if extras:
        _fail("%s.%s" % (path, extras[0]), "is not allowed")
    return payload


def _require_string(value: Any, path: str) -> str:
    if type(value) is not str:
        _fail(path, "must be a string")
    if value.strip() == "":
        _fail(path, "must not be blank")
    return value


def _require_optional_string(value: Any, path: str) -> Optional[str]:
    if value is None:
        return None
    return _require_string(value, path)


def _require_choice(value: Any, path: str, allowed: Tuple[str, ...]) -> str:
    parsed = _require_string(value, path)
    if parsed not in allowed:
        _fail(path, "must be one of: %s" % ", ".join(allowed))
    return parsed


def _require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _require_int(value: Any, path: str, minimum: Optional[int] = None) -> int:
    if type(value) is int:
        parsed = value
    elif type(value) is float and isfinite(value) and value.is_integer():
        parsed = int(value)
    else:
        _fail(path, "must be an integer")
    if minimum is not None and parsed < minimum:
        _fail(path, "must be at least %d" % minimum)
    return parsed


def _require_optional_int(value: Any, path: str) -> Optional[int]:
    if value is None:
        return None
    return _require_int(value, path)


def _require_number(value: Any, path: str):
    if type(value) not in (int, float):
        _fail(path, "must be a number")
    if type(value) is float and not isfinite(value):
        _fail(path, "must be finite")
    return value


def _require_optional_number(value: Any, path: str):
    if value is None:
        return None
    return _require_number(value, path)


def _require_list(value: Any, path: str):
    if type(value) is not list:
        _fail(path, "must be an array")
    return value


def _require_tuple(value: Any, path: str):
    if type(value) is not tuple:
        _fail(path, "must be a tuple")
    return value


def _parse_string_list(value: Any, path: str) -> Tuple[str, ...]:
    items = _require_list(value, path)
    return tuple(
        _require_string(item, "%s[%d]" % (path, index))
        for index, item in enumerate(items)
    )


def _validate_string_tuple(value: Any, path: str) -> Tuple[str, ...]:
    items = _require_tuple(value, path)
    for index, item in enumerate(items):
        _require_string(item, "%s[%d]" % (path, index))
    return items


def _require_instance(value: Any, expected_type, path: str):
    if type(value) is not expected_type:
        _fail(path, "must be %s" % expected_type.__name__)
    return value


def _validate_instance_tuple(
    value: Any,
    expected_type,
    path: str,
    minimum_items: int = 0,
):
    items = _require_tuple(value, path)
    if len(items) < minimum_items:
        _fail(path, "must contain at least %d item(s)" % minimum_items)
    for index, item in enumerate(items):
        _require_instance(item, expected_type, "%s[%d]" % (path, index))
    return items


def _parse_object_list(value: Any, path: str, parser) -> tuple:
    items = _require_list(value, path)
    return tuple(
        parser(item, "%s[%d]" % (path, index))
        for index, item in enumerate(items)
    )


def _parse_numeric_mapping(value: Any, path: str) -> Dict[str, float]:
    payload = _validate_mapping_keys(_require_mapping(value, path), path)
    parsed = {}
    for key, item in payload.items():
        _require_string(key, path)
        parsed[key] = _require_number(item, "%s.%s" % (path, key))
    return parsed


def _freeze_numeric_mapping(value: Any, path: str):
    return MappingProxyType(dict(_parse_numeric_mapping(value, path)))


def _parse_string_mapping(value: Any, path: str) -> Dict[str, str]:
    payload = _validate_mapping_keys(_require_mapping(value, path), path)
    parsed = {}
    for key, item in payload.items():
        _require_string(key, path)
        parsed[key] = _require_string(item, "%s.%s" % (path, key))
    return parsed


def _freeze_string_mapping(value: Any, path: str):
    return MappingProxyType(dict(_parse_string_mapping(value, path)))


def _materialize_instances(value: Any, expected_type, path: str) -> tuple:
    try:
        items = tuple(value)
    except Exception as error:
        raise ContractError("%s could not be read as an iterable" % path) from error
    return _validate_instance_tuple(items, expected_type, path)


def _materialize_strings(value: Any, path: str) -> Tuple[str, ...]:
    if type(value) in (str, bytes):
        _fail(path, "must be an iterable of strings")
    try:
        items = tuple(value)
    except Exception as error:
        raise ContractError("%s could not be read as an iterable" % path) from error
    return _validate_string_tuple(items, path)


class TextEnum(str, Enum):
    @classmethod
    def _missing_(cls, value):
        allowed = ", ".join(item.value for item in cls)
        raise ContractError("%s must be one of: %s" % (cls.__name__, allowed))

    @classmethod
    def parse(cls, field_name: str, value: Any):
        _require_string(value, field_name)
        try:
            return cls(value)
        except (TypeError, ValueError):
            allowed = ", ".join(item.value for item in cls)
            raise ContractError("%s must be one of: %s" % (field_name, allowed))


class Effort(TextEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class Topology(TextEnum):
    SINGLE = "single"
    MULTI = "multi"


class SandboxMode(TextEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class ApprovalPolicy(TextEnum):
    UNTRUSTED = "untrusted"
    ON_REQUEST = "on-request"
    NEVER = "never"


class FailureClass(TextEnum):
    TRANSIENT = "transient"
    DEPTH = "depth"
    CAPACITY = "capacity"
    COVERAGE = "coverage"
    RISK = "risk"
    EXTERNAL_BLOCK = "external_block"


def _parse_enum(enum_type, value: Any, path: str):
    return enum_type.parse(path, value)


def _require_enum(value: Any, enum_type, path: str):
    return _require_instance(value, enum_type, path)


def _parse_optional_enum(enum_type, value: Any, path: str):
    if value is None:
        return None
    return _parse_enum(enum_type, value, path)


def _require_optional_enum(value: Any, enum_type, path: str):
    if value is None:
        return None
    return _require_enum(value, enum_type, path)


_ROUTE_FIELDS = ("model", "effort")


@dataclass(frozen=True)
class Route:
    model: str
    effort: Effort

    def __post_init__(self):
        _require_string(self.model, "route.model")
        _require_enum(self.effort, Effort, "route.effort")

    @property
    def topology(self) -> Topology:
        return Topology.MULTI if self.effort is Effort.ULTRA else Topology.SINGLE

    @property
    def key(self) -> str:
        return "%s:%s:%s" % (self.model, self.effort.value, self.topology.value)

    @classmethod
    def _from_dict(cls, payload: Mapping[str, Any], path: str):
        payload = _validate_object(payload, path, _ROUTE_FIELDS)
        return cls(
            model=_require_string(payload["model"], path + ".model"),
            effort=_parse_enum(Effort, payload["effort"], path + ".effort"),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]):
        return cls._from_dict(payload, "route")

    def to_dict(self):
        return {"model": self.model, "effort": self.effort.value}


_VALIDATION_CHECK_FIELDS = ("id", "category", "required")


@dataclass(frozen=True)
class ValidationCheck:
    id: str
    category: str
    required: bool

    def __post_init__(self):
        _require_string(self.id, "validation_check.id")
        _require_string(self.category, "validation_check.category")
        _require_bool(self.required, "validation_check.required")

    @classmethod
    def _from_dict(cls, payload: Mapping[str, Any], path: str):
        payload = _validate_object(payload, path, _VALIDATION_CHECK_FIELDS)
        return cls(
            id=_require_string(payload["id"], path + ".id"),
            category=_require_string(payload["category"], path + ".category"),
            required=_require_bool(payload["required"], path + ".required"),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]):
        return cls._from_dict(payload, "validation_check")

    def to_dict(self):
        return {"id": self.id, "category": self.category, "required": self.required}


_TASK_REQUEST_FIELDS = (
    "schema_version",
    "primary_profile",
    "secondary_profiles",
    "archetype",
    "novelty",
    "ambiguity",
    "reasoning_depth",
    "context_load",
    "tool_dependency",
    "urgency",
    "cost_tolerance",
    "latency_tolerance",
    "verifiability",
    "impact",
    "reversibility",
    "external_effects",
    "external_effects_authorized",
    "decomposability",
    "independent_fronts",
    "parallel_writes",
    "worktree_isolated",
    "required_tools",
    "acceptance_criteria",
    "validation_checks",
)

_TASK_CHOICES = {
    "novelty": ("low", "medium", "high", "ood"),
    "ambiguity": ("low", "medium", "high"),
    "reasoning_depth": ("low", "medium", "high"),
    "context_load": ("small", "medium", "large", "distributed"),
    "tool_dependency": ("none", "low", "high"),
    "urgency": ("low", "normal", "high"),
    "cost_tolerance": ("low", "default", "high"),
    "latency_tolerance": ("low", "default", "high"),
    "verifiability": ("strong", "mixed", "weak"),
    "impact": ("low", "medium", "high", "critical"),
    "reversibility": ("easy", "partial", "hard"),
    "decomposability": ("none", "limited", "high"),
}


@dataclass(frozen=True)
class TaskRequest:
    schema_version: str
    primary_profile: str
    secondary_profiles: Tuple[str, ...]
    archetype: str
    novelty: str
    ambiguity: str
    reasoning_depth: str
    context_load: str
    tool_dependency: str
    urgency: str
    cost_tolerance: str
    latency_tolerance: str
    verifiability: str
    impact: str
    reversibility: str
    external_effects: bool
    external_effects_authorized: bool
    decomposability: str
    independent_fronts: int
    parallel_writes: bool
    worktree_isolated: bool
    required_tools: Tuple[str, ...]
    acceptance_criteria: Tuple[str, ...]
    validation_checks: Tuple[ValidationCheck, ...]

    def __post_init__(self):
        _require_string(self.schema_version, "task_request.schema_version")
        if self.schema_version != "1.0.0":
            _fail("task_request.schema_version", "must equal 1.0.0")
        _require_string(self.primary_profile, "task_request.primary_profile")
        _validate_string_tuple(self.secondary_profiles, "task_request.secondary_profiles")
        _require_string(self.archetype, "task_request.archetype")
        for field_name, allowed in _TASK_CHOICES.items():
            _require_choice(
                getattr(self, field_name),
                "task_request.%s" % field_name,
                allowed,
            )
        _require_bool(self.external_effects, "task_request.external_effects")
        _require_bool(
            self.external_effects_authorized,
            "task_request.external_effects_authorized",
        )
        object.__setattr__(
            self,
            "independent_fronts",
            _require_int(
                self.independent_fronts,
                "task_request.independent_fronts",
                minimum=1,
            ),
        )
        _require_bool(self.parallel_writes, "task_request.parallel_writes")
        _require_bool(self.worktree_isolated, "task_request.worktree_isolated")
        _validate_string_tuple(self.required_tools, "task_request.required_tools")
        _validate_string_tuple(
            self.acceptance_criteria,
            "task_request.acceptance_criteria",
        )
        _validate_instance_tuple(
            self.validation_checks,
            ValidationCheck,
            "task_request.validation_checks",
            minimum_items=1,
        )

    @property
    def strongly_verifiable(self) -> bool:
        return self.verifiability == "strong"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]):
        path = "task_request"
        payload = _validate_object(payload, path, _TASK_REQUEST_FIELDS)
        values = {
            field_name: _require_choice(
                payload[field_name],
                "%s.%s" % (path, field_name),
                allowed,
            )
            for field_name, allowed in _TASK_CHOICES.items()
        }
        schema_version = _require_string(
            payload["schema_version"],
            path + ".schema_version",
        )
        if schema_version != "1.0.0":
            _fail(path + ".schema_version", "must equal 1.0.0")
        checks = _parse_object_list(
            payload["validation_checks"],
            path + ".validation_checks",
            ValidationCheck._from_dict,
        )
        if not checks:
            _fail(path + ".validation_checks", "must not be empty")
        return cls(
            schema_version=schema_version,
            primary_profile=_require_string(
                payload["primary_profile"],
                path + ".primary_profile",
            ),
            secondary_profiles=_parse_string_list(
                payload["secondary_profiles"],
                path + ".secondary_profiles",
            ),
            archetype=_require_string(payload["archetype"], path + ".archetype"),
            novelty=values["novelty"],
            ambiguity=values["ambiguity"],
            reasoning_depth=values["reasoning_depth"],
            context_load=values["context_load"],
            tool_dependency=values["tool_dependency"],
            urgency=values["urgency"],
            cost_tolerance=values["cost_tolerance"],
            latency_tolerance=values["latency_tolerance"],
            verifiability=values["verifiability"],
            impact=values["impact"],
            reversibility=values["reversibility"],
            external_effects=_require_bool(
                payload["external_effects"],
                path + ".external_effects",
            ),
            external_effects_authorized=_require_bool(
                payload["external_effects_authorized"],
                path + ".external_effects_authorized",
            ),
            decomposability=values["decomposability"],
            independent_fronts=_require_int(
                payload["independent_fronts"],
                path + ".independent_fronts",
                minimum=1,
            ),
            parallel_writes=_require_bool(
                payload["parallel_writes"],
                path + ".parallel_writes",
            ),
            worktree_isolated=_require_bool(
                payload["worktree_isolated"],
                path + ".worktree_isolated",
            ),
            required_tools=_parse_string_list(
                payload["required_tools"],
                path + ".required_tools",
            ),
            acceptance_criteria=_parse_string_list(
                payload["acceptance_criteria"],
                path + ".acceptance_criteria",
            ),
            validation_checks=checks,
        )

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "primary_profile": self.primary_profile,
            "secondary_profiles": list(self.secondary_profiles),
            "archetype": self.archetype,
            "novelty": self.novelty,
            "ambiguity": self.ambiguity,
            "reasoning_depth": self.reasoning_depth,
            "context_load": self.context_load,
            "tool_dependency": self.tool_dependency,
            "urgency": self.urgency,
            "cost_tolerance": self.cost_tolerance,
            "latency_tolerance": self.latency_tolerance,
            "verifiability": self.verifiability,
            "impact": self.impact,
            "reversibility": self.reversibility,
            "external_effects": self.external_effects,
            "external_effects_authorized": self.external_effects_authorized,
            "decomposability": self.decomposability,
            "independent_fronts": self.independent_fronts,
            "parallel_writes": self.parallel_writes,
            "worktree_isolated": self.worktree_isolated,
            "required_tools": list(self.required_tools),
            "acceptance_criteria": list(self.acceptance_criteria),
            "validation_checks": [item.to_dict() for item in self.validation_checks],
        }


_EVIDENCE_FIELDS = ("category", "passed", "summary", "exit_code")
_EVIDENCE_REQUIRED_FIELDS = ("category", "passed", "summary")


@dataclass(frozen=True)
class EvidenceItem:
    category: str
    passed: bool
    summary: str
    exit_code: Optional[int] = None

    def __post_init__(self):
        _require_string(self.category, "evidence.category")
        _require_bool(self.passed, "evidence.passed")
        _require_string(self.summary, "evidence.summary")
        object.__setattr__(
            self,
            "exit_code",
            _require_optional_int(self.exit_code, "evidence.exit_code"),
        )

    @classmethod
    def _from_dict(cls, payload: Mapping[str, Any], path: str):
        payload = _validate_object(
            payload,
            path,
            _EVIDENCE_REQUIRED_FIELDS,
            _EVIDENCE_FIELDS,
        )
        return cls(
            category=_require_string(payload["category"], path + ".category"),
            passed=_require_bool(payload["passed"], path + ".passed"),
            summary=_require_string(payload["summary"], path + ".summary"),
            exit_code=_require_optional_int(
                payload.get("exit_code"),
                path + ".exit_code",
            ),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]):
        return cls._from_dict(payload, "evidence")

    def to_dict(self):
        return {
            "category": self.category,
            "passed": self.passed,
            "summary": self.summary,
            "exit_code": self.exit_code,
        }


_CHILD_REPORT_FIELDS = (
    "status",
    "deliverable",
    "evidence",
    "metrics",
    "failure_class",
    "next_hint",
)
_CHILD_STATUSES = ("pass", "fail", "blocked")


@dataclass(frozen=True)
class ChildReport:
    status: str
    deliverable: str
    evidence: Tuple[EvidenceItem, ...]
    metrics: Dict[str, float]
    failure_class: Optional[FailureClass]
    next_hint: Optional[str]

    def __post_init__(self):
        _require_choice(self.status, "child_report.status", _CHILD_STATUSES)
        _require_string(self.deliverable, "child_report.deliverable")
        _validate_instance_tuple(self.evidence, EvidenceItem, "child_report.evidence")
        object.__setattr__(
            self,
            "metrics",
            _freeze_numeric_mapping(self.metrics, "child_report.metrics"),
        )
        _require_optional_enum(
            self.failure_class,
            FailureClass,
            "child_report.failure_class",
        )
        _require_optional_string(self.next_hint, "child_report.next_hint")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]):
        path = "child_report"
        payload = _validate_object(payload, path, _CHILD_REPORT_FIELDS)
        return cls(
            status=_require_choice(payload["status"], path + ".status", _CHILD_STATUSES),
            deliverable=_require_string(
                payload["deliverable"],
                path + ".deliverable",
            ),
            evidence=_parse_object_list(
                payload["evidence"],
                path + ".evidence",
                EvidenceItem._from_dict,
            ),
            metrics=_parse_numeric_mapping(payload["metrics"], path + ".metrics"),
            failure_class=_parse_optional_enum(
                FailureClass,
                payload["failure_class"],
                path + ".failure_class",
            ),
            next_hint=_require_optional_string(
                payload["next_hint"],
                path + ".next_hint",
            ),
        )

    def to_dict(self):
        return {
            "status": self.status,
            "deliverable": self.deliverable,
            "evidence": [item.to_dict() for item in self.evidence],
            "metrics": dict(self.metrics),
            "failure_class": self.failure_class.value if self.failure_class else None,
            "next_hint": self.next_hint,
        }


_VALIDATION_RESULT_FIELDS = (
    "status",
    "evidence",
    "metrics",
    "failure_class",
    "stop_reason",
    "requires_independent_verifier",
)
_VALIDATION_STATUSES = ("pass", "fail", "blocked", "needs-verifier")


@dataclass(frozen=True)
class ValidationResult:
    status: str
    evidence: Tuple[EvidenceItem, ...]
    metrics: Dict[str, float]
    failure_class: Optional[FailureClass]
    stop_reason: Optional[str]
    requires_independent_verifier: bool = False

    def __post_init__(self):
        _require_choice(
            self.status,
            "validation_result.status",
            _VALIDATION_STATUSES,
        )
        _validate_instance_tuple(
            self.evidence,
            EvidenceItem,
            "validation_result.evidence",
        )
        object.__setattr__(
            self,
            "metrics",
            _freeze_numeric_mapping(self.metrics, "validation_result.metrics"),
        )
        _require_optional_enum(
            self.failure_class,
            FailureClass,
            "validation_result.failure_class",
        )
        _require_optional_string(self.stop_reason, "validation_result.stop_reason")
        _require_bool(
            self.requires_independent_verifier,
            "validation_result.requires_independent_verifier",
        )
        if self.requires_independent_verifier != (self.status == "needs-verifier"):
            _fail(
                "validation_result.requires_independent_verifier",
                "must be true exactly when status is needs-verifier",
            )
        if self.status == "pass" and self.failure_class is not None:
            _fail(
                "validation_result.failure_class",
                "must be null when status is pass",
            )
        if self.status == "pass" and self.stop_reason is not None:
            _fail(
                "validation_result.stop_reason",
                "must be null when status is pass",
            )
        if self.status in ("fail", "blocked") and self.failure_class is None:
            _fail(
                "validation_result.failure_class",
                "is required when status is fail or blocked",
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]):
        path = "validation_result"
        payload = _validate_object(payload, path, _VALIDATION_RESULT_FIELDS)
        return cls(
            status=_require_choice(
                payload["status"],
                path + ".status",
                _VALIDATION_STATUSES,
            ),
            evidence=_parse_object_list(
                payload["evidence"],
                path + ".evidence",
                EvidenceItem._from_dict,
            ),
            metrics=_parse_numeric_mapping(payload["metrics"], path + ".metrics"),
            failure_class=_parse_optional_enum(
                FailureClass,
                payload["failure_class"],
                path + ".failure_class",
            ),
            stop_reason=_require_optional_string(
                payload["stop_reason"],
                path + ".stop_reason",
            ),
            requires_independent_verifier=_require_bool(
                payload["requires_independent_verifier"],
                path + ".requires_independent_verifier",
            ),
        )

    def to_dict(self):
        return {
            "status": self.status,
            "evidence": [item.to_dict() for item in self.evidence],
            "metrics": dict(self.metrics),
            "failure_class": self.failure_class.value if self.failure_class else None,
            "stop_reason": self.stop_reason,
            "requires_independent_verifier": self.requires_independent_verifier,
        }

    @classmethod
    def passed(cls, evidence, metrics):
        return cls(
            "pass",
            _materialize_instances(
                evidence,
                EvidenceItem,
                "validation_result.evidence",
            ),
            _parse_numeric_mapping(metrics, "validation_result.metrics"),
            None,
            None,
            False,
        )

    @classmethod
    def blocked(cls, evidence, failure_class):
        return cls(
            "blocked",
            _materialize_instances(
                evidence,
                EvidenceItem,
                "validation_result.evidence",
            ),
            {},
            failure_class,
            "blocked",
            False,
        )

    @classmethod
    def failed(cls, failure_class, missing=(), failed=()):
        missing_items = _materialize_strings(missing, "validation_result.missing")
        failed_items = _materialize_strings(failed, "validation_result.failed")
        metrics = {
            "missing_checks": float(len(missing_items)),
            "failed_checks": float(len(failed_items)),
        }
        return cls("fail", (), metrics, failure_class, None, False)

    @classmethod
    def needs_independent_verifier(cls):
        return cls("needs-verifier", (), {}, None, None, True)

    def to_feedback(self):
        return {
            "status": self.status,
            "failure_class": self.failure_class.value if self.failure_class else None,
            "metrics": dict(self.metrics),
            "stop_reason": self.stop_reason,
            "evidence": [
                {"category": item.category, "passed": item.passed, "summary": item.summary[:500]}
                for item in self.evidence
            ],
        }


_ROUTE_ASSESSMENT_FIELDS = (
    "route",
    "evidence_grade",
    "quality_signal",
    "quality_basis",
    "expected_cost",
    "expected_latency",
    "residual_risk",
    "sample_size",
    "comparable_groups",
    "capabilities",
    "prior_roles",
    "provisional",
    "evidence_ids",
)


@dataclass(frozen=True)
class RouteAssessment:
    route: Route
    evidence_grade: int
    quality_signal: Optional[float]
    quality_basis: str
    expected_cost: Optional[float]
    expected_latency: Optional[float]
    residual_risk: Optional[float]
    sample_size: int
    comparable_groups: Tuple[str, ...]
    capabilities: Tuple[str, ...]
    prior_roles: Tuple[str, ...]
    provisional: bool
    evidence_ids: Tuple[str, ...]

    def __post_init__(self):
        _require_instance(self.route, Route, "route_assessment.route")
        object.__setattr__(
            self,
            "evidence_grade",
            _require_int(
                self.evidence_grade,
                "route_assessment.evidence_grade",
                minimum=0,
            ),
        )
        _require_optional_number(
            self.quality_signal,
            "route_assessment.quality_signal",
        )
        _require_string(self.quality_basis, "route_assessment.quality_basis")
        _require_optional_number(self.expected_cost, "route_assessment.expected_cost")
        _require_optional_number(
            self.expected_latency,
            "route_assessment.expected_latency",
        )
        _require_optional_number(self.residual_risk, "route_assessment.residual_risk")
        object.__setattr__(
            self,
            "sample_size",
            _require_int(
                self.sample_size,
                "route_assessment.sample_size",
                minimum=0,
            ),
        )
        _validate_string_tuple(
            self.comparable_groups,
            "route_assessment.comparable_groups",
        )
        _validate_string_tuple(self.capabilities, "route_assessment.capabilities")
        _validate_string_tuple(self.prior_roles, "route_assessment.prior_roles")
        _require_bool(self.provisional, "route_assessment.provisional")
        _validate_string_tuple(self.evidence_ids, "route_assessment.evidence_ids")

    @classmethod
    def _from_dict(cls, payload: Mapping[str, Any], path: str):
        payload = _validate_object(payload, path, _ROUTE_ASSESSMENT_FIELDS)
        return cls(
            route=Route._from_dict(payload["route"], path + ".route"),
            evidence_grade=_require_int(
                payload["evidence_grade"],
                path + ".evidence_grade",
                minimum=0,
            ),
            quality_signal=_require_optional_number(
                payload["quality_signal"],
                path + ".quality_signal",
            ),
            quality_basis=_require_string(
                payload["quality_basis"],
                path + ".quality_basis",
            ),
            expected_cost=_require_optional_number(
                payload["expected_cost"],
                path + ".expected_cost",
            ),
            expected_latency=_require_optional_number(
                payload["expected_latency"],
                path + ".expected_latency",
            ),
            residual_risk=_require_optional_number(
                payload["residual_risk"],
                path + ".residual_risk",
            ),
            sample_size=_require_int(
                payload["sample_size"],
                path + ".sample_size",
                minimum=0,
            ),
            comparable_groups=_parse_string_list(
                payload["comparable_groups"],
                path + ".comparable_groups",
            ),
            capabilities=_parse_string_list(
                payload["capabilities"],
                path + ".capabilities",
            ),
            prior_roles=_parse_string_list(
                payload["prior_roles"],
                path + ".prior_roles",
            ),
            provisional=_require_bool(
                payload["provisional"],
                path + ".provisional",
            ),
            evidence_ids=_parse_string_list(
                payload["evidence_ids"],
                path + ".evidence_ids",
            ),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]):
        return cls._from_dict(payload, "route_assessment")

    def to_dict(self):
        return {
            "route": self.route.to_dict(),
            "evidence_grade": self.evidence_grade,
            "quality_signal": self.quality_signal,
            "quality_basis": self.quality_basis,
            "expected_cost": self.expected_cost,
            "expected_latency": self.expected_latency,
            "residual_risk": self.residual_risk,
            "sample_size": self.sample_size,
            "comparable_groups": list(self.comparable_groups),
            "capabilities": list(self.capabilities),
            "prior_roles": list(self.prior_roles),
            "provisional": self.provisional,
            "evidence_ids": list(self.evidence_ids),
        }

    def meets_quality_floor(self, floor) -> bool:
        return set(floor.required_capabilities).issubset(self.capabilities)


_ELIMINATED_ROUTE_FIELDS = ("route", "reason")


@dataclass(frozen=True)
class EliminatedRoute:
    route: Route
    reason: str

    def __post_init__(self):
        _require_instance(self.route, Route, "eliminated_route.route")
        _require_string(self.reason, "eliminated_route.reason")

    @classmethod
    def _from_dict(cls, payload: Mapping[str, Any], path: str):
        payload = _validate_object(payload, path, _ELIMINATED_ROUTE_FIELDS)
        return cls(
            route=Route._from_dict(payload["route"], path + ".route"),
            reason=_require_string(payload["reason"], path + ".reason"),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]):
        return cls._from_dict(payload, "eliminated_route")

    def to_dict(self):
        return {"route": self.route.to_dict(), "reason": self.reason}


_ROUTE_DECISION_FIELDS = (
    "economic",
    "ideal",
    "maximum_safety",
    "frontier",
    "eliminated",
    "rationale",
)


@dataclass(frozen=True)
class RouteDecision:
    economic: Route
    ideal: Route
    maximum_safety: Route
    frontier: Tuple[RouteAssessment, ...]
    eliminated: Tuple[EliminatedRoute, ...]
    rationale: Dict[str, str]

    def __post_init__(self):
        _require_instance(self.economic, Route, "route_decision.economic")
        _require_instance(self.ideal, Route, "route_decision.ideal")
        _require_instance(
            self.maximum_safety,
            Route,
            "route_decision.maximum_safety",
        )
        _validate_instance_tuple(
            self.frontier,
            RouteAssessment,
            "route_decision.frontier",
            minimum_items=1,
        )
        _validate_instance_tuple(
            self.eliminated,
            EliminatedRoute,
            "route_decision.eliminated",
        )
        object.__setattr__(
            self,
            "rationale",
            _freeze_string_mapping(self.rationale, "route_decision.rationale"),
        )
        frontier_keys = {item.route.key for item in self.frontier}
        for field_name in ("economic", "ideal", "maximum_safety"):
            route = getattr(self, field_name)
            if route.key not in frontier_keys:
                _fail(
                    "route_decision.%s" % field_name,
                    "must belong to frontier",
                )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]):
        path = "route_decision"
        payload = _validate_object(payload, path, _ROUTE_DECISION_FIELDS)
        return cls(
            economic=Route._from_dict(payload["economic"], path + ".economic"),
            ideal=Route._from_dict(payload["ideal"], path + ".ideal"),
            maximum_safety=Route._from_dict(
                payload["maximum_safety"],
                path + ".maximum_safety",
            ),
            frontier=_parse_object_list(
                payload["frontier"],
                path + ".frontier",
                RouteAssessment._from_dict,
            ),
            eliminated=_parse_object_list(
                payload["eliminated"],
                path + ".eliminated",
                EliminatedRoute._from_dict,
            ),
            rationale=_parse_string_mapping(
                payload["rationale"],
                path + ".rationale",
            ),
        )

    def to_dict(self):
        return {
            "economic": self.economic.to_dict(),
            "ideal": self.ideal.to_dict(),
            "maximum_safety": self.maximum_safety.to_dict(),
            "frontier": [item.to_dict() for item in self.frontier],
            "eliminated": [item.to_dict() for item in self.eliminated],
            "rationale": dict(self.rationale),
        }

    @classmethod
    def from_assessments(
        cls, frontier, economic, ideal, maximum_safety, eliminated=(), rationale=None
    ):
        frontier_items = _materialize_instances(
            frontier,
            RouteAssessment,
            "route_decision.frontier",
        )
        if not frontier_items:
            _fail("route_decision.frontier", "must not be empty")
        economic = _require_instance(
            economic,
            RouteAssessment,
            "route_decision.economic",
        )
        ideal = _require_instance(ideal, RouteAssessment, "route_decision.ideal")
        maximum_safety = _require_instance(
            maximum_safety,
            RouteAssessment,
            "route_decision.maximum_safety",
        )
        eliminated_items = _materialize_instances(
            eliminated,
            EliminatedRoute,
            "route_decision.eliminated",
        )
        return cls(
            economic=economic.route,
            ideal=ideal.route,
            maximum_safety=maximum_safety.route,
            frontier=frontier_items,
            eliminated=eliminated_items,
            rationale={} if rationale is None else rationale,
        )
