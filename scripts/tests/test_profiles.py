import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

# ruff: noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1]
SKILL_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from model_router.contracts import TaskRequest
from model_router.profiles import (
    Profile,
    ProfileError,
    QualityFloor,
    load_profiles,
    resolve_profile,
)
from support import invalid_profile_payloads, valid_profile_payload, valid_task_payload


EXPECTED_PROFILES = {
    "software": {
        "required_capabilities": ("correctness", "tool-use", "code-change"),
        "validator_categories": (
            "tests",
            "typecheck",
            "lint",
            "build",
            "diff-scope",
            "security",
            "acceptance",
        ),
        "progress_metrics": (
            "required_checks_passed",
            "test_failures",
            "type_errors",
        ),
        "noisy_progress": False,
    },
    "data": {
        "required_capabilities": (
            "schema-reasoning",
            "quantitative-reconciliation",
            "tool-use",
        ),
        "validator_categories": (
            "schema",
            "counts",
            "reconciliation",
            "invariants",
            "lineage",
            "samples",
        ),
        "progress_metrics": (
            "required_checks_passed",
            "unreconciled_count",
            "invariant_failures",
        ),
        "noisy_progress": False,
    },
    "research": {
        "required_capabilities": (
            "source-grounding",
            "web-research",
            "conflict-analysis",
        ),
        "validator_categories": (
            "sources",
            "freshness",
            "coverage",
            "conflicts",
            "attribution",
            "question-fit",
        ),
        "progress_metrics": (
            "required_checks_passed",
            "unsupported_claims",
            "source_conflicts_open",
        ),
        "noisy_progress": True,
    },
    "documents-design": {
        "required_capabilities": (
            "document-production",
            "visual-reasoning",
            "tool-use",
        ),
        "validator_categories": (
            "content",
            "reference-fidelity",
            "render",
            "legibility",
            "accessibility",
            "visual-consistency",
        ),
        "progress_metrics": (
            "required_checks_passed",
            "render_errors",
            "accessibility_failures",
        ),
        "noisy_progress": True,
    },
    "strategy": {
        "required_capabilities": (
            "professional-work",
            "decision-analysis",
            "source-grounding",
        ),
        "validator_categories": (
            "assumptions",
            "alternatives",
            "evidence",
            "contradictions",
            "sensitivity",
            "decision",
        ),
        "progress_metrics": (
            "required_checks_passed",
            "unsupported_assumptions",
            "open_contradictions",
        ),
        "noisy_progress": True,
    },
    "operations": {
        "required_capabilities": (
            "tool-use",
            "state-change",
            "recovery-planning",
        ),
        "validator_categories": (
            "before-state",
            "after-state",
            "dry-run",
            "idempotence",
            "permissions",
            "recovery",
            "rollback",
        ),
        "progress_metrics": (
            "required_checks_passed",
            "state_drift",
            "rollback_gaps",
        ),
        "noisy_progress": False,
    },
}


