import json
import re
from collections.abc import Mapping as RuntimeMapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple

from .contracts import TaskRequest


class ProfileError(ValueError):
    pass


_PROFILE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
_REQUIRED_PROFILE_FIELDS = (
    "id",
    "version",
    "signals",
    "benchmark_tags",
    "required_capabilities",
    "validator_categories",
    "progress_metrics",
    "critical_requires_independent_verifier",
)
_ALLOWED_PROFILE_FIELDS = _REQUIRED_PROFILE_FIELDS + ("noisy_progress",)


def _fail(path: str, message: str):
    raise ProfileError("%s %s" % (path, message))


def _require_string(value: Any, path: str) -> str:
    if type(value) is not str:
        _fail(path, "must be a string")
    if value == "" or value.strip() == "":
        _fail(path, "must not be blank")
    if value != value.strip():
        _fail(path, "must not have leading or trailing whitespace")
    return value


def _require_profile_id(value: Any, path: str) -> str:
    profile_id = _require_string(value, path)
    if _PROFILE_ID_PATTERN.fullmatch(profile_id) is None:
        _fail(path, "must be a lowercase kebab-case identifier")
    return profile_id


def _require_version(value: Any, path: str) -> str:
    version = _require_string(value, path)
    if _VERSION_PATTERN.fullmatch(version) is None:
        _fail(path, "must be a semantic version such as 1.0.0")
    return version


def _require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _copy_strings(value: Any, path: str) -> Tuple[str, ...]:
    if type(value) in (str, bytes) or isinstance(value, RuntimeMapping):
        _fail(path, "must be an iterable of strings")
    try:
        items = tuple(value)
    except Exception as error:
        raise ProfileError("%s could not be read as an iterable" % path) from error
    if not items:
        _fail(path, "must not be empty")
    first_indexes = {}
    for index, item in enumerate(items):
        _require_string(item, "%s[%d]" % (path, index))
        if item in first_indexes:
            _fail(
                "%s[%d]" % (path, index),
                "is a duplicate of index %d" % first_indexes[item],
            )
        first_indexes[item] = index
    return items


def _parse_string_array(value: Any, path: str) -> Tuple[str, ...]:
    if type(value) is not list:
        _fail(path, "must be an array")
    return _copy_strings(value, path)


@dataclass(frozen=True)
class QualityFloor:
    required_capabilities: Tuple[str, ...]
    required_validation_status: str = "pass"

    def __post_init__(self):
        object.__setattr__(
            self,
            "required_capabilities",
            _copy_strings(
                self.required_capabilities,
                "quality_floor.required_capabilities",
            ),
        )
        status = _require_string(
            self.required_validation_status,
            "quality_floor.required_validation_status",
        )
        if status != "pass":
            _fail(
                "quality_floor.required_validation_status",
                "must equal pass",
            )


@dataclass(frozen=True)
class Profile:
    id: str
    version: str
    signals: Tuple[str, ...]
    benchmark_tags: Tuple[str, ...]
    quality_floor: QualityFloor
    validator_categories: Tuple[str, ...]
    progress_metrics: Tuple[str, ...]
    noisy_progress: bool
    critical_requires_independent_verifier: bool

    def __post_init__(self):
        _require_profile_id(self.id, "profile.id")
        _require_version(self.version, "profile.version")
        for field_name in (
            "signals",
            "benchmark_tags",
            "validator_categories",
            "progress_metrics",
        ):
            object.__setattr__(
                self,
                field_name,
                _copy_strings(
                    getattr(self, field_name),
                    "profile.%s" % field_name,
                ),
            )
        if type(self.quality_floor) is not QualityFloor:
            _fail("profile.quality_floor", "must be QualityFloor")
        _require_bool(self.noisy_progress, "profile.noisy_progress")
        _require_bool(
            self.critical_requires_independent_verifier,
            "profile.critical_requires_independent_verifier",
        )

    @property
    def progress_directions(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                name: "max" if name == "required_checks_passed" else "min"
                for name in self.progress_metrics
            }
        )


