import json
import re
from collections.abc import Mapping as RuntimeMapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple

from .contracts import Effort, Route


class CatalogError(ValueError):
    pass


_TARGET_SLUG = re.compile(r"^gpt-5\.6-(luna|terra|sol)$")
_MULTI_AGENT_VERSION = re.compile(r"^v[12]$")
_SOURCES = ("live", "cache", "bundled", "seed")
_REQUIRED_MODEL_FIELDS = (
    "slug",
    "display_name",
    "default_reasoning_level",
    "supported_reasoning_levels",
    "supported_in_api",
    "visibility",
    "priority",
    "multi_agent_version",
)


def _fail(path: str, message: str):
    raise CatalogError("%s %s" % (path, message))


def _require_string(value: Any, path: str) -> str:
    if type(value) is not str:
        _fail(path, "must be a string")
    if value == "" or value.strip() == "":
        _fail(path, "must not be blank")
    if value != value.strip():
        _fail(path, "must not have leading or trailing whitespace")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _require_int(value: Any, path: str) -> int:
    if type(value) is not int:
        _fail(path, "must be an integer")
    if value < 0:
        _fail(path, "must be non-negative")
    return value


def _require_optional_price(value: Any, path: str) -> Optional[float]:
    if value is None:
        return None
    if type(value) not in (int, float):
        _fail(path, "must be a number")
    if not isfinite(value):
        _fail(path, "must be finite")
    if value < 0:
        _fail(path, "must be non-negative")
    return value


def _require_optional_multi_agent_version(
    value: Any,
    path: str,
) -> Optional[str]:
    if value is None:
        return None
    version = _require_string(value, path)
    if _MULTI_AGENT_VERSION.fullmatch(version) is None:
        _fail(path, "must be one of: v1, v2")
    return version


def _copy_efforts(value: Any, path: str) -> Tuple[Effort, ...]:
    if type(value) in (str, bytes) or isinstance(value, RuntimeMapping):
        _fail(path, "must be an iterable of Effort")
    try:
        efforts = tuple(value)
    except Exception:
        raise CatalogError("%s could not be read as an iterable" % path) from None
    if not efforts:
        _fail(path, "must not be empty")
    seen = set()
    for index, effort in enumerate(efforts):
        if type(effort) is not Effort:
            _fail("%s[%d]" % (path, index), "must be Effort")
        if effort in seen:
            _fail("%s[%d]" % (path, index), "is a duplicate effort")
        seen.add(effort)
    return efforts


def _copy_tools(value: Any, path: str) -> Tuple[str, ...]:
    if type(value) in (str, bytes) or isinstance(value, RuntimeMapping):
        _fail(path, "must be an iterable of strings")
    try:
        tools = tuple(value)
    except Exception:
        raise CatalogError("%s could not be read as an iterable" % path) from None
    seen = set()
    for index, tool in enumerate(tools):
        parsed = _require_string(tool, "%s[%d]" % (path, index))
        if parsed in seen:
            _fail("%s[%d]" % (path, index), "is a duplicate tool")
        seen.add(parsed)
    return tools


@dataclass(frozen=True)
class ModelSpec:
    slug: str
    display_name: str
    default_effort: Effort
    supported_efforts: Tuple[Effort, ...]
    supported_in_api: bool
    visibility: str
    priority: int
    multi_agent_version: Optional[str]
    input_price_per_million: Optional[float]
    output_price_per_million: Optional[float]
    supported_tools: Tuple[str, ...] = ()
    supported_tools_known: bool = False

    def __post_init__(self):
        slug = _require_string(self.slug, "model.slug")
        if _TARGET_SLUG.fullmatch(slug) is None:
            _fail("model.slug", "must be a supported GPT-5.6 route model")
        _require_string(self.display_name, "model.display_name")
        if type(self.default_effort) is not Effort:
            _fail("model.default_effort", "must be Effort")
        efforts = _copy_efforts(self.supported_efforts, "model.supported_efforts")
        object.__setattr__(self, "supported_efforts", efforts)
        if self.default_effort not in efforts:
            _fail("model.default_effort", "must be a supported effort")
        _require_bool(self.supported_in_api, "model.supported_in_api")
        _require_string(self.visibility, "model.visibility")
        _require_int(self.priority, "model.priority")
        object.__setattr__(
            self,
            "multi_agent_version",
            _require_optional_multi_agent_version(
                self.multi_agent_version,
                "model.multi_agent_version",
            ),
        )
        object.__setattr__(
            self,
            "input_price_per_million",
            _require_optional_price(
                self.input_price_per_million,
                "model.input_price_per_million",
            ),
        )
        object.__setattr__(
            self,
            "output_price_per_million",
            _require_optional_price(
                self.output_price_per_million,
                "model.output_price_per_million",
            ),
        )
        tools = _copy_tools(self.supported_tools, "model.supported_tools")
        object.__setattr__(self, "supported_tools", tools)
        _require_bool(
            self.supported_tools_known,
            "model.supported_tools_known",
        )
        if tools and not self.supported_tools_known:
            _fail(
                "model.supported_tools_known",
                "must be true when supported_tools are present",
            )


