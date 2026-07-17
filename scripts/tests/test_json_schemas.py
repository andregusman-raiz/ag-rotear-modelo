import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from support import (
    PROFILE_ARRAY_FIELDS,
    invalid_child_report_payloads,
    invalid_profile_payloads,
    invalid_task_payloads,
    valid_child_report_payload,
    valid_profile_payload,
    valid_task_payload,
)


SKILL_ROOT = Path(__file__).resolve().parents[2]


class Draft202012SchemaTests(unittest.TestCase):
    def load_schema(self, filename):
        path = SKILL_ROOT / "references" / filename
        with path.open(encoding="utf-8") as schema_file:
            return json.load(schema_file)

    def test_schemas_are_valid_draft_2020_12(self):
        for filename in (
            "task-request-schema.json",
            "child-result-schema.json",
            "profile-schema.json",
        ):
            with self.subTest(filename=filename):
                Draft202012Validator.check_schema(self.load_schema(filename))

    def test_task_request_schema_accepts_canonical_payload(self):
        validator = Draft202012Validator(self.load_schema("task-request-schema.json"))
        validator.validate(valid_task_payload())

    def test_integer_fields_follow_draft_2020_12_integral_number_semantics(self):
        task_payload = valid_task_payload()
        task_payload["independent_fronts"] = 2.0
        Draft202012Validator(
            self.load_schema("task-request-schema.json")
        ).validate(task_payload)

        child_payload = valid_child_report_payload()
        child_payload["evidence"][0]["exit_code"] = 0.0
        Draft202012Validator(
            self.load_schema("child-result-schema.json")
        ).validate(child_payload)

    def test_task_request_schema_rejects_invalid_json_mutations(self):
        validator = Draft202012Validator(self.load_schema("task-request-schema.json"))
        for expected_path, payload in invalid_task_payloads():
            with self.subTest(path=expected_path):
                self.assertFalse(validator.is_valid(payload))

    def test_child_result_schema_accepts_canonical_payload(self):
        validator = Draft202012Validator(self.load_schema("child-result-schema.json"))
        validator.validate(valid_child_report_payload())

    def test_child_result_schema_rejects_every_runtime_invalid_mutation(self):
        validator = Draft202012Validator(self.load_schema("child-result-schema.json"))
        for expected_path, payload in invalid_child_report_payloads():
            with self.subTest(path=expected_path):
                self.assertFalse(validator.is_valid(payload))

    def test_profile_schema_accepts_canonical_and_optional_noisy_progress(self):
        validator = Draft202012Validator(self.load_schema("profile-schema.json"))
        payload = valid_profile_payload()
        validator.validate(payload)
        del payload["noisy_progress"]
        validator.validate(payload)

    def test_profile_schema_rejects_every_runtime_invalid_mutation(self):
        validator = Draft202012Validator(self.load_schema("profile-schema.json"))
        for expected_path, payload in invalid_profile_payloads():
            with self.subTest(path=expected_path):
                self.assertFalse(validator.is_valid(payload))

    def test_profile_schema_rejects_duplicates_in_every_semantic_array(self):
        validator = Draft202012Validator(self.load_schema("profile-schema.json"))
        for field_name in PROFILE_ARRAY_FIELDS:
            with self.subTest(field_name=field_name):
                payload = valid_profile_payload()
                duplicate_value = payload[field_name][0]
                payload[field_name] = [duplicate_value, duplicate_value]
                self.assertFalse(validator.is_valid(payload))


if __name__ == "__main__":
    unittest.main()