class ProfileTests(unittest.TestCase):
    def write_profile(self, root, filename, payload):
        Path(root, filename).write_text(json.dumps(payload), encoding="utf-8")

    def duplicate_key_document(self, key, first_value, second_value):
        payload = valid_profile_payload()
        del payload[key]
        members = [
            "%s:%s" % (json.dumps(key), json.dumps(first_value)),
            "%s:%s" % (json.dumps(key), json.dumps(second_value)),
        ]
        members.extend(
            "%s:%s" % (json.dumps(field_name), json.dumps(value))
            for field_name, value in payload.items()
        )
        return "{%s}" % ",".join(members)

    def test_loads_exactly_the_six_initial_profiles(self):
        profiles = load_profiles(SKILL_ROOT / "references" / "profiles")
        self.assertEqual(set(EXPECTED_PROFILES), set(profiles))

    def test_six_profiles_match_the_declared_quality_and_progress_values(self):
        profiles = load_profiles(SKILL_ROOT / "references" / "profiles")
        for profile_id, expected in EXPECTED_PROFILES.items():
            with self.subTest(profile_id=profile_id):
                profile = profiles[profile_id]
                self.assertEqual(
                    expected["required_capabilities"],
                    profile.quality_floor.required_capabilities,
                )
                self.assertEqual(
                    expected["validator_categories"],
                    profile.validator_categories,
                )
                self.assertEqual(expected["progress_metrics"], profile.progress_metrics)
                self.assertEqual(expected["noisy_progress"], profile.noisy_progress)

    def test_each_profile_declares_the_global_quality_floor(self):
        for profile in load_profiles(
            SKILL_ROOT / "references" / "profiles"
        ).values():
            with self.subTest(profile_id=profile.id):
                self.assertTrue(profile.quality_floor.required_capabilities)
                self.assertEqual(
                    "pass", profile.quality_floor.required_validation_status
                )
                self.assertTrue(profile.validator_categories)
                self.assertTrue(profile.critical_requires_independent_verifier)

    def test_progress_directions_maximize_passes_and_minimize_failures(self):
        profile = load_profiles(
            SKILL_ROOT / "references" / "profiles"
        )["software"]
        self.assertEqual(
            {
                "required_checks_passed": "max",
                "test_failures": "min",
                "type_errors": "min",
            },
            profile.progress_directions,
        )
        with self.assertRaises(TypeError):
            profile.progress_directions["test_failures"] = "max"

    def test_new_legal_profile_loads_without_editing_python(self):
        payload = valid_profile_payload()
        payload.pop("noisy_progress")
        with tempfile.TemporaryDirectory() as tmp:
            self.write_profile(tmp, "legal.json", payload)
            profiles = load_profiles(Path(tmp))
        self.assertEqual("legal", profiles["legal"].id)
        self.assertFalse(profiles["legal"].noisy_progress)

    def test_rejects_profile_that_tries_to_disable_global_gate(self):
        payload = valid_profile_payload("unsafe")
        payload["disable_global_gates"] = ["authorization"]
        with tempfile.TemporaryDirectory() as tmp:
            self.write_profile(tmp, "unsafe.json", payload)
            with self.assertRaisesRegex(
                ProfileError,
                r"unsafe\.json\.disable_global_gates",
            ):
                load_profiles(Path(tmp))

    def test_rejects_duplicate_json_keys_before_last_value_wins(self):
        cases = (
            ("id", "legal", "strategy"),
            ("version", "1.0.0", "2.0.0"),
            ("critical_requires_independent_verifier", True, False),
        )
        for key, first_value, second_value in cases:
            with self.subTest(key=key):
                with tempfile.TemporaryDirectory() as tmp:
                    filename = "%s.json" % key
                    Path(tmp, filename).write_text(
                        self.duplicate_key_document(
                            key,
                            first_value,
                            second_value,
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        ProfileError,
                        r"%s\.%s.*duplicate" % (re.escape(filename), re.escape(key)),
                    ):
                        load_profiles(Path(tmp))

    def test_rejects_duplicate_json_keys_inside_nested_objects(self):
        document = json.dumps(valid_profile_payload())
        document = (
            document[:-1]
            + ', "metadata": {"owner": "first", "owner": "second"}}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "nested.json").write_text(document, encoding="utf-8")
            with self.assertRaisesRegex(
                ProfileError,
                r"nested\.json\.owner.*duplicate",
            ):
                load_profiles(Path(tmp))

    def test_loader_rejects_duplicates_in_every_semantic_array(self):
        for field_name in (
            "signals",
            "benchmark_tags",
            "required_capabilities",
            "validator_categories",
            "progress_metrics",
        ):
            with self.subTest(field_name=field_name):
                payload = valid_profile_payload()
                duplicate_value = payload[field_name][0]
                payload[field_name] = [duplicate_value, duplicate_value]
                with tempfile.TemporaryDirectory() as tmp:
                    self.write_profile(tmp, "duplicate.json", payload)
                    with self.assertRaisesRegex(
                        ProfileError,
                        r"duplicate\.json\.%s\[1\].*duplicate"
                        % re.escape(field_name),
                    ):
                        load_profiles(Path(tmp))

    def test_rejects_invalid_payloads_with_profile_error_and_field_path(self):
        for expected_path, payload in invalid_profile_payloads():
            with self.subTest(expected_path=expected_path):
                with tempfile.TemporaryDirectory() as tmp:
                    self.write_profile(tmp, "profile.json", payload)
                    try:
                        load_profiles(Path(tmp))
                    except Exception as error:
                        self.assertIsInstance(error, ProfileError)
                        field_path = expected_path.removeprefix("profile")
                        self.assertIn("profile.json%s" % field_path, str(error))
                    else:
                        self.fail("ProfileError not raised for %s" % expected_path)

    def test_rejects_invalid_json_and_non_object_files_as_profile_errors(self):
        cases = (
            ("{", "invalid JSON"),
            ("[]", "must be an object"),
            ("null", "must be an object"),
        )
        for content, message in cases:
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as tmp:
                    Path(tmp, "broken.json").write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(
                        ProfileError,
                        r"broken\.json.*%s" % message,
                    ):
                        load_profiles(Path(tmp))

    def test_rejects_duplicate_ids_with_the_second_file_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_profile(tmp, "first.json", valid_profile_payload("legal"))
            self.write_profile(tmp, "second.json", valid_profile_payload("legal"))
            with self.assertRaisesRegex(
                ProfileError,
                r"second\.json\.id.*duplicate",
            ):
                load_profiles(Path(tmp))

    def test_rejects_missing_non_directory_and_empty_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ProfileError, r"missing.*does not exist"):
                load_profiles(root / "missing")
            file_root = root / "profiles.json"
            file_root.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ProfileError, r"profiles\.json.*directory"):
                load_profiles(file_root)
            empty_root = root / "empty"
            empty_root.mkdir()
            with self.assertRaisesRegex(ProfileError, r"empty.*no profiles found"):
                load_profiles(empty_root)

    def test_dataclasses_copy_iterables_and_reject_invalid_direct_construction(self):
        capabilities = ["source-grounding"]
        signals = ["contrato"]
        quality_floor = QualityFloor(capabilities)
        profile = Profile(
            id="legal",
            version="1.0.0",
            signals=signals,
            benchmark_tags=["professional-work"],
            quality_floor=quality_floor,
            validator_categories=["sources"],
            progress_metrics=["required_checks_passed"],
            noisy_progress=False,
            critical_requires_independent_verifier=True,
        )
        capabilities.append("risk-analysis")
        signals.append("regulação")
        self.assertEqual(("source-grounding",), quality_floor.required_capabilities)
        self.assertEqual(("contrato",), profile.signals)
        self.assertIs(type(profile.signals), tuple)

        invalid_constructions = (
            ("quality_floor.required_capabilities", lambda: QualityFloor([])),
            (
                "quality_floor.required_validation_status",
                lambda: QualityFloor(["source-grounding"], "fail"),
            ),
            (
                "profile.version",
                lambda: Profile(
                    "legal",
                    "1.0",
                    ["contrato"],
                    ["professional-work"],
                    quality_floor,
                    ["sources"],
                    ["required_checks_passed"],
                    False,
                    True,
                ),
            ),
            (
                "profile.noisy_progress",
                lambda: Profile(
                    "legal",
                    "1.0.0",
                    ["contrato"],
                    ["professional-work"],
                    quality_floor,
                    ["sources"],
                    ["required_checks_passed"],
                    "false",
                    True,
                ),
            ),
            (
                "profile.critical_requires_independent_verifier",
                lambda: Profile(
                    "legal",
                    "1.0.0",
                    ["contrato"],
                    ["professional-work"],
                    quality_floor,
                    ["sources"],
                    ["required_checks_passed"],
                    False,
                    1,
                ),
            ),
        )
        for expected_path, operation in invalid_constructions:
            with self.subTest(expected_path=expected_path):
                with self.assertRaisesRegex(ProfileError, expected_path):
                    operation()

    def test_dataclasses_reject_duplicates_in_every_semantic_array(self):
        quality_floor = QualityFloor(["source-grounding"])
        for field_name in (
            "signals",
            "benchmark_tags",
            "validator_categories",
            "progress_metrics",
        ):
            with self.subTest(field_name=field_name):
                values = {
                    "signals": ["contrato"],
                    "benchmark_tags": ["professional-work"],
                    "validator_categories": ["sources"],
                    "progress_metrics": ["required_checks_passed"],
                }
                duplicate_value = values[field_name][0]
                values[field_name] = [duplicate_value, duplicate_value]
                with self.assertRaisesRegex(
                    ProfileError,
                    r"profile\.%s\[1\].*duplicate" % re.escape(field_name),
                ):
                    Profile(
                        id="legal",
                        version="1.0.0",
                        quality_floor=quality_floor,
                        noisy_progress=False,
                        critical_requires_independent_verifier=True,
                        **values
                    )

        with self.assertRaisesRegex(
            ProfileError,
            r"quality_floor\.required_capabilities\[1\].*duplicate",
        ):
            QualityFloor(["source-grounding", "source-grounding"])

    def test_resolve_profile_validates_primary_and_secondary_profiles(self):
        profiles = load_profiles(SKILL_ROOT / "references" / "profiles")
        payload = valid_task_payload()
        payload["secondary_profiles"] = ["research", "data"]
        request = TaskRequest.from_dict(payload)
        self.assertIs(profiles["software"], resolve_profile(request, profiles))

        payload["primary_profile"] = "legal"
        with self.assertRaisesRegex(
            ProfileError,
            r"task_request\.primary_profile.*legal",
        ):
            resolve_profile(TaskRequest.from_dict(payload), profiles)

        payload["primary_profile"] = "software"
        payload["secondary_profiles"] = ["research", "legal"]
        with self.assertRaisesRegex(
            ProfileError,
            r"task_request\.secondary_profiles\[1\].*legal",
        ):
            resolve_profile(TaskRequest.from_dict(payload), profiles)


if __name__ == "__main__":
    unittest.main()
