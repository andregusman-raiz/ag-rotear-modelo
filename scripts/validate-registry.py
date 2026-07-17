#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import List, Optional, Sequence

from model_router.profiles import load_profiles
from model_router.registry import (
    load_model_catalog,
    validate_registry_document,
)


SCHEMA_NAMES = (
    "benchmark-registry-schema.json",
    "child-result-schema.json",
    "profile-schema.json",
    "task-request-schema.json",
)
PROFILE_IDS = frozenset(
    (
        "data",
        "documents-design",
        "operations",
        "research",
        "software",
        "strategy",
    )
)


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate(skill_root: Path) -> List[str]:
    errors = []
    root = Path(skill_root)
    references = root / "references"
    profiles_root = references / "profiles"
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:
        return ["jsonschema is required for the registry development gate"]

    schemas = {}
    for name in SCHEMA_NAMES:
        path = references / name
        try:
            schema = _load_json(path)
            Draft202012Validator.check_schema(schema)
            schemas[name] = schema
        except Exception:
            errors.append("%s is invalid" % name)

    try:
        catalog = load_model_catalog(references / "model-registry.json")
    except Exception:
        catalog = None
        errors.append("model-registry.json is invalid")

    try:
        profiles = load_profiles(profiles_root)
        if frozenset(profiles) != PROFILE_IDS:
            errors.append("profiles do not contain the six canonical profile ids")
    except Exception:
        profiles = {}
        errors.append("profiles are invalid")

    profile_schema = schemas.get("profile-schema.json")
    if profile_schema is not None:
        validator = Draft202012Validator(
            profile_schema,
            format_checker=FormatChecker(),
        )
        for path in sorted(profiles_root.glob("*.json")):
            try:
                payload = _load_json(path)
                if next(validator.iter_errors(payload), None) is not None:
                    errors.append("profiles/%s failed schema validation" % path.name)
            except Exception:
                errors.append("profiles/%s contains invalid JSON" % path.name)

    try:
        registry = validate_registry_document(
            (references / "benchmark-registry.json").read_text(encoding="utf-8"),
            schema_path=references / "benchmark-registry-schema.json",
            model_registry_path=references / "model-registry.json",
            require_schema=True,
        )
        if catalog is not None:
            registry.validate_model_catalog(catalog)
        source_ids = {source.id for source in registry.sources}
        for observation in registry.observations:
            if observation.source_id not in source_ids:
                errors.append(
                    "observation %s references an unknown source"
                    % observation.id
                )
    except Exception:
        errors.append("benchmark-registry.json is invalid")

    if profiles and any(
        "required_checks_passed" not in profile.progress_metrics
        for profile in profiles.values()
    ):
        errors.append("every profile must declare required_checks_passed")
    return sorted(set(errors))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="validate-registry.py")
    parser.add_argument("--skill-root", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    errors = validate(Path(args.skill_root))
    if errors:
        for error in errors:
            print(error)
        return 1
    print("registry valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