@dataclass(frozen=True)
class CatalogSnapshot:
    source: str
    observed_at: str
    models: Tuple[ModelSpec, ...]

    def __post_init__(self):
        source = _require_string(self.source, "catalog.source")
        if source not in _SOURCES:
            _fail("catalog.source", "must be one of: %s" % ", ".join(_SOURCES))
        _require_string(self.observed_at, "catalog.observed_at")
        if type(self.models) in (str, bytes) or isinstance(
            self.models,
            RuntimeMapping,
        ):
            _fail("catalog.models", "must be an iterable of ModelSpec")
        try:
            models = tuple(self.models)
        except Exception:
            raise CatalogError(
                "catalog.models could not be read as an iterable"
            ) from None
        if not models:
            _fail("catalog.models", "must contain a target model")
        seen = set()
        for index, model in enumerate(models):
            if type(model) is not ModelSpec:
                _fail("catalog.models[%d]" % index, "must be ModelSpec")
            if model.slug in seen:
                _fail("catalog.models[%d].slug" % index, "is a duplicate slug")
            seen.add(model.slug)
        object.__setattr__(self, "models", models)

    @property
    def best_capability_model(self) -> ModelSpec:
        return min(self.models, key=lambda item: (item.priority, item.slug))


def _mapping_copy(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, RuntimeMapping):
        _fail(path, "must be an object")
    try:
        payload = dict(value)
    except Exception:
        raise CatalogError("%s could not be read as an object" % path) from None
    for key in payload:
        if type(key) is not str:
            _fail(path + ".<key>", "must be a string")
    return payload


def _parse_effort(value: Any, path: str) -> Effort:
    if type(value) is not str:
        _fail(path, "must be a string")
    try:
        return Effort(value)
    except (TypeError, ValueError):
        _fail(path, "must be a supported effort")


def _parse_supported_efforts(value: Any, path: str) -> Tuple[Effort, ...]:
    if type(value) is not list:
        _fail(path, "must be an array")
    efforts = []
    for index, item in enumerate(value):
        item_path = "%s[%d]" % (path, index)
        payload = _mapping_copy(item, item_path)
        if "effort" not in payload:
            _fail(item_path + ".effort", "is required")
        efforts.append(_parse_effort(payload["effort"], item_path + ".effort"))
    return _copy_efforts(efforts, path)


def _parse_tools(value: Any, path: str) -> Tuple[str, ...]:
    if type(value) is not list:
        _fail(path, "must be an array")
    return _copy_tools(value, path)


def _parse_model(payload: Mapping[str, Any], path: str) -> ModelSpec:
    for field_name in _REQUIRED_MODEL_FIELDS:
        if field_name not in payload:
            _fail("%s.%s" % (path, field_name), "is required")
    supported_efforts = _parse_supported_efforts(
        payload["supported_reasoning_levels"],
        path + ".supported_reasoning_levels",
    )
    default_effort = _parse_effort(
        payload["default_reasoning_level"],
        path + ".default_reasoning_level",
    )
    tools_known = "supported_tools" in payload
    return ModelSpec(
        slug=_require_string(payload["slug"], path + ".slug"),
        display_name=_require_string(
            payload["display_name"],
            path + ".display_name",
        ),
        default_effort=default_effort,
        supported_efforts=supported_efforts,
        supported_in_api=_require_bool(
            payload["supported_in_api"],
            path + ".supported_in_api",
        ),
        visibility=_require_string(payload["visibility"], path + ".visibility"),
        priority=_require_int(payload["priority"], path + ".priority"),
        multi_agent_version=_require_optional_multi_agent_version(
            payload["multi_agent_version"],
            path + ".multi_agent_version",
        ),
        input_price_per_million=_require_optional_price(
            payload.get("input_price_per_million"),
            path + ".input_price_per_million",
        ),
        output_price_per_million=_require_optional_price(
            payload.get("output_price_per_million"),
            path + ".output_price_per_million",
        ),
        supported_tools=(
            _parse_tools(payload["supported_tools"], path + ".supported_tools")
            if tools_known
            else ()
        ),
        supported_tools_known=tools_known,
    )


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def catalog_from_json(
    value: Any,
    source: str = "live",
) -> CatalogSnapshot:
    if type(value) is list:
        entries = value
    elif isinstance(value, RuntimeMapping):
        payload = _mapping_copy(value, "catalog")
        if "models" not in payload:
            _fail("catalog.models", "is required")
        entries = payload["models"]
    else:
        _fail("catalog", "must be an array or an object with models")
    if type(entries) is not list:
        _fail("catalog.models", "must be an array")

    models = []
    seen = set()
    for index, item in enumerate(entries):
        path = "catalog.models[%d]" % index
        payload = _mapping_copy(item, path)
        if "slug" not in payload:
            _fail(path + ".slug", "is required")
        slug = _require_string(payload["slug"], path + ".slug")
        if _TARGET_SLUG.fullmatch(slug) is None:
            continue
        if slug in seen:
            _fail(path + ".slug", "is a duplicate slug")
        seen.add(slug)
        models.append(_parse_model(payload, path))
    return CatalogSnapshot(source=source, observed_at=_observed_at(), models=models)