def _validate_payload(value: Any, path: str) -> Dict[str, Any]:
    if type(value) is not dict:
        _fail(path, "must be an object")
    if "disable_global_gates" in value:
        _fail(path + ".disable_global_gates", "is forbidden")
    for field_name in _REQUIRED_PROFILE_FIELDS:
        if field_name not in value:
            _fail("%s.%s" % (path, field_name), "is required")
    for field_name in value:
        if field_name not in _ALLOWED_PROFILE_FIELDS:
            _fail("%s.%s" % (path, field_name), "is not allowed")
    return value


def _profile_from_payload(value: Any, path: str) -> Profile:
    payload = _validate_payload(value, path)
    return Profile(
        id=_require_profile_id(payload["id"], path + ".id"),
        version=_require_version(payload["version"], path + ".version"),
        signals=_parse_string_array(payload["signals"], path + ".signals"),
        benchmark_tags=_parse_string_array(
            payload["benchmark_tags"],
            path + ".benchmark_tags",
        ),
        quality_floor=QualityFloor(
            _parse_string_array(
                payload["required_capabilities"],
                path + ".required_capabilities",
            )
        ),
        validator_categories=_parse_string_array(
            payload["validator_categories"],
            path + ".validator_categories",
        ),
        progress_metrics=_parse_string_array(
            payload["progress_metrics"],
            path + ".progress_metrics",
        ),
        noisy_progress=_require_bool(
            payload.get("noisy_progress", False),
            path + ".noisy_progress",
        ),
        critical_requires_independent_verifier=_require_bool(
            payload["critical_requires_independent_verifier"],
            path + ".critical_requires_independent_verifier",
        ),
    )


class _DuplicateJsonKeyError(ValueError):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def _strict_object(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateJsonKeyError(key)
        payload[key] = value
    return payload


def load_profiles(root: Path) -> Dict[str, Profile]:
    try:
        root_path = Path(root)
    except Exception as error:
        raise ProfileError("profiles.root must be a filesystem path") from error
    if not root_path.exists():
        _fail(str(root_path), "does not exist")
    if not root_path.is_dir():
        _fail(str(root_path), "must be a directory")
    try:
        paths = sorted(root_path.glob("*.json"))
    except OSError as error:
        raise ProfileError("%s could not be listed" % root_path) from error
    if not paths:
        _fail(str(root_path), "contains no profiles found")

    profiles = {}  # type: Dict[str, Profile]
    for path in paths:
        profile_path = str(path)
        try:
            document = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ProfileError("%s could not be read" % profile_path) from error
        try:
            payload = json.loads(document, object_pairs_hook=_strict_object)
        except _DuplicateJsonKeyError as error:
            raise ProfileError(
                "%s.%s is a duplicate JSON key" % (profile_path, error.key)
            ) from error
        except (json.JSONDecodeError, UnicodeError) as error:
            raise ProfileError("%s contains invalid JSON" % profile_path) from error

        try:
            profile = _profile_from_payload(payload, profile_path)
        except ProfileError:
            raise
        except Exception as error:
            raise ProfileError("%s could not be loaded" % profile_path) from error
        if profile.id in profiles:
            _fail(profile_path + ".id", "is a duplicate profile id")
        profiles[profile.id] = profile
    return profiles


def resolve_profile(
    request: TaskRequest,
    profiles: Mapping[str, Profile],
) -> Profile:
    if type(request) is not TaskRequest:
        _fail("request", "must be TaskRequest")
    if not isinstance(profiles, RuntimeMapping):
        _fail("profiles", "must be a mapping")
    try:
        available = dict(profiles)
    except Exception as error:
        raise ProfileError("profiles could not be read as a mapping") from error
    for profile_id, profile in available.items():
        if type(profile_id) is not str:
            _fail("profiles.<key>", "must be a string")
        _require_profile_id(profile_id, "profiles.%s" % profile_id)
        if type(profile) is not Profile:
            _fail("profiles.%s" % profile_id, "must be Profile")
        if profile.id != profile_id:
            _fail("profiles.%s.id" % profile_id, "must match its mapping key")

    if request.primary_profile not in available:
        _fail(
            "task_request.primary_profile",
            "references unknown profile %s" % request.primary_profile,
        )
    for index, profile_id in enumerate(request.secondary_profiles):
        if profile_id not in available:
            _fail(
                "task_request.secondary_profiles[%d]" % index,
                "references unknown profile %s" % profile_id,
            )
    return available[request.primary_profile]
