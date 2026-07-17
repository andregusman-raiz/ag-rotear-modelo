import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Mapping as RuntimeMapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from .contracts import Effort, FailureClass, Route
from .profiles import ProfileError, load_profiles
from .portable_flock import fcntl
from .registry import (
    BenchmarkRegistry,
    RegistryError,
    load_model_catalog,
    promote_candidate,
)


class StateError(ValueError):
    pass


SAFE_OBSERVATION_FIELDS = frozenset(
    (
        "project_hash",
        "profile_id",
        "profile_version",
        "archetype",
        "route",
        "model_version",
        "engine_version",
        "duration_seconds",
        "input_tokens",
        "output_tokens",
        "observed_cost_usd",
        "validation_status",
        "metrics",
        "failure_class",
        "escalations",
        "stop_reason",
    )
)
_VALIDATION_STATUSES = ("pass", "fail", "blocked", "needs-verifier")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_DECISION_BYTES = 1024 * 1024
_DECISION_KEY_NAME = ".decision-hmac-key"
_DECISION_KEY_LOCK_NAME = ".decision-hmac-key.lock"
_DECISION_KEY_TEMP_PREFIX = ".decision-hmac-key.tmp-"
_DECISION_KEY_BYTES = 32
_DECISION_HMAC_DOMAIN = b"ag-model-router:decision:v1\x00"
_DECISION_HMAC_PLACEHOLDER = "0" * 64
_SAFE_LABEL = re.compile(r"^[a-z0-9][a-z0-9._:+-]{0,127}$")
_EVIDENCE_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TYPED_EVIDENCE_DIGEST = re.compile(r"^sha256:evidence:[0-9a-f]{64}$")
_SEMANTIC_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
_REASON_CODES = (
    "approved",
    "attempt-limit",
    "budget-exhausted",
    "cold-start",
    "completed",
    "critical-task-without-validator",
    "external-dependency",
    "external-effects-not-authorized",
    "executor-technical-failure",
    "highest-evidence-quality-safety",
    "increase-capacity-or-safety",
    "increase-depth",
    "initial-route",
    "lowest-cost-quality-floor",
    "minimum-normalized-regret",
    "next-safe-route",
    "no-progress",
    "no-untried-route-with-plausible-gain",
    "official-capability-prior",
    "official-price-prior",
    "parallel-coverage",
    "parallel-write-without-worktree",
    "partial-mutation-recovery-failed",
    "partial-mutation-recovery-invalid",
    "partial-mutation-recovery-verified",
    "quality-floor",
    "required-tools-unsupported",
    "risk-floor",
    "route-eliminated",
    "single-transient-retry",
    "structural-cold-start-prior",
    "supported-tools-not-observed",
    "time-limit",
    "ultra-without-useful-decomposition",
    "user-stopped",
    "validation-fail",
    "validation-pass",
    "validator-technical-failure",
    "verifier-budget-exhausted",
    "verifier-provenance-invalid",
    "verified-quality",
)
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_DECISION_FIELDS = (
    "schema_version",
    "fingerprint",
    "request_fingerprint",
    "profile",
    "catalog",
    "eliminated",
    "selection",
    "decision",
    "evidence_ids",
    "history",
    "validation",
    "status",
    "stop_reason",
    "budget",
    "timestamps",
    "integrity",
)
_COMPLETE_DECISION_FIELDS = tuple(
    name
    for name in _DECISION_FIELDS
    if name not in ("integrity", "request_fingerprint")
)
_TASK_DIMENSIONS = {
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
_BUDGET_FIELDS = (
    "attempts",
    "elapsed_seconds",
    "input_tokens",
    "max_attempts",
    "max_seconds",
    "observed_cost_usd",
    "output_tokens",
    "remaining_seconds",
    "remaining_tokens",
    "spent_tokens",
)
_MUTATION_IN_PROGRESS = b"mutation-in-progress\n"
_MUTATION_OUTCOME_CONFIRMED = b"outcome-confirmed\n"


@dataclass(frozen=True)
class _DecisionVocabulary:
    profile_versions: Mapping[str, str]
    model_ids: frozenset
    model_efforts: Mapping[str, frozenset]
    benchmark_ids: frozenset
    evidence_ids: frozenset
    model_registry_version: str
    benchmark_registry_version: str
    benchmark_verified_at: str


def _fail(path: str, message: str):
    raise StateError("%s %s" % (path, message))


def _string(value: Any, path: str) -> str:
    if type(value) is not str:
        _fail(path, "must be a string")
    if value.strip() == "":
        _fail(path, "must not be blank")
    if value != value.strip():
        _fail(path, "must not have leading or trailing whitespace")
    return value


def _optional_string(value: Any, path: str) -> Optional[str]:
    if value is None:
        return None
    return _string(value, path)


def _digest_open_string(value: Any, path: str, kind: str) -> str:
    parsed = _string(value, path)
    typed_prefix = "sha256:%s:" % kind
    if re.fullmatch(re.escape(typed_prefix) + r"[0-9a-f]{64}", parsed):
        return parsed
    digest = hashlib.sha256(
        ("ag-model-router:v1:%s:" % kind).encode("ascii")
        + parsed.encode("utf-8")
    ).hexdigest()
    return "sha256:%s:%s" % (kind, digest)


def _digest_open_array(value: Any, path: str, kind: str) -> list:
    items = _decision_array(value, path)
    result = []
    seen = set()
    for index, item in enumerate(items):
        digest = _digest_open_string(item, "%s[%d]" % (path, index), kind)
        if digest in seen:
            _fail("%s[%d]" % (path, index), "is a duplicate")
        seen.add(digest)
        result.append(digest)
    return result


def _decision_project_hash(value: Any, path: str) -> str:
    parsed = _string(value, path)
    typed = re.fullmatch(r"sha256:project-hash:[0-9a-f]{64}", parsed)
    if typed is not None:
        return parsed
    if _EVIDENCE_DIGEST.fullmatch(parsed) is None:
        _fail(path, "must be a SHA-256 hexadecimal digest")
    return _digest_open_string(parsed, path, "project-hash")


def _label(value: Any, path: str) -> str:
    parsed = _string(value, path)
    if _SAFE_LABEL.fullmatch(parsed) is None:
        _fail(path, "must be a bounded structural label")
    return parsed


def _optional_label(value: Any, path: str) -> Optional[str]:
    if value is None:
        return None
    return _label(value, path)


def _number(value: Any, path: str, optional: bool = False) -> Optional[float]:
    if optional and value is None:
        return None
    if type(value) not in (int, float):
        _fail(path, "must be a number")
    if not isfinite(value):
        _fail(path, "must be finite")
    if value < 0:
        _fail(path, "must be non-negative")
    return value


def _integer(value: Any, path: str) -> int:
    if type(value) is not int:
        _fail(path, "must be an integer")
    if value < 0:
        _fail(path, "must be non-negative")
    return value


def _metrics(value: Any, path: str) -> Mapping[str, float]:
    if not isinstance(value, RuntimeMapping):
        _fail(path, "must be a mapping")
    try:
        copied = dict(value)
    except Exception:
        raise StateError("%s could not be read" % path) from StateError(
            "mapping access failed"
        )
    parsed = {}
    for key, item in copied.items():
        name = _label(key, path + ".<key>")
        parsed[name] = _number(item, "%s.%s" % (path, name))
    return MappingProxyType(parsed)


def _failure_class(value: Any, path: str) -> Optional[str]:
    if value is None:
        return None
    parsed = _string(value, path)
    allowed = tuple(item.value for item in FailureClass)
    if parsed not in allowed:
        _fail(path, "must be one of: %s" % ", ".join(allowed))
    return parsed


@dataclass(frozen=True)
class Observation:
    project_path: str
    profile_id: str
    profile_version: str
    archetype: str
    route: Route
    model_version: str
    engine_version: str
    duration_seconds: float
    input_tokens: int
    output_tokens: int
    validation_status: str
    metrics: Mapping[str, float]
    failure_class: Optional[str]
    escalations: int
    stop_reason: Optional[str]
    observed_cost_usd: Optional[float] = None

    def __post_init__(self):
        _string(self.project_path, "observation.project_path")
        _label(self.profile_id, "observation.profile_id")
        _label(self.profile_version, "observation.profile_version")
        _label(self.archetype, "observation.archetype")
        if type(self.route) is not Route:
            _fail("observation.route", "must be Route")
        _label(self.model_version, "observation.model_version")
        _label(self.engine_version, "observation.engine_version")
        _number(self.duration_seconds, "observation.duration_seconds")
        _integer(self.input_tokens, "observation.input_tokens")
        _integer(self.output_tokens, "observation.output_tokens")
        _number(
            self.observed_cost_usd,
            "observation.observed_cost_usd",
            optional=True,
        )
        status = _string(self.validation_status, "observation.validation_status")
        if status not in _VALIDATION_STATUSES:
            _fail(
                "observation.validation_status",
                "must be one of: %s" % ", ".join(_VALIDATION_STATUSES),
            )
        object.__setattr__(
            self,
            "metrics",
            _metrics(self.metrics, "observation.metrics"),
        )
        object.__setattr__(
            self,
            "failure_class",
            _failure_class(self.failure_class, "observation.failure_class"),
        )
        _integer(self.escalations, "observation.escalations")
        _optional_label(self.stop_reason, "observation.stop_reason")

    def to_safe_dict(self, project_hash: str) -> Mapping[str, object]:
        safe = {
            "project_hash": _project_hash(project_hash),
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "archetype": self.archetype,
            "route": self.route.key,
            "model_version": self.model_version,
            "engine_version": self.engine_version,
            "duration_seconds": self.duration_seconds,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "observed_cost_usd": self.observed_cost_usd,
            "validation_status": self.validation_status,
            "metrics": dict(self.metrics),
            "failure_class": self.failure_class,
            "escalations": self.escalations,
            "stop_reason": self.stop_reason,
        }
        if frozenset(safe) != SAFE_OBSERVATION_FIELDS:
            raise StateError("safe observation allowlist is inconsistent")
        return MappingProxyType(safe)


@dataclass(frozen=True)
class StoredObservation:
    project_hash: str
    profile_id: str
    profile_version: str
    archetype: str
    route: Route
    model_version: str
    engine_version: str
    duration_seconds: float
    input_tokens: int
    output_tokens: int
    validation_status: str
    metrics: Mapping[str, float]
    failure_class: Optional[str]
    observed_cost_usd: Optional[float] = None
    escalations: int = 0
    stop_reason: Optional[str] = None

    def __post_init__(self):
        _project_hash(self.project_hash)
        _label(self.profile_id, "stored_observation.profile_id")
        _label(self.profile_version, "stored_observation.profile_version")
        _label(self.archetype, "stored_observation.archetype")
        if type(self.route) is not Route:
            _fail("stored_observation.route", "must be Route")
        _label(self.model_version, "stored_observation.model_version")
        _label(self.engine_version, "stored_observation.engine_version")
        _number(self.duration_seconds, "stored_observation.duration_seconds")
        _integer(self.input_tokens, "stored_observation.input_tokens")
        _integer(self.output_tokens, "stored_observation.output_tokens")
        _number(
            self.observed_cost_usd,
            "stored_observation.observed_cost_usd",
            optional=True,
        )
        if self.validation_status not in _VALIDATION_STATUSES:
            _fail("stored_observation.validation_status", "is invalid")
        object.__setattr__(
            self,
            "metrics",
            _metrics(self.metrics, "stored_observation.metrics"),
        )
        object.__setattr__(
            self,
            "failure_class",
            _failure_class(
                self.failure_class,
                "stored_observation.failure_class",
            ),
        )
        _integer(self.escalations, "stored_observation.escalations")
        _optional_label(self.stop_reason, "stored_observation.stop_reason")


def _project_hash(value: Any) -> str:
    parsed = _string(value, "project_hash")
    if len(parsed) != 64 or any(character not in "0123456789abcdef" for character in parsed):
        _fail("project_hash", "must be a SHA-256 hexadecimal digest")
    return parsed


def _route_from_key(value: Any) -> Route:
    key = _string(value, "stored_observation.route")
    parts = key.split(":")
    if len(parts) != 3:
        _fail("stored_observation.route", "must contain model, effort and topology")
    model, effort_value, topology = parts
    try:
        effort = Effort(effort_value)
    except (TypeError, ValueError):
        _fail("stored_observation.route", "contains an invalid effort")
    route = Route(model, effort)
    if topology != route.topology.value:
        _fail("stored_observation.route", "contains an inconsistent topology")
    return route


def stored_observation_from_safe_dict(payload: Any) -> StoredObservation:
    if type(payload) is not dict:
        _fail("stored_observation", "must be an object")
    keys = frozenset(payload)
    if keys != SAFE_OBSERVATION_FIELDS:
        missing = SAFE_OBSERVATION_FIELDS.difference(keys)
        extra = keys.difference(SAFE_OBSERVATION_FIELDS)
        if missing:
            _fail("stored_observation.%s" % sorted(missing)[0], "is required")
        _fail("stored_observation.%s" % sorted(extra)[0], "is not allowed")
    return StoredObservation(
        project_hash=_project_hash(payload["project_hash"]),
        profile_id=_label(payload["profile_id"], "stored_observation.profile_id"),
        profile_version=_label(
            payload["profile_version"],
            "stored_observation.profile_version",
        ),
        archetype=_label(payload["archetype"], "stored_observation.archetype"),
        route=_route_from_key(payload["route"]),
        model_version=_label(
            payload["model_version"],
            "stored_observation.model_version",
        ),
        engine_version=_label(
            payload["engine_version"],
            "stored_observation.engine_version",
        ),
        duration_seconds=_number(
            payload["duration_seconds"],
            "stored_observation.duration_seconds",
        ),
        input_tokens=_integer(
            payload["input_tokens"],
            "stored_observation.input_tokens",
        ),
        output_tokens=_integer(
            payload["output_tokens"],
            "stored_observation.output_tokens",
        ),
        observed_cost_usd=_number(
            payload["observed_cost_usd"],
            "stored_observation.observed_cost_usd",
            optional=True,
        ),
        validation_status=_string(
            payload["validation_status"],
            "stored_observation.validation_status",
        ),
        metrics=_metrics(payload["metrics"], "stored_observation.metrics"),
        failure_class=_failure_class(
            payload["failure_class"],
            "stored_observation.failure_class",
        ),
        escalations=_integer(
            payload["escalations"],
            "stored_observation.escalations",
        ),
        stop_reason=_optional_label(
            payload["stop_reason"],
            "stored_observation.stop_reason",
        ),
    )


class _DuplicateJsonKeyError(ValueError):
    pass


def _strict_object(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateJsonKeyError(key)
        payload[key] = value
    return payload


def _loads_line(document: str) -> Any:
    try:
        return json.loads(
            document,
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise StateError("telemetry contains an invalid JSON record") from None


def _registry_schema_version(path: Path) -> str:
    try:
        if path.is_symlink():
            raise StateError("model registry snapshot must not be a symlink")
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except StateError:
        raise
    except (json.JSONDecodeError, UnicodeError, ValueError, OSError):
        raise StateError("model registry metadata is invalid") from None
    if type(payload) is not dict or type(payload.get("schema_version")) is not str:
        raise StateError("model registry metadata is invalid")
    return _string(payload["schema_version"], "model_registry.schema_version")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _fsync_file(descriptor: int) -> None:
    os.fsync(descriptor)


def _unlink_if_present(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    descriptor = -1
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".new-",
            dir=str(path.parent),
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _decision_object(
    value: Any,
    path: str,
    required: Tuple[str, ...] = (),
    allowed: Tuple[str, ...] = (),
) -> Mapping[str, Any]:
    if not isinstance(value, RuntimeMapping):
        _fail(path, "must be an object")
    try:
        payload = dict(value)
    except Exception:
        raise StateError("decision payload could not be read") from None
    permitted = frozenset(allowed or required)
    for name in required:
        if name not in payload:
            _fail("%s.%s" % (path, name), "is required")
    for name in payload:
        if type(name) is not str or name not in permitted:
            rendered = name if type(name) is str else "<key>"
            _fail("%s.%s" % (path, rendered), "is not allowed")
    return payload


def _decision_array(value: Any, path: str, maximum: int = 256) -> list:
    if type(value) not in (list, tuple):
        _fail(path, "must be an array")
    if len(value) > maximum:
        _fail(path, "contains too many items")
    return list(value)


def _known_ids(
    value: Any,
    path: str,
    known_ids: frozenset,
    kind: str,
) -> list:
    items = _decision_array(value, path)
    parsed = []
    seen = set()
    for index, item in enumerate(items):
        item_path = "%s[%d]" % (path, index)
        identifier = _string(item, item_path)
        if identifier not in known_ids:
            _fail(item_path, "must reference a registered %s" % kind)
        if identifier in seen:
            _fail("%s[%d]" % (path, index), "is a duplicate")
        seen.add(identifier)
        parsed.append(identifier)
    return parsed


def _known_id(
    value: Any,
    path: str,
    known_ids: frozenset,
    kind: str,
) -> str:
    identifier = _string(value, path)
    if identifier not in known_ids:
        _fail(path, "must reference a registered %s" % kind)
    return identifier


def _evidence_ids(
    value: Any,
    path: str,
    vocabulary: _DecisionVocabulary,
) -> list:
    result = []
    seen = set()
    for index, item in enumerate(_decision_array(value, path)):
        item_path = "%s[%d]" % (path, index)
        evidence_id = _string(item, item_path)
        if evidence_id in vocabulary.evidence_ids:
            safe_id = evidence_id
        elif _EVIDENCE_DIGEST.fullmatch(evidence_id) is not None:
            safe_id = "sha256:evidence:%s" % evidence_id
        elif _TYPED_EVIDENCE_DIGEST.fullmatch(evidence_id) is not None:
            safe_id = evidence_id
        else:
            _fail(
                item_path,
                "must reference registry evidence or a SHA-256 digest",
            )
        if safe_id in seen:
            _fail(item_path, "is a duplicate")
        seen.add(safe_id)
        result.append(safe_id)
    return result


def _decision_choice(value: Any, path: str, allowed: Tuple[str, ...]) -> str:
    parsed = _string(value, path)
    if parsed not in allowed:
        _fail(path, "contains an unsupported structural value")
    return parsed


def _reason_code(value: Any, path: str, optional: bool = False) -> Optional[str]:
    if optional and value is None:
        return None
    return _decision_choice(value, path, _REASON_CODES)


def _decision_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _registered_effort(
    model: str,
    effort: Effort,
    path: str,
    vocabulary: _DecisionVocabulary,
) -> None:
    if effort not in vocabulary.model_efforts[model]:
        _fail(path, "is not supported by the registered model")


def _decision_route(
    value: Any,
    path: str,
    vocabulary: _DecisionVocabulary,
) -> Any:
    if type(value) is str:
        key = _string(value, path)
        if key == "route":
            return key
        parts = key.split(":")
        if len(parts) != 3:
            _fail(path, "must be a structural route key")
        model, effort_value, topology = parts
        _known_id(
            model,
            path + ".model",
            vocabulary.model_ids,
            "model id",
        )
        try:
            effort = Effort(effort_value)
            route = Route(model, effort)
        except (TypeError, ValueError):
            _fail(path, "must be a structural route key")
        _registered_effort(model, effort, path + ".effort", vocabulary)
        if route.topology.value != topology:
            _fail(path, "contains an inconsistent topology")
        return key
    payload = _decision_object(
        value,
        path,
        required=("model", "effort"),
        allowed=("model", "effort", "topology"),
    )
    model = _known_id(
        payload["model"],
        path + ".model",
        vocabulary.model_ids,
        "model id",
    )
    effort_value = _decision_choice(
        payload["effort"],
        path + ".effort",
        tuple(item.value for item in Effort),
    )
    effort = Effort(effort_value)
    _registered_effort(model, effort, path + ".effort", vocabulary)
    route = Route(model, effort)
    result = {"model": model, "effort": effort_value}
    if "topology" in payload:
        topology = _decision_choice(
            payload["topology"],
            path + ".topology",
            ("single", "multi"),
        )
        if topology != route.topology.value:
            _fail(path + ".topology", "is inconsistent with effort")
        result["topology"] = topology
    return result


def _decision_route_list(
    value: Any,
    path: str,
    vocabulary: _DecisionVocabulary,
) -> list:
    return [
        _decision_route(item, "%s[%d]" % (path, index), vocabulary)
        for index, item in enumerate(_decision_array(value, path))
    ]


def _reason_codes(value: Any, path: str) -> Any:
    if type(value) in (list, tuple):
        items = _decision_array(value, path)
        return [
            _reason_code(item, "%s[%d]" % (path, index))
            for index, item in enumerate(items)
        ]
    payload = _decision_object(value, path, allowed=(
        "economic",
        "ideal",
        "maximum_safety",
        "selected",
        "route",
    ))
    return {
        name: _reason_code(item, "%s.%s" % (path, name))
        for name, item in payload.items()
    }


def _selection(
    value: Any,
    path: str,
    vocabulary: _DecisionVocabulary,
) -> Mapping[str, Any]:
    fields = (
        "economic",
        "ideal",
        "maximum_safety",
        "selected",
        "route",
        "frontier",
        "rationale_codes",
        "blocked",
    )
    payload = _decision_object(value, path, allowed=fields)
    if not payload:
        _fail(path, "must not be empty")
    result = {}
    for name, item in payload.items():
        item_path = "%s.%s" % (path, name)
        if name == "blocked":
            result[name] = _reason_code(item, item_path)
        elif name == "frontier":
            result[name] = _decision_route_list(item, item_path, vocabulary)
        elif name == "rationale_codes":
            result[name] = _reason_codes(item, item_path)
        else:
            result[name] = _decision_route(item, item_path, vocabulary)
    return result


def _profile(
    value: Any,
    path: str,
    vocabulary: _DecisionVocabulary,
) -> Mapping[str, Any]:
    payload = _decision_object(
        value,
        path,
        required=("id", "version"),
    )
    profile_id = _known_id(
        payload["id"],
        path + ".id",
        frozenset(vocabulary.profile_versions),
        "profile id",
    )
    version = _string(payload["version"], path + ".version")
    if version != vocabulary.profile_versions[profile_id]:
        _fail(path + ".version", "must match the registered profile version")
    return {"id": profile_id, "version": version}


def _validation_checks(value: Any, path: str) -> list:
    parsed = []
    for index, item in enumerate(_decision_array(value, path)):
        item_path = "%s[%d]" % (path, index)
        payload = _decision_object(
            item,
            item_path,
            required=("id", "category", "required"),
        )
        parsed.append(
            {
                "id": _digest_open_string(
                    payload["id"],
                    item_path + ".id",
                    "validation-check-id",
                ),
                "category": _digest_open_string(
                    payload["category"],
                    item_path + ".category",
                    "validation-check-category",
                ),
                "required": _decision_bool(
                    payload["required"], item_path + ".required"
                ),
            }
        )
    return parsed


def _fingerprint(
    value: Any,
    path: str,
    vocabulary: _DecisionVocabulary,
) -> Mapping[str, Any]:
    fields = (
        "project_hash",
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
        "validation_checks",
    )
    payload = _decision_object(value, path, allowed=fields)
    result = {}
    for name, item in payload.items():
        item_path = "%s.%s" % (path, name)
        if name == "project_hash":
            result[name] = _decision_project_hash(item, item_path)
        elif name in _TASK_DIMENSIONS:
            result[name] = _decision_choice(
                item,
                item_path,
                _TASK_DIMENSIONS[name],
            )
        elif name in (
            "external_effects",
            "external_effects_authorized",
            "parallel_writes",
            "worktree_isolated",
        ):
            result[name] = _decision_bool(item, item_path)
        elif name == "independent_fronts":
            result[name] = _integer(item, item_path)
            if result[name] < 1:
                _fail(item_path, "must be at least 1")
        elif name == "primary_profile":
            result[name] = _known_id(
                item,
                item_path,
                frozenset(vocabulary.profile_versions),
                "profile id",
            )
        elif name == "secondary_profiles":
            result[name] = _known_ids(
                item,
                item_path,
                frozenset(vocabulary.profile_versions),
                "profile id",
            )
        elif name == "required_tools":
            result[name] = _digest_open_array(
                item,
                item_path,
                "required-tool",
            )
        elif name == "validation_checks":
            result[name] = _validation_checks(item, item_path)
        elif name == "archetype":
            result[name] = _digest_open_string(item, item_path, "archetype")
        else:
            _fail(item_path, "is not a supported fingerprint field")
    return result


def _version_reference(
    value: Any,
    path: str,
    registered_version: str,
    kind: str,
) -> str:
    parsed = _string(value, path)
    if (
        parsed == registered_version
        and _SEMANTIC_VERSION.fullmatch(parsed) is not None
    ):
        return parsed
    return _digest_open_string(parsed, path, kind)


def _date(value: Any, path: str) -> str:
    parsed = _string(value, path)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", parsed) is None:
        _fail(path, "must be an ISO 8601 calendar date")
    try:
        datetime.strptime(parsed, "%Y-%m-%d")
    except ValueError:
        _fail(path, "must be an ISO 8601 calendar date")
    return parsed


def _catalog(
    value: Any,
    path: str,
    vocabulary: _DecisionVocabulary,
) -> Mapping[str, Any]:
    fields = (
        "source",
        "schema_version",
        "model_registry_version",
        "benchmark_registry_version",
        "verified_at",
        "model_ids",
        "benchmark_ids",
        "evidence_ids",
    )
    payload = _decision_object(value, path, allowed=fields)
    result = {}
    for name, item in payload.items():
        item_path = "%s.%s" % (path, name)
        if name == "source":
            result[name] = _decision_choice(
                item,
                item_path,
                ("live", "cache", "bundled", "seed"),
            )
        elif name == "evidence_ids":
            result[name] = _evidence_ids(item, item_path, vocabulary)
        elif name == "model_ids":
            result[name] = _known_ids(
                item,
                item_path,
                vocabulary.model_ids,
                "model id",
            )
        elif name == "benchmark_ids":
            result[name] = _known_ids(
                item,
                item_path,
                vocabulary.benchmark_ids,
                "benchmark id",
            )
        elif name == "verified_at":
            verified_at = _date(item, item_path)
            if verified_at != vocabulary.benchmark_verified_at:
                _fail(item_path, "must match the benchmark registry")
            result[name] = verified_at
        elif name == "schema_version":
            result[name] = _decision_choice(item, item_path, ("1.0.0",))
        elif name == "model_registry_version":
            result[name] = _version_reference(
                item,
                item_path,
                vocabulary.model_registry_version,
                "model-registry-version",
            )
        elif name == "benchmark_registry_version":
            result[name] = _version_reference(
                item,
                item_path,
                vocabulary.benchmark_registry_version,
                "benchmark-registry-version",
            )
        else:
            _fail(item_path, "is not a supported catalog field")
    return result


def _eliminated(
    value: Any,
    path: str,
    vocabulary: _DecisionVocabulary,
) -> list:
    result = []
    for index, item in enumerate(_decision_array(value, path)):
        item_path = "%s[%d]" % (path, index)
        payload = _decision_object(
            item,
            item_path,
            required=("route", "reason_code"),
        )
        result.append(
            {
                "route": _decision_route(
                    payload["route"],
                    item_path + ".route",
                    vocabulary,
                ),
                "reason_code": _reason_code(
                    payload["reason_code"], item_path + ".reason_code"
                ),
            }
        )
    return result


def _hashed_numeric_map(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, RuntimeMapping):
        _fail(path, "must be an object")
    try:
        items = tuple(value.items())
    except Exception:
        raise StateError("decision payload could not be read") from None
    if len(items) > 128:
        _fail(path, "contains too many fields")
    result = {}
    for index, (name, item) in enumerate(items):
        digest = _digest_open_string(name, path + ".<key>", "metric-name")
        if digest in result:
            _fail("%s[%d]" % (path, index), "is a duplicate metric")
        result[digest] = _number(item, "%s[%d]" % (path, index))
    return result


def _budget(value: Any, path: str) -> Mapping[str, Any]:
    payload = _decision_object(value, path, allowed=_BUDGET_FIELDS)
    return {
        name: _number(item, "%s.%s" % (path, name))
        for name, item in payload.items()
    }


def _validation(
    value: Any,
    path: str,
    vocabulary: _DecisionVocabulary,
) -> Mapping[str, Any]:
    fields = (
        "status",
        "failure_class",
        "stop_reason",
        "required_checks_passed",
        "metrics",
        "evidence_ids",
        "requires_independent_verifier",
    )
    payload = _decision_object(value, path, allowed=fields)
    result = {}
    for name, item in payload.items():
        item_path = "%s.%s" % (path, name)
        if name == "status":
            result[name] = _decision_choice(
                item,
                item_path,
                _VALIDATION_STATUSES,
            )
        elif name == "failure_class":
            result[name] = _failure_class(item, item_path)
        elif name == "stop_reason":
            result[name] = _reason_code(item, item_path, optional=True)
        elif name == "required_checks_passed":
            result[name] = _integer(item, item_path)
        elif name == "metrics":
            result[name] = _hashed_numeric_map(item, item_path)
        elif name == "evidence_ids":
            result[name] = _evidence_ids(item, item_path, vocabulary)
        else:
            result[name] = _decision_bool(item, item_path)
    return result


def _history(
    value: Any,
    path: str,
    vocabulary: _DecisionVocabulary,
) -> list:
    fields = (
        "route",
        "validation_status",
        "failure_class",
        "reason_code",
        "evidence_ids",
        "escalation",
    )
    result = []
    for index, item in enumerate(_decision_array(value, path)):
        item_path = "%s[%d]" % (path, index)
        if type(item) is str:
            result.append(_decision_route(item, item_path, vocabulary))
            continue
        payload = _decision_object(
            item,
            item_path,
            required=("route",),
            allowed=fields,
        )
        parsed = {
            "route": _decision_route(
                payload["route"],
                item_path + ".route",
                vocabulary,
            )
        }
        for name, nested in payload.items():
            nested_path = "%s.%s" % (item_path, name)
            if name == "route":
                continue
            if name == "validation_status":
                parsed[name] = _decision_choice(
                    nested,
                    nested_path,
                    _VALIDATION_STATUSES,
                )
            elif name == "failure_class":
                parsed[name] = _failure_class(nested, nested_path)
            elif name == "evidence_ids":
                parsed[name] = _evidence_ids(
                    nested,
                    nested_path,
                    vocabulary,
                )
            elif name == "escalation":
                parsed[name] = _integer(nested, nested_path)
            else:
                parsed[name] = _reason_code(nested, nested_path)
        result.append(parsed)
    return result


def _timestamp(value: Any, path: str) -> str:
    parsed = _string(value, path)
    if _RFC3339.fullmatch(parsed) is None:
        _fail(path, "must be an RFC 3339 timestamp")
    try:
        datetime.fromisoformat(parsed.replace("Z", "+00:00"))
    except ValueError:
        _fail(path, "must be an RFC 3339 timestamp")
    return parsed


def _timestamps(value: Any, path: str) -> Mapping[str, str]:
    fields = ("started_at", "updated_at", "finished_at", "verified_at")
    payload = _decision_object(value, path, allowed=fields)
    return {
        name: _timestamp(item, "%s.%s" % (path, name))
        for name, item in payload.items()
    }


def _validate_decision_envelope(
    value: Any,
    vocabulary: _DecisionVocabulary,
    require_complete: bool = False,
) -> Mapping[str, Any]:
    payload = _decision_object(
        value,
        "decision",
        required=(
            _COMPLETE_DECISION_FIELDS
            if require_complete
            else ("schema_version", "selection")
        ),
        allowed=_DECISION_FIELDS,
    )
    if payload["schema_version"] != "1.0.0":
        _fail("decision.schema_version", "must equal 1.0.0")
    if "fingerprint" in payload and "request_fingerprint" in payload:
        _fail("decision.request_fingerprint", "duplicates fingerprint")
    result = {"schema_version": "1.0.0"}
    for name, item in payload.items():
        if name == "schema_version":
            continue
        path = "decision.%s" % name
        if name in ("fingerprint", "request_fingerprint"):
            result[name] = _fingerprint(item, path, vocabulary)
        elif name == "profile":
            result[name] = _profile(item, path, vocabulary)
        elif name == "catalog":
            result[name] = _catalog(item, path, vocabulary)
        elif name == "eliminated":
            result[name] = _eliminated(item, path, vocabulary)
        elif name in ("selection", "decision"):
            result[name] = _selection(item, path, vocabulary)
        elif name == "evidence_ids":
            result[name] = _evidence_ids(item, path, vocabulary)
        elif name == "history":
            result[name] = _history(item, path, vocabulary)
        elif name == "validation":
            result[name] = _validation(item, path, vocabulary)
        elif name == "status":
            result[name] = _decision_choice(
                item,
                path,
                ("running", "completed", "pass", "fail", "blocked", "stopped"),
            )
        elif name == "stop_reason":
            result[name] = _reason_code(item, path, optional=True)
        elif name == "budget":
            result[name] = _budget(item, path)
        elif name == "timestamps":
            result[name] = _timestamps(item, path)
        elif name == "integrity":
            _fail(path, "is managed by the runtime")
    if require_complete:
        _validate_terminal_decision(result)
    return result


def _validate_terminal_decision(payload: Mapping[str, Any]) -> None:
    validation = payload["validation"]
    required_validation = (
        "status",
        "failure_class",
        "stop_reason",
        "required_checks_passed",
        "metrics",
        "evidence_ids",
        "requires_independent_verifier",
    )
    if tuple(name for name in required_validation if name not in validation):
        _fail("decision.validation", "is incomplete")
    for name in ("attempts", "elapsed_seconds", "input_tokens", "output_tokens"):
        if name not in payload["budget"]:
            _fail("decision.budget", "is incomplete")
    for name in ("started_at", "finished_at"):
        if name not in payload["timestamps"]:
            _fail("decision.timestamps", "is incomplete")
    status = payload["status"]
    stop_reason = payload["stop_reason"]
    if status not in ("pass", "blocked", "stopped"):
        _fail("decision.status", "must be terminal")
    if validation["stop_reason"] != stop_reason:
        _fail("decision.validation.stop_reason", "must match terminal reason")
    blocked_selection = payload["selection"].get("blocked")
    if blocked_selection is not None:
        if (
            payload["selection"] != {"blocked": stop_reason}
            or payload["decision"] != {"blocked": stop_reason}
            or status != "blocked"
            or payload["history"]
        ):
            _fail("decision.selection.blocked", "is incoherent")
    else:
        for name in (
            "economic",
            "ideal",
            "maximum_safety",
            "frontier",
            "rationale_codes",
        ):
            if name not in payload["selection"]:
                _fail("decision.selection", "is incomplete")
        if "selected" not in payload["decision"]:
            _fail("decision.decision", "is incomplete")
    if status == "pass":
        if (
            stop_reason != "approved"
            or validation["status"] != "pass"
            or validation["failure_class"] is not None
        ):
            _fail("decision.status", "is incoherent")
    elif validation["status"] == "pass" or stop_reason == "approved":
        _fail("decision.status", "is incoherent")
    elif status == "stopped" and (
        stop_reason != "user-stopped" or validation["status"] != "blocked"
    ):
        _fail("decision.status", "is incoherent")
    elif status == "blocked" and validation["status"] not in ("fail", "blocked"):
        _fail("decision.status", "is incoherent")


def _decision_hmac_marker(tag: str) -> bytes:
    return ('"tag": "%s"' % tag).encode("ascii")


def _signed_decision_document(
    payload: Mapping[str, Any],
    key: bytes,
) -> bytes:
    persisted = dict(payload)
    persisted["integrity"] = {
        "algorithm": "hmac-sha256",
        "tag": _DECISION_HMAC_PLACEHOLDER,
    }
    unsigned = (
        json.dumps(
            persisted,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    marker = _decision_hmac_marker(_DECISION_HMAC_PLACEHOLDER)
    if unsigned.count(marker) != 1:
        raise StateError("decision integrity could not be encoded")
    tag = hmac.new(
        key,
        _DECISION_HMAC_DOMAIN + unsigned,
        hashlib.sha256,
    ).hexdigest()
    return unsigned.replace(marker, _decision_hmac_marker(tag), 1)


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _regular_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _read_limited_descriptor(descriptor: int, limit: int) -> bytes:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise StateError("runtime file must be regular")
    if metadata.st_size > limit:
        raise StateError("runtime file exceeds size limit")
    chunks = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    document = b"".join(chunks)
    if len(document) > limit:
        raise StateError("runtime file exceeds size limit")
    return document


def _validate_decision_key_descriptor(
    descriptor: int,
    *,
    repair_mode: bool,
) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise StateError("decision integrity key must be a regular file")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise StateError("decision integrity key owner is invalid")
    mode = stat.S_IMODE(metadata.st_mode)
    if not repair_mode and (mode & 0o077) == 0:
        return
    if not repair_mode:
        raise StateError("decision integrity key permissions are invalid")
    if mode == 0o600:
        return
    os.fchmod(descriptor, 0o600)
    _fsync_file(descriptor)


def _cleanup_decision_key_temporaries(root_descriptor: int) -> None:
    removed = False
    for name in os.listdir(root_descriptor):
        if not name.startswith(_DECISION_KEY_TEMP_PREFIX):
            continue
        try:
            os.unlink(name, dir_fd=root_descriptor)
            removed = True
        except FileNotFoundError:
            continue
    if removed:
        os.fsync(root_descriptor)


def _prepare_decision_file(path: Path, payload: bytes) -> Path:
    descriptor = -1
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".new-",
            dir=str(path.parent),
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        _fsync_file(descriptor)
        os.close(descriptor)
        descriptor = -1
        return temporary
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        _unlink_if_present(temporary)
        raise


def _rollback_decision_commit(
    path: Path,
    run_dir: Path,
    runs_dir: Path,
) -> bool:
    try:
        _unlink_if_present(path)
        _fsync_directory(run_dir)
        run_dir.rmdir()
        _fsync_directory(runs_dir)
        return not os.path.lexists(str(run_dir))
    except Exception:
        return False


def _acquire_runtime_lock(path: Path) -> int:
    descriptor = -1
    try:
        if path.is_symlink():
            raise StateError("runtime mutation lock must not be a symlink")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(path), flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise StateError("runtime mutation lock must be a regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except StateError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        if error.errno in (errno.EACCES, errno.EAGAIN):
            raise StateError("runtime mutation is busy") from None
        raise StateError("runtime mutation lock could not be acquired") from None


def _release_runtime_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _create_runtime_witness(path: Path) -> None:
    _atomic_write_bytes(path, _MUTATION_IN_PROGRESS)


def _clear_runtime_witness(path: Path) -> bool:
    try:
        _unlink_if_present(path)
        _fsync_directory(path.parent)
        return not os.path.lexists(str(path))
    except Exception:
        return False


def _mark_runtime_witness_confirmed(path: Path) -> None:
    _atomic_write_bytes(path, _MUTATION_OUTCOME_CONFIRMED)


def _runtime_witness_is_confirmed(path: Path) -> bool:
    descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(path), flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False
        return os.read(descriptor, 128) == _MUTATION_OUTCOME_CONFIRMED
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _finalize_runtime_witness(path: Path) -> bool:
    try:
        _mark_runtime_witness_confirmed(path)
        return True
    except Exception:
        return False


class RuntimeState:
    def __init__(self, root: Path, seed_root: Path):
        try:
            self.root = Path(root)
            self.seed_root = Path(seed_root)
        except Exception:
            raise StateError("runtime paths are invalid") from None
        if self.root.is_symlink():
            raise StateError("runtime root must not be a symlink")
        self.registry_dir = self.root / "registry"
        self.runs_dir = self.root / "runs"
        self.telemetry_dir = self.root / "telemetry"
        self.model_registry_path = self.registry_dir / "model-registry.json"
        self.benchmark_registry_path = self.registry_dir / "benchmark-registry.json"
        self.observations_path = self.telemetry_dir / "observations.jsonl"
        self.salt_path = self.root / ".project-hash-salt"
        self.decision_key_path = self.root / _DECISION_KEY_NAME
        self.decision_key_lock_path = self.root / _DECISION_KEY_LOCK_NAME
        self.salt_lock_path = self.root / ".project-hash-salt.lock"
        self.mutation_lock_path = self.root / ".runtime-mutation.lock"
        self.mutation_witness_path = self.root / ".mutation-in-progress"
        self.unknown_outcome_path = self.root / ".commit-outcome-unknown"
        self._mutation_blocked = False
        self._root_identity = None

    def _reconcile_runtime_witness(self) -> None:
        if not os.path.lexists(str(self.mutation_witness_path)):
            return
        if _runtime_witness_is_confirmed(self.mutation_witness_path):
            return
        self._record_unknown_outcome()
        raise StateError(
            "commit-outcome-unknown blocks runtime mutations"
        )

    def _ensure_mutations_allowed(self) -> None:
        if self._mutation_blocked or os.path.lexists(
            str(self.unknown_outcome_path)
        ):
            self._mutation_blocked = True
            raise StateError(
                "commit-outcome-unknown blocks runtime mutations"
            )
        if not os.path.lexists(str(self.mutation_witness_path)):
            return
        descriptor = _acquire_runtime_lock(self.mutation_lock_path)
        try:
            if os.path.lexists(str(self.unknown_outcome_path)):
                self._mutation_blocked = True
            else:
                self._reconcile_runtime_witness()
            if self._mutation_blocked:
                raise StateError(
                    "commit-outcome-unknown blocks runtime mutations"
                )
        finally:
            _release_runtime_lock(descriptor)

    def _record_unknown_outcome(self) -> None:
        self._mutation_blocked = True
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(str(self.unknown_outcome_path), flags, 0o600)
            try:
                _write_all(descriptor, b"commit-outcome-unknown\n")
                _fsync_file(descriptor)
            finally:
                os.close(descriptor)
            try:
                _fsync_directory(self.root)
            except Exception:
                pass
        except Exception:
            pass

    def bootstrap(self) -> None:
        self._ensure_mutations_allowed()
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            for directory in (
                self.registry_dir,
                self.runs_dir,
                self.telemetry_dir,
            ):
                if directory.is_symlink():
                    raise StateError("runtime directory must not be a symlink")
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(str(directory), 0o700)
            if self.model_registry_path.is_symlink():
                raise StateError("model registry snapshot must not be a symlink")
            if self.benchmark_registry_path.is_symlink():
                raise StateError("benchmark registry snapshot must not be a symlink")
            if not self.model_registry_path.exists():
                model_seed = self.seed_root / "model-registry.json"
                if model_seed.is_symlink():
                    raise StateError("model registry seed must not be a symlink")
                _atomic_write_bytes(
                    self.model_registry_path,
                    model_seed.read_bytes(),
                )
            if not self.benchmark_registry_path.exists():
                promote_candidate(
                    self.seed_root / "benchmark-registry.json",
                    self.benchmark_registry_path,
                )
            BenchmarkRegistry.load(self.benchmark_registry_path)
            self._ensure_salt()
            self._ensure_decision_key()
            descriptor = self._open_bound_root()
            os.close(descriptor)
        except (StateError, RegistryError):
            raise
        except Exception:
            raise StateError("runtime state could not be bootstrapped") from None

    def _ensure_salt(self) -> bytes:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.salt_path.is_symlink() or self.salt_lock_path.is_symlink():
            raise StateError("project hash salt files must not be symlinks")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(
            str(self.salt_lock_path),
            flags,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if not self.salt_path.exists():
                _atomic_write_bytes(self.salt_path, secrets.token_bytes(32))
            salt = self.salt_path.read_bytes()
            if len(salt) != 32:
                raise StateError("project hash salt is invalid")
            os.chmod(str(self.salt_path), 0o600)
            return salt
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _ensure_decision_key(self) -> bytes:
        root_descriptor = -1
        lock_descriptor = -1
        key_descriptor = -1
        temporary_name = None
        try:
            root_descriptor = self._open_bound_root()
            lock_flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                lock_flags |= os.O_NOFOLLOW
            lock_descriptor = os.open(
                _DECISION_KEY_LOCK_NAME,
                lock_flags,
                0o600,
                dir_fd=root_descriptor,
            )
            lock_metadata = os.fstat(lock_descriptor)
            if not stat.S_ISREG(lock_metadata.st_mode):
                raise StateError(
                    "decision integrity key lock must be a regular file"
                )
            if hasattr(os, "getuid") and lock_metadata.st_uid != os.getuid():
                raise StateError("decision integrity key lock owner is invalid")
            os.fchmod(lock_descriptor, 0o600)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            _cleanup_decision_key_temporaries(root_descriptor)

            try:
                key_descriptor = os.open(
                    _DECISION_KEY_NAME,
                    _regular_open_flags(),
                    dir_fd=root_descriptor,
                )
            except FileNotFoundError:
                key = secrets.token_bytes(_DECISION_KEY_BYTES)
                temporary_name = (
                    _DECISION_KEY_TEMP_PREFIX + secrets.token_hex(16)
                )
                create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    create_flags |= os.O_NOFOLLOW
                key_descriptor = os.open(
                    temporary_name,
                    create_flags,
                    0o600,
                    dir_fd=root_descriptor,
                )
                try:
                    _validate_decision_key_descriptor(
                        key_descriptor,
                        repair_mode=True,
                    )
                    _write_all(key_descriptor, key)
                    _fsync_file(key_descriptor)
                finally:
                    os.close(key_descriptor)
                    key_descriptor = -1

                key_descriptor = os.open(
                    temporary_name,
                    _regular_open_flags(),
                    dir_fd=root_descriptor,
                )
                _validate_decision_key_descriptor(
                    key_descriptor,
                    repair_mode=False,
                )
                staged_key = _read_limited_descriptor(
                    key_descriptor,
                    _DECISION_KEY_BYTES,
                )
                if (
                    len(staged_key) != _DECISION_KEY_BYTES
                    or not hmac.compare_digest(staged_key, key)
                ):
                    raise StateError("decision integrity key is invalid")
                os.close(key_descriptor)
                key_descriptor = -1

                try:
                    os.link(
                        temporary_name,
                        _DECISION_KEY_NAME,
                        src_dir_fd=root_descriptor,
                        dst_dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                    os.fsync(root_descriptor)
                except FileExistsError:
                    pass
                os.unlink(temporary_name, dir_fd=root_descriptor)
                temporary_name = None
                os.fsync(root_descriptor)
                key_descriptor = os.open(
                    _DECISION_KEY_NAME,
                    _regular_open_flags(),
                    dir_fd=root_descriptor,
                )

            _validate_decision_key_descriptor(
                key_descriptor,
                repair_mode=True,
            )
            key = _read_limited_descriptor(
                key_descriptor,
                _DECISION_KEY_BYTES,
            )
            if len(key) != _DECISION_KEY_BYTES:
                raise StateError("decision integrity key is invalid")
            os.fsync(root_descriptor)
            return key
        except StateError:
            raise
        except Exception:
            raise StateError("decision integrity key could not be read") from None
        finally:
            if key_descriptor >= 0:
                os.close(key_descriptor)
            if temporary_name is not None and root_descriptor >= 0:
                try:
                    os.unlink(temporary_name, dir_fd=root_descriptor)
                    os.fsync(root_descriptor)
                except OSError:
                    pass
            if lock_descriptor >= 0:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(lock_descriptor)
            if root_descriptor >= 0:
                os.close(root_descriptor)

    def _open_bound_root(self) -> int:
        descriptor = -1
        try:
            descriptor = os.open(str(self.root), _directory_open_flags())
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise StateError("runtime root must be a directory")
            identity = (metadata.st_dev, metadata.st_ino)
            if self._root_identity is None:
                self._root_identity = identity
            elif self._root_identity != identity:
                raise StateError("runtime root identity changed")
            return descriptor
        except StateError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            raise StateError("runtime root could not be opened safely") from None

    def project_hash(self, project_path: str) -> str:
        raw = _string(project_path, "project_path")
        try:
            canonical = str(Path(raw).resolve(strict=False)).encode("utf-8")
            salt = self._ensure_salt()
        except StateError:
            raise
        except Exception:
            raise StateError("project path could not be hashed") from None
        return hmac.new(salt, canonical, hashlib.sha256).hexdigest()

    def append_observation(self, observation: Observation) -> None:
        self._ensure_mutations_allowed()
        if type(observation) is not Observation:
            _fail("observation", "must be Observation")
        self.telemetry_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.telemetry_dir.is_symlink() or self.observations_path.is_symlink():
            raise StateError("telemetry paths must not be symlinks")
        safe = observation.to_safe_dict(
            project_hash=self.project_hash(observation.project_path)
        )
        try:
            line = (
                json.dumps(
                    dict(safe),
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(
                str(self.observations_path),
                flags,
                0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                offset = 0
                while offset < len(line):
                    written = os.write(descriptor, line[offset:])
                    if written <= 0:
                        raise OSError("short telemetry write")
                    offset += written
                os.fsync(descriptor)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        except Exception:
            raise StateError("telemetry observation could not be written") from None

    def _decision_vocabulary(
        self,
        registry: BenchmarkRegistry,
    ) -> _DecisionVocabulary:
        try:
            profiles = load_profiles(self.seed_root / "profiles")
            catalog = load_model_catalog(self.model_registry_path)
            model_registry_version = _registry_schema_version(
                self.model_registry_path
            )
            registry_ids = frozenset(
                [source.id for source in registry.sources]
                + [observation.id for observation in registry.observations]
            )
            model_efforts = MappingProxyType(
                {
                    model.slug: frozenset(model.supported_efforts)
                    for model in catalog.models
                }
            )
            return _DecisionVocabulary(
                profile_versions=MappingProxyType(
                    {
                        profile_id: profile.version
                        for profile_id, profile in profiles.items()
                    }
                ),
                model_ids=frozenset(model_efforts),
                model_efforts=model_efforts,
                benchmark_ids=registry_ids,
                evidence_ids=registry_ids,
                model_registry_version=model_registry_version,
                benchmark_registry_version=registry.schema_version,
                benchmark_verified_at=registry.verified_at,
            )
        except (ProfileError, RegistryError, StateError):
            raise StateError("decision vocabulary could not be loaded") from None
        except Exception:
            raise StateError("decision vocabulary could not be loaded") from None

    def write_decision(
        self,
        run_id: str,
        payload: Mapping[str, object],
    ) -> Path:
        self._ensure_mutations_allowed()
        identifier = _string(run_id, "run_id")
        if identifier in (".", "..") or _RUN_ID.fullmatch(identifier) is None:
            _fail("run_id", "must be a safe identifier")
        registry = self.benchmark_registry()
        vocabulary = self._decision_vocabulary(registry)
        safe = _validate_decision_envelope(
            payload,
            vocabulary,
            require_complete=True,
        )
        key = self._ensure_decision_key()
        self.runs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.runs_dir.is_symlink():
            raise StateError("runs directory must not be a symlink")
        run_dir = self.runs_dir / identifier
        path = run_dir / "decision.json"
        lock_descriptor = -1
        temporary = None
        run_created = False
        outcome_unknown = False
        witness_created = False
        try:
            lock_descriptor = _acquire_runtime_lock(self.mutation_lock_path)
            if self._mutation_blocked or os.path.lexists(
                str(self.unknown_outcome_path)
            ):
                self._mutation_blocked = True
                raise StateError(
                    "commit-outcome-unknown blocks runtime mutations"
                )
            if os.path.lexists(str(self.mutation_witness_path)):
                self._reconcile_runtime_witness()
            try:
                _create_runtime_witness(self.mutation_witness_path)
                witness_created = True
            except Exception:
                self._mutation_blocked = True
                self._record_unknown_outcome()
                raise StateError(
                    "decision mutation preflight could not be confirmed"
                ) from None
            run_dir.mkdir(mode=0o700, exist_ok=False)
            run_created = True
            _fsync_directory(self.runs_dir)
            document = _signed_decision_document(safe, key)
            if len(document) > MAX_DECISION_BYTES:
                raise StateError("decision exceeds size limit")
            temporary = _prepare_decision_file(path, document)
            os.replace(str(temporary), str(path))
            temporary = None
            try:
                _fsync_directory(run_dir)
            except Exception:
                if _rollback_decision_commit(path, run_dir, self.runs_dir):
                    run_created = False
                    raise StateError(
                        "decision commit was rolled back"
                    ) from None
                outcome_unknown = True
                self._record_unknown_outcome()
                raise StateError(
                    "commit-outcome-unknown blocks runtime mutations"
                ) from None
            run_created = False
            return path
        except FileExistsError:
            raise StateError("run_id already exists") from None
        except StateError:
            raise
        except Exception:
            raise StateError("decision could not be written") from None
        finally:
            finalization_unknown = False
            try:
                if not outcome_unknown:
                    _unlink_if_present(temporary)
                    if run_created:
                        _unlink_if_present(path)
                        try:
                            run_dir.rmdir()
                        except OSError:
                            pass
                        try:
                            _fsync_directory(self.runs_dir)
                        except Exception:
                            pass
                    if witness_created and not _finalize_runtime_witness(
                        self.mutation_witness_path
                    ):
                        self._mutation_blocked = True
                        self._record_unknown_outcome()
                        finalization_unknown = True
            finally:
                if lock_descriptor >= 0:
                    _release_runtime_lock(lock_descriptor)
            if finalization_unknown:
                raise StateError(
                    "commit-outcome-unknown blocks runtime mutations"
                ) from None

    def read_decision(self, run_id: str) -> Mapping[str, Any]:
        identifier = _string(run_id, "run_id")
        if identifier in (".", "..") or _RUN_ID.fullmatch(identifier) is None:
            _fail("run_id", "must be a safe identifier")
        descriptors = []
        try:
            root_descriptor = self._open_bound_root()
            descriptors.append(root_descriptor)
            runs_descriptor = os.open(
                "runs",
                _directory_open_flags(),
                dir_fd=root_descriptor,
            )
            descriptors.append(runs_descriptor)
            run_descriptor = os.open(
                identifier,
                _directory_open_flags(),
                dir_fd=runs_descriptor,
            )
            descriptors.append(run_descriptor)
            decision_descriptor = os.open(
                "decision.json",
                _regular_open_flags(),
                dir_fd=run_descriptor,
            )
            descriptors.append(decision_descriptor)
            document_bytes = _read_limited_descriptor(
                decision_descriptor,
                MAX_DECISION_BYTES,
            )
            key_descriptor = os.open(
                _DECISION_KEY_NAME,
                _regular_open_flags(),
                dir_fd=root_descriptor,
            )
            descriptors.append(key_descriptor)
            _validate_decision_key_descriptor(
                key_descriptor,
                repair_mode=False,
            )
            key = _read_limited_descriptor(
                key_descriptor,
                _DECISION_KEY_BYTES,
            )
            if len(key) != _DECISION_KEY_BYTES:
                raise StateError("decision integrity key is invalid")
            document = document_bytes.decode("utf-8")
            payload = json.loads(
                document,
                object_pairs_hook=_strict_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(payload, RuntimeMapping):
                raise StateError("decision integrity is invalid")
            integrity = payload.get("integrity")
            if (
                not isinstance(integrity, RuntimeMapping)
                or set(integrity) != {"algorithm", "tag"}
                or integrity.get("algorithm") != "hmac-sha256"
                or type(integrity.get("tag")) is not str
                or _EVIDENCE_DIGEST.fullmatch(integrity["tag"]) is None
            ):
                raise StateError("decision integrity is invalid")
            marker = _decision_hmac_marker(integrity["tag"])
            if document_bytes.count(marker) != 1:
                raise StateError("decision integrity is invalid")
            unsigned = document_bytes.replace(
                marker,
                _decision_hmac_marker(_DECISION_HMAC_PLACEHOLDER),
                1,
            )
            expected = hmac.new(
                key,
                _DECISION_HMAC_DOMAIN + unsigned,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, integrity["tag"]):
                raise StateError("decision integrity is invalid")
            payload = dict(payload)
            del payload["integrity"]
        except StateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            raise StateError("decision could not be read") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        registry = self.benchmark_registry()
        vocabulary = self._decision_vocabulary(registry)
        safe = _validate_decision_envelope(
            payload,
            vocabulary,
            require_complete=True,
        )
        descriptor = self._open_bound_root()
        os.close(descriptor)
        return safe

    def benchmark_registry(self) -> BenchmarkRegistry:
        return BenchmarkRegistry.load(self.benchmark_registry_path)

    def local_observations(
        self,
        project_path: str,
    ) -> Tuple[StoredObservation, ...]:
        if not self.observations_path.exists():
            return ()
        if self.observations_path.is_symlink():
            raise StateError("telemetry path must not be a symlink")
        project_hash = self.project_hash(project_path)
        try:
            document = self.observations_path.read_bytes()
            complete = document.endswith(b"\n")
            raw_lines = document.split(b"\n")
            if complete:
                raw_lines = raw_lines[:-1]
            elif raw_lines:
                final = raw_lines[-1]
                try:
                    final_text = final.decode("utf-8")
                    _loads_line(final_text)
                except (UnicodeError, StateError):
                    raw_lines = raw_lines[:-1]
            parsed = []
            for raw_line in raw_lines:
                if raw_line == b"":
                    continue
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeError:
                    raise StateError("telemetry contains invalid UTF-8") from None
                stored = stored_observation_from_safe_dict(_loads_line(line))
                if stored.project_hash == project_hash:
                    parsed.append(stored)
            return tuple(parsed)
        except StateError:
            raise
        except Exception:
            raise StateError("telemetry observations could not be read") from None
