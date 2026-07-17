import errno
import json
import os
import re
import stat
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from math import isfinite
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .contracts import Effort, Route
from .model_registry import CatalogError, CatalogSnapshot, catalog_from_json
from .portable_flock import fcntl


class RegistryError(ValueError):
    pass


_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SOURCE_STATUSES = ("active", "quarantine", "retired")
_SOURCE_KINDS = ("vendor", "independent-aggregator", "primary-independent")
_TOPOLOGIES = ("single", "multi", "unspecified")
_METRIC_UNITS = {
    "arc-agi-1-public-eval": "percent",
    "arc-agi-2-public-eval": "percent",
    "arc-agi-3-public-demo": "percent",
    "task-completed-correctly": "percent",
    "artificial-analysis-intelligence-index": "index-points",
}
_KIND_PRECEDENCE = {
    "vendor": 1,
    "independent-aggregator": 2,
    "primary-independent": 3,
}
_MUTATION_IN_PROGRESS = b"mutation-in-progress\n"
_MUTATION_OUTCOME_CONFIRMED = b"outcome-confirmed\n"
_MUTATION_OUTCOME_UNKNOWN = b"commit-outcome-unknown\n"
_REFERENCES = Path(__file__).resolve().parents[2] / "references"
_DEFAULT_SCHEMA = _REFERENCES / "benchmark-registry-schema.json"
_DEFAULT_MODEL_REGISTRY = _REFERENCES / "model-registry.json"


def _fail(path: str, message: str):
    raise RegistryError("%s %s" % (path, message))