class _DuplicateJsonKeyError(ValueError):
    pass


def _strict_object(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateJsonKeyError()
        payload[key] = value
    return payload


def _reject_json_constant(_value):
    raise ValueError("non-finite JSON number")


def _loads_json(document: Any) -> Any:
    if type(document) is not str:
        raise CatalogError("catalog output must be text")
    try:
        return json.loads(
            document,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise CatalogError("catalog contains invalid JSON") from None


def _runner_payload(runner: Callable, argv: Tuple[str, ...]) -> Any:
    try:
        completed = runner(argv)
        returncode = completed.returncode
        stdout = completed.stdout
    except Exception:
        raise CatalogError("catalog runner failed") from None
    if type(returncode) is not int:
        raise CatalogError("catalog runner returned an invalid status")
    if returncode != 0:
        raise CatalogError("catalog command failed")
    return _loads_json(stdout)


def _path_payload(path: Path) -> Any:
    try:
        document = Path(path).read_text(encoding="utf-8")
    except Exception:
        raise CatalogError("catalog file could not be read") from None
    return _loads_json(document)


def _enrich_prices(
    catalog: CatalogSnapshot,
    seed_path: Path,
) -> CatalogSnapshot:
    try:
        seed = catalog_from_json(_path_payload(seed_path), source="seed")
    except Exception:
        return catalog
    pricing = {model.slug: model for model in seed.models}
    enriched = []
    for model in catalog.models:
        seed_model = pricing.get(model.slug)
        if seed_model is None:
            enriched.append(model)
            continue
        enriched.append(
            replace(
                model,
                input_price_per_million=(
                    model.input_price_per_million
                    if model.input_price_per_million is not None
                    else seed_model.input_price_per_million
                ),
                output_price_per_million=(
                    model.output_price_per_million
                    if model.output_price_per_million is not None
                    else seed_model.output_price_per_million
                ),
            )
        )
    return CatalogSnapshot(catalog.source, catalog.observed_at, enriched)


def discover_catalog(
    codex_bin: str,
    cache_path: Path,
    seed_path: Path,
    runner: Callable,
) -> CatalogSnapshot:
    attempts = (
        ("live", (codex_bin, "debug", "models")),
        ("cache", None),
        ("bundled", (codex_bin, "debug", "models", "--bundled")),
    )
    errors = []
    for source, argv in attempts:
        try:
            payload = (
                _path_payload(cache_path)
                if source == "cache"
                else _runner_payload(runner, argv)
            )
            catalog = catalog_from_json(payload, source=source)
            return _enrich_prices(catalog, seed_path)
        except Exception:
            errors.append("%s:unavailable" % source)
    try:
        return catalog_from_json(_path_payload(seed_path), source="seed")
    except Exception:
        errors.append("seed:unavailable")
    raise CatalogError(
        "catalog discovery failed (%s)" % "; ".join(errors)
    ) from None


def generate_candidates(catalog: CatalogSnapshot) -> Tuple[Route, ...]:
    if type(catalog) is not CatalogSnapshot:
        _fail("catalog", "must be CatalogSnapshot")
    return tuple(
        Route(model=model.slug, effort=effort)
        for model in catalog.models
        if model.supported_in_api and model.visibility == "list"
        for effort in model.supported_efforts
    )