def _object(
    value: Any,
    path: str,
    required: Tuple[str, ...],
    optional: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    if type(value) is not dict:
        _fail(path, "must be an object")
    allowed = frozenset(required + optional)
    for name in required:
        if name not in value:
            _fail("%s.%s" % (path, name), "is required")
    for name in value:
        if type(name) is not str:
            _fail(path + ".<key>", "must be a string")
        if name not in allowed:
            _fail("%s.%s" % (path, name), "is not allowed")
    return dict(value)


def _string(value: Any, path: str) -> str:
    if type(value) is not str:
        _fail(path, "must be a string")
    if value.strip() == "":
        _fail(path, "must not be blank")
    if value != value.strip():
        _fail(path, "must not have leading or trailing whitespace")
    return value


def _identifier(value: Any, path: str) -> str:
    parsed = _string(value, path)
    if _ID.fullmatch(parsed) is None:
        _fail(path, "must be a lowercase hyphenated identifier")
    return parsed


def _model_id(value: Any, path: str) -> str:
    parsed = _string(value, path)
    if _MODEL_ID.fullmatch(parsed) is None:
        _fail(path, "must be a model identifier")
    return parsed


def _choice(value: Any, path: str, allowed: Tuple[str, ...]) -> str:
    parsed = _string(value, path)
    if parsed not in allowed:
        _fail(path, "must be one of: %s" % ", ".join(allowed))
    return parsed


def _number(value: Any, path: str, minimum: float = 0.0):
    if type(value) not in (int, float):
        _fail(path, "must be a number")
    if not isfinite(value):
        _fail(path, "must be finite")
    if value < minimum:
        _fail(path, "must be at least %s" % minimum)
    return value


def _boolean(value: Any, path: str) -> bool:
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _date(value: Any, path: str) -> str:
    parsed = _string(value, path)
    try:
        normalized = date.fromisoformat(parsed)
    except ValueError:
        _fail(path, "must be an ISO 8601 calendar date")
    if normalized.isoformat() != parsed:
        _fail(path, "must be an ISO 8601 calendar date")
    return parsed


def _optional_date(value: Any, path: str) -> Optional[str]:
    if value is None:
        return None
    return _date(value, path)


def _string_array(
    value: Any,
    path: str,
    minimum_items: int = 1,
) -> Tuple[str, ...]:
    if type(value) is not list:
        _fail(path, "must be an array")
    if len(value) < minimum_items:
        _fail(path, "must contain at least %d item(s)" % minimum_items)
    items = []
    seen = set()
    for index, item in enumerate(value):
        parsed = _string(item, "%s[%d]" % (path, index))
        if parsed in seen:
            _fail("%s[%d]" % (path, index), "is a duplicate")
        seen.add(parsed)
        items.append(parsed)
    return tuple(items)


def _url(value: Any, path: str) -> str:
    parsed = _string(value, path)
    if any(character.isspace() for character in parsed):
        _fail(path, "must not contain whitespace")
    split = urlsplit(parsed)
    if split.scheme != "https" or not split.netloc or split.hostname is None:
        _fail(path, "must be an absolute HTTPS URL")
    if split.username is not None or split.password is not None:
        _fail(path, "must not contain credentials")
    return parsed


def _effort(value: Any, path: str) -> Effort:
    parsed = _string(value, path)
    try:
        return Effort(parsed)
    except (TypeError, ValueError):
        _fail(path, "must be a supported effort")


@dataclass(frozen=True)
class BenchmarkSource:
    id: str
    owner: str
    url: str
    status: str
    kind: str
    limitations: Tuple[str, ...]

    def __post_init__(self):
        _identifier(self.id, "source.id")
        _string(self.owner, "source.owner")
        _url(self.url, "source.url")
        _choice(self.status, "source.status", _SOURCE_STATUSES)
        _choice(self.kind, "source.kind", _SOURCE_KINDS)
        object.__setattr__(
            self,
            "limitations",
            _copy_string_tuple(self.limitations, "source.limitations", 1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "owner": self.owner,
            "url": self.url,
            "status": self.status,
            "kind": self.kind,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class ScalarMeasurement:
    name: str
    value: float
    unit: str

    def __post_init__(self):
        name = _choice(
            self.name,
            "measurement.name",
            tuple(_METRIC_UNITS) + ("cost-per-task",),
        )
        expected_unit = (
            "USD/task" if name == "cost-per-task" else _METRIC_UNITS[name]
        )
        if _string(self.unit, "measurement.unit") != expected_unit:
            _fail(
                "measurement.unit",
                "must equal %s for %s" % (expected_unit, name),
            )
        value = _number(self.value, "measurement.value")
        if self.unit == "percent" and value > 100:
            _fail("measurement.value", "must not exceed 100 percent")

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "value": self.value, "unit": self.unit}


def _copy_string_tuple(
    value: Any,
    path: str,
    minimum_items: int = 0,
) -> Tuple[str, ...]:
    if type(value) in (str, bytes) or isinstance(value, Mapping):
        _fail(path, "must be an iterable of strings")
    try:
        items = tuple(value)
    except Exception:
        raise RegistryError("%s could not be read" % path) from RegistryError(
            "iterable access failed"
        )
    if len(items) < minimum_items:
        _fail(path, "must contain at least %d item(s)" % minimum_items)
    seen = set()
    for index, item in enumerate(items):
        parsed = _string(item, "%s[%d]" % (path, index))
        if parsed in seen:
            _fail("%s[%d]" % (path, index), "is a duplicate")
        seen.add(parsed)
    return items


@dataclass(frozen=True)
class BenchmarkObservation:
    id: str
    source_id: str
    benchmark: str
    dataset: str
    harness: str
    route: Route
    topology: str
    profile_tags: Tuple[str, ...]
    metric: ScalarMeasurement
    cost: Optional[ScalarMeasurement]
    evaluated_at: Optional[str]
    topology_declared: bool = True

    def __post_init__(self):
        _identifier(self.id, "observation.id")
        _identifier(self.source_id, "observation.source_id")
        _string(self.benchmark, "observation.benchmark")
        _string(self.dataset, "observation.dataset")
        _string(self.harness, "observation.harness")
        if type(self.route) is not Route:
            _fail("observation.route", "must be Route")
        _model_id(self.route.model, "observation.route.model")
        _choice(self.topology, "observation.topology", _TOPOLOGIES)
        _boolean(self.topology_declared, "observation.topology_declared")
        if self.topology_declared and self.topology == "unspecified":
            _fail(
                "observation.topology",
                "must be declared when topology_declared is true",
            )
        if not self.topology_declared and self.topology != "unspecified":
            _fail(
                "observation.topology",
                "must be unspecified when topology_declared is false",
            )
        if self.route.effort is Effort.ULTRA and self.topology != "multi":
            _fail("observation.topology", "must be multi for ultra effort")
        if self.topology == "multi" and self.route.effort is not Effort.ULTRA:
            _fail("observation.effort", "must be ultra for multi topology")
        object.__setattr__(
            self,
            "profile_tags",
            _copy_string_tuple(self.profile_tags, "observation.profile_tags", 1),
        )
        if type(self.metric) is not ScalarMeasurement:
            _fail("observation.metric", "must be ScalarMeasurement")
        if self.cost is not None:
            if type(self.cost) is not ScalarMeasurement:
                _fail("observation.cost", "must be ScalarMeasurement or null")
            if self.cost.name != "cost-per-task":
                _fail("observation.cost.name", "must equal cost-per-task")
        _optional_date(self.evaluated_at, "observation.evaluated_at")

    @property
    def semantic_key(self) -> Tuple[str, ...]:
        return (
            self.source_id,
            self.benchmark,
            self.dataset,
            self.harness,
            self.route.model,
            self.route.effort.value,
            self.topology,
            self.metric.name,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "id": self.id,
            "source_id": self.source_id,
            "benchmark": self.benchmark,
            "dataset": self.dataset,
            "harness": self.harness,
            "model": self.route.model,
            "effort": self.route.effort.value,
            "topology": self.topology,
            "topology_declared": self.topology_declared,
            "profile_tags": list(self.profile_tags),
            "metric": self.metric.to_dict(),
            "cost": None if self.cost is None else self.cost.to_dict(),
        }
        if self.evaluated_at is not None:
            payload["evaluated_at"] = self.evaluated_at
        else:
            payload["evaluated_at"] = None
        return payload


@dataclass(frozen=True)
class BenchmarkRegistry:
    schema_version: str
    verified_at: str
    sources: Tuple[BenchmarkSource, ...]
    observations: Tuple[BenchmarkObservation, ...]

    def __post_init__(self):
        if _string(self.schema_version, "registry.schema_version") != "1.0.0":
            _fail("registry.schema_version", "must equal 1.0.0")
        _date(self.verified_at, "registry.verified_at")
        sources = _copy_instances(
            self.sources,
            BenchmarkSource,
            "registry.sources",
            minimum_items=1,
        )
        observations = _copy_instances(
            self.observations,
            BenchmarkObservation,
            "registry.observations",
        )
        _reject_duplicate_attribute(sources, "id", "registry.sources")
        _reject_duplicate_attribute(observations, "id", "registry.observations")
        semantic = set()
        for index, observation in enumerate(observations):
            if observation.semantic_key in semantic:
                _fail(
                    "registry.observations[%d]" % index,
                    "is a duplicate observation",
                )
            semantic.add(observation.semantic_key)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "observations", observations)
        self.validate_references()

    @classmethod
    def load(
        cls,
        path: Path,
        model_registry_path: Optional[Path] = None,
    ) -> "BenchmarkRegistry":
        try:
            candidate = Path(path)
        except Exception:
            raise RegistryError("benchmark registry path is invalid") from None
        try:
            if candidate.is_symlink():
                raise RegistryError("benchmark registry path must not be a symlink")
            document = candidate.read_text(encoding="utf-8")
        except RegistryError:
            raise
        except Exception:
            raise RegistryError("benchmark registry could not be read") from None
        return validate_registry_document(
            document,
            schema_path=_schema_path_for(candidate),
            model_registry_path=_model_registry_path_for(
                candidate,
                model_registry_path,
            ),
        )

    def validate_references(self) -> None:
        source_ids = {source.id for source in self.sources}
        for index, observation in enumerate(self.observations):
            if observation.source_id not in source_ids:
                _fail(
                    "registry.observations[%d].source_id" % index,
                    "references an unknown source",
                )

    def validate_model_catalog(self, catalog: CatalogSnapshot) -> None:
        if type(catalog) is not CatalogSnapshot:
            _fail("model_registry", "must be a CatalogSnapshot")
        models = {model.slug: model for model in catalog.models}
        sources = {source.id: source for source in self.sources}
        for index, observation in enumerate(self.observations):
            if sources[observation.source_id].status != "active":
                continue
            model = models.get(observation.route.model)
            if model is None:
                _fail(
                    "registry.observations[%d].model" % index,
                    "is not present in the model registry",
                )
            if observation.route.effort not in model.supported_efforts:
                _fail(
                    "registry.observations[%d].effort" % index,
                    "is not supported by the model registry",
                )

    def source(self, source_id: str) -> BenchmarkSource:
        requested = _identifier(source_id, "source_id")
        for source in self.sources:
            if source.id == requested:
                return source
        _fail("source_id", "references an unknown source")

    def active_observations(
        self,
        profile_tags: Tuple[str, ...],
    ) -> Tuple[BenchmarkObservation, ...]:
        tags = frozenset(
            _copy_string_tuple(profile_tags, "profile_tags", minimum_items=1)
        )
        sources = {source.id: source for source in self.sources}
        active = tuple(
            observation
            for observation in self.observations
            if sources[observation.source_id].status == "active"
            and tags.intersection(observation.profile_tags)
        )
        return tuple(
            sorted(
                active,
                key=lambda item: (
                    -_KIND_PRECEDENCE[sources[item.source_id].kind],
                    item.source_id,
                    item.id,
                ),
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verified_at": self.verified_at,
            "sources": [source.to_dict() for source in self.sources],
            "observations": [item.to_dict() for item in self.observations],
        }


def _copy_instances(
    value: Any,
    expected_type,
    path: str,
    minimum_items: int = 0,
) -> tuple:
    if type(value) in (str, bytes) or isinstance(value, Mapping):
        _fail(path, "must be an iterable")
    try:
        items = tuple(value)
    except Exception:
        raise RegistryError("%s could not be read" % path) from RegistryError(
            "iterable access failed"
        )
    if len(items) < minimum_items:
        _fail(path, "must contain at least %d item(s)" % minimum_items)
    for index, item in enumerate(items):
        if type(item) is not expected_type:
            _fail("%s[%d]" % (path, index), "has an invalid type")
    return items


def _reject_duplicate_attribute(items: tuple, attribute: str, path: str) -> None:
    seen = set()
    for index, item in enumerate(items):
        value = getattr(item, attribute)
        if value in seen:
            _fail("%s[%d].%s" % (path, index, attribute), "is a duplicate")
        seen.add(value)


class _DuplicateJsonKeyError(ValueError):
    pass


def _strict_object(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateJsonKeyError(key)
        payload[key] = value
    return payload


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _loads_json(document: Any) -> Any:
    if type(document) is not str:
        raise RegistryError("benchmark registry must be text")
    try:
        return json.loads(
            document,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except _DuplicateJsonKeyError:
        raise RegistryError("benchmark registry contains duplicate JSON keys") from None
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise RegistryError("benchmark registry contains invalid JSON") from None


def _schema_path_for(registry_path: Path) -> Path:
    sibling = registry_path.parent / "benchmark-registry-schema.json"
    return sibling if sibling.is_file() else _DEFAULT_SCHEMA


def _model_registry_path_for(
    registry_path: Path,
    explicit: Optional[Path],
) -> Path:
    if explicit is not None:
        try:
            return Path(explicit)
        except Exception:
            raise RegistryError("model registry path is invalid") from None
    sibling = registry_path.parent / "model-registry.json"
    return sibling if sibling.is_file() else _DEFAULT_MODEL_REGISTRY


def _read_validation_file(path: Path, label: str) -> str:
    try:
        candidate = Path(path)
        if candidate.is_symlink():
            raise RegistryError("%s must not be a symlink" % label)
        return candidate.read_text(encoding="utf-8")
    except RegistryError:
        raise
    except Exception:
        raise RegistryError("%s could not be read" % label) from None


def load_model_catalog(path: Path) -> CatalogSnapshot:
    document = _read_validation_file(path, "model registry")
    payload = _loads_json(document)
    try:
        return catalog_from_json(payload, source="seed")
    except CatalogError:
        raise RegistryError("model registry is invalid") from None


def _schema_for_catalog(
    schema: Mapping[str, Any],
    catalog: CatalogSnapshot,
    active_source_ids: Tuple[str, ...],
) -> Dict[str, Any]:
    dynamic = deepcopy(schema)
    try:
        observation = dynamic["$defs"]["observation"]
        model_slugs = [model.slug for model in catalog.models]
        effort_rules = [
            {
                "if": {
                    "properties": {
                        "source_id": {"enum": list(active_source_ids)}
                    },
                    "required": ["source_id"],
                },
                "then": {"properties": {"model": {"enum": model_slugs}}},
            }
        ]
        for model in catalog.models:
            effort_rules.append(
                {
                    "if": {
                        "properties": {
                            "source_id": {"enum": list(active_source_ids)},
                            "model": {"const": model.slug},
                        },
                        "required": ["source_id", "model"],
                    },
                    "then": {
                        "properties": {
                            "effort": {
                                "enum": [
                                    effort.value
                                    for effort in model.supported_efforts
                                ]
                            }
                        }
                    },
                }
            )
        observation.setdefault("allOf", []).extend(effort_rules)
    except (AttributeError, KeyError, TypeError):
        raise RegistryError("benchmark registry schema is incomplete") from None
    return dynamic


def _active_source_ids(payload: Any) -> Tuple[str, ...]:
    if type(payload) is not dict or type(payload.get("sources")) is not list:
        return ()
    return tuple(
        source["id"]
        for source in payload["sources"]
        if type(source) is dict
        and source.get("status") == "active"
        and type(source.get("id")) is str
    )


def validate_registry_document(
    document: str,
    schema_path: Optional[Path] = None,
    model_registry_path: Optional[Path] = None,
    require_schema: bool = False,
) -> BenchmarkRegistry:
    """Validate the operational stdlib contract and optional Draft dev gate."""

    payload = _loads_json(document)
    catalog = load_model_catalog(
        _DEFAULT_MODEL_REGISTRY
        if model_registry_path is None
        else Path(model_registry_path)
    )
    if type(require_schema) is not bool:
        raise RegistryError("require_schema must be a boolean")
    if require_schema:
        schema_document = _read_validation_file(
            _DEFAULT_SCHEMA if schema_path is None else Path(schema_path),
            "benchmark registry schema",
        )
        schema = _loads_json(schema_document)
        dynamic_schema = _schema_for_catalog(
            schema,
            catalog,
            _active_source_ids(payload),
        )
        try:
            from jsonschema import Draft202012Validator, FormatChecker
            from jsonschema.exceptions import SchemaError
        except ImportError:
            raise RegistryError(
                "Draft 2020-12 validator is unavailable for the dev schema gate"
            ) from None
        try:
            Draft202012Validator.check_schema(dynamic_schema)
            validator = Draft202012Validator(
                dynamic_schema,
                format_checker=FormatChecker(),
            )
            if next(validator.iter_errors(payload), None) is not None:
                raise RegistryError(
                    "benchmark registry failed Draft 2020-12 validation"
                )
        except RegistryError:
            raise
        except SchemaError:
            raise RegistryError("benchmark registry schema is invalid") from None
    registry = _registry_from_payload(payload)
    registry.validate_model_catalog(catalog)
    return registry


def _source_from_payload(value: Any, path: str) -> BenchmarkSource:
    payload = _object(
        value,
        path,
        ("id", "owner", "url", "status", "kind", "limitations"),
    )
    return BenchmarkSource(
        id=_identifier(payload["id"], path + ".id"),
        owner=_string(payload["owner"], path + ".owner"),
        url=_url(payload["url"], path + ".url"),
        status=_choice(payload["status"], path + ".status", _SOURCE_STATUSES),
        kind=_choice(payload["kind"], path + ".kind", _SOURCE_KINDS),
        limitations=_string_array(payload["limitations"], path + ".limitations"),
    )


def _measurement_from_payload(
    value: Any,
    path: str,
    cost: bool = False,
) -> ScalarMeasurement:
    payload = _object(value, path, ("name", "value", "unit"))
    name = _string(payload["name"], path + ".name")
    allowed = ("cost-per-task",) if cost else tuple(_METRIC_UNITS)
    name = _choice(name, path + ".name", allowed)
    unit = _string(payload["unit"], path + ".unit")
    expected = "USD/task" if cost else _METRIC_UNITS[name]
    if unit != expected:
        _fail(path + ".unit", "must equal %s" % expected)
    return ScalarMeasurement(
        name=name,
        value=_number(payload["value"], path + ".value"),
        unit=unit,
    )


def _observation_from_payload(value: Any, path: str) -> BenchmarkObservation:
    payload = _object(
        value,
        path,
        (
            "id",
            "source_id",
            "benchmark",
            "dataset",
            "harness",
            "model",
            "effort",
            "topology",
            "topology_declared",
            "profile_tags",
            "metric",
            "cost",
        ),
        ("evaluated_at",),
    )
    model = _model_id(payload["model"], path + ".model")
    effort = _effort(payload["effort"], path + ".effort")
    cost_value = payload["cost"]
    return BenchmarkObservation(
        id=_identifier(payload["id"], path + ".id"),
        source_id=_identifier(payload["source_id"], path + ".source_id"),
        benchmark=_string(payload["benchmark"], path + ".benchmark"),
        dataset=_string(payload["dataset"], path + ".dataset"),
        harness=_string(payload["harness"], path + ".harness"),
        route=Route(model, effort),
        topology=_choice(payload["topology"], path + ".topology", _TOPOLOGIES),
        topology_declared=_boolean(
            payload["topology_declared"],
            path + ".topology_declared",
        ),
        profile_tags=_string_array(payload["profile_tags"], path + ".profile_tags"),
        metric=_measurement_from_payload(payload["metric"], path + ".metric"),
        cost=(
            None
            if cost_value is None
            else _measurement_from_payload(cost_value, path + ".cost", cost=True)
        ),
        evaluated_at=_optional_date(
            payload.get("evaluated_at"),
            path + ".evaluated_at",
        ),
    )


def _registry_from_payload(value: Any) -> BenchmarkRegistry:
    payload = _object(
        value,
        "registry",
        ("schema_version", "verified_at", "sources", "observations"),
    )
    if type(payload["sources"]) is not list:
        _fail("registry.sources", "must be an array")
    if type(payload["observations"]) is not list:
        _fail("registry.observations", "must be an array")
    return BenchmarkRegistry(
        schema_version=_string(payload["schema_version"], "registry.schema_version"),
        verified_at=_date(payload["verified_at"], "registry.verified_at"),
        sources=tuple(
            _source_from_payload(item, "registry.sources[%d]" % index)
            for index, item in enumerate(payload["sources"])
        ),
        observations=tuple(
            _observation_from_payload(item, "registry.observations[%d]" % index)
            for index, item in enumerate(payload["observations"])
        ),
    )


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


def _mutation_guard_path(destination: Path) -> Path:
    return destination.with_name(
        destination.name + ".commit-outcome-unknown"
    )


def _mutation_lock_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".mutation.lock")


def _acquire_mutation_lock(path: Path) -> int:
    descriptor = -1
    try:
        if path.is_symlink():
            raise RegistryError("benchmark mutation lock must not be a symlink")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(path), flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RegistryError(
                "benchmark mutation lock must be a regular file"
            )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except RegistryError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        if error.errno in (errno.EACCES, errno.EAGAIN):
            raise RegistryError("benchmark mutation is busy") from None
        raise RegistryError(
            "benchmark mutation lock could not be acquired"
        ) from None


def _release_mutation_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _create_mutation_guard(path: Path) -> None:
    _replace_mutation_guard_phase(path, _MUTATION_IN_PROGRESS)


def _mark_commit_outcome_unknown(path: Path) -> bool:
    try:
        _replace_mutation_guard_phase(path, _MUTATION_OUTCOME_UNKNOWN)
        return True
    except Exception:
        return False


def _create_same_directory_backup(destination: Path) -> Path:
    descriptor, placeholder_name = tempfile.mkstemp(
        prefix=destination.name + ".backup-",
        dir=str(destination.parent),
    )
    os.close(descriptor)
    placeholder = Path(placeholder_name)
    placeholder.unlink()
    os.link(str(destination), str(placeholder))
    return placeholder


def _prepare_temporary(destination: Path, payload: bytes) -> Path:
    descriptor = -1
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=destination.name + ".new-",
            dir=str(destination.parent),
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


def _replace_mutation_guard_phase(path: Path, payload: bytes) -> None:
    temporary = None
    try:
        temporary = _prepare_temporary(
            path,
            payload,
        )
        os.replace(str(temporary), str(path))
        temporary = None
        _fsync_directory(path.parent)
    finally:
        _unlink_if_present(temporary)


def _mark_mutation_guard_confirmed(path: Path) -> None:
    _replace_mutation_guard_phase(path, _MUTATION_OUTCOME_CONFIRMED)


def _clear_mutation_guard(path: Path) -> bool:
    try:
        _unlink_if_present(path)
        _fsync_directory(path.parent)
        return not os.path.lexists(str(path))
    except Exception:
        return False


def _mutation_guard_is_confirmed(path: Path) -> bool:
    try:
        return path.read_bytes() == _MUTATION_OUTCOME_CONFIRMED
    except OSError:
        return False


def _finalize_mutation_guard(path: Path) -> bool:
    try:
        _mark_mutation_guard_confirmed(path)
        return True
    except Exception:
        return False


def _rollback_registry_commit(
    destination: Path,
    backup: Optional[Path],
    previous: Optional[bytes],
) -> Tuple[bool, Optional[Path]]:
    try:
        if previous is None:
            _unlink_if_present(destination)
        else:
            if backup is None:
                return False, backup
            os.replace(str(backup), str(destination))
            backup = None
        _fsync_directory(destination.parent)
        if previous is None:
            confirmed = not os.path.lexists(str(destination))
        else:
            confirmed = (
                destination.is_file()
                and not destination.is_symlink()
                and destination.read_bytes() == previous
            )
        return confirmed, backup
    except Exception:
        return False, backup


def promote_candidate(candidate_path: Path, destination_path: Path) -> None:
    try:
        destination = Path(destination_path)
    except Exception:
        raise RegistryError("benchmark destination path is invalid") from None
    guard = _mutation_guard_path(destination)
    lock = _mutation_lock_path(destination)
    lock_descriptor = -1
    temporary = None
    backup = None
    preserve_backup = False
    guard_created = False
    outcome_unknown = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (
            destination.parent.is_symlink()
            or destination.is_symlink()
            or guard.is_symlink()
            or lock.is_symlink()
        ):
            raise RegistryError("benchmark destination must not be a symlink")
        lock_descriptor = _acquire_mutation_lock(lock)
        if guard.exists():
            if not _mutation_guard_is_confirmed(guard):
                raise RegistryError(
                    "commit-outcome-unknown blocks benchmark promotion"
                )
        model_registry = destination.parent / "model-registry.json"
        candidate = BenchmarkRegistry.load(
            candidate_path,
            model_registry_path=(model_registry if model_registry.is_file() else None),
        )
        payload = (
            json.dumps(
                candidate.to_dict(),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

        try:
            _create_mutation_guard(guard)
            guard_created = True
        except Exception:
            raise RegistryError(
                "benchmark promotion preflight could not be confirmed"
            ) from None

        previous = None
        if os.path.lexists(str(destination)):
            if destination.is_symlink() or not destination.is_file():
                raise RegistryError("benchmark destination must be a regular file")
            previous = destination.read_bytes()
            backup = _create_same_directory_backup(destination)
            _fsync_directory(destination.parent)

        temporary = _prepare_temporary(destination, payload)
        os.replace(str(temporary), str(destination))
        temporary = None
        try:
            _fsync_directory(destination.parent)
        except Exception:
            rolled_back, backup = _rollback_registry_commit(
                destination,
                backup,
                previous,
            )
            if rolled_back:
                raise RegistryError(
                    "benchmark promotion commit was rolled back"
                ) from None
            preserve_backup = backup is not None
            outcome_unknown = True
            _mark_commit_outcome_unknown(guard)
            raise RegistryError(
                "commit-outcome-unknown blocks benchmark promotion"
            ) from None

        _unlink_if_present(backup)
        backup = None
    except RegistryError:
        raise
    except Exception:
        raise RegistryError("benchmark candidate could not be promoted") from None
    finally:
        finalization_unknown = False
        try:
            _unlink_if_present(temporary)
            if not preserve_backup:
                _unlink_if_present(backup)
        finally:
            try:
                if (
                    guard_created
                    and not outcome_unknown
                    and not _finalize_mutation_guard(guard)
                ):
                    outcome_unknown = True
                    _mark_commit_outcome_unknown(guard)
                    finalization_unknown = True
            finally:
                if lock_descriptor >= 0:
                    _release_mutation_lock(lock_descriptor)
        if finalization_unknown:
            raise RegistryError(
                "commit-outcome-unknown blocks benchmark promotion"
            ) from None
