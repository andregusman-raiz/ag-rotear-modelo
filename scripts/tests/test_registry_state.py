import hashlib
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
from collections import Counter
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator, FormatChecker
from platform_support import (
    DIRECTORY_SYMLINK_AVAILABLE,
    FILE_SYMLINK_AVAILABLE,
    POSIX,
)

SCRIPTS = Path(__file__).resolve().parents[1]
SKILL_ROOT = SCRIPTS.parent
REFERENCES = SKILL_ROOT / "references"
SEED = REFERENCES / "benchmark-registry.json"
SCHEMA = REFERENCES / "benchmark-registry-schema.json"
sys.path.insert(0, str(SCRIPTS))

from model_router.contracts import Effort, Route  # noqa: E402
from model_router.registry import (  # noqa: E402
    BenchmarkObservation,
    BenchmarkRegistry,
    BenchmarkSource,
    RegistryError,
    ScalarMeasurement,
    load_model_catalog,
    promote_candidate,
    validate_registry_document,
)
from model_router.state import (  # noqa: E402
    SAFE_OBSERVATION_FIELDS,
    Observation,
    RuntimeState,
    StateError,
    StoredObservation,
)
from support import (  # noqa: E402
    benchmark_registry_contract_mutations,
    complete_decision_payload,
    observation_fixture,
)


def _complete_decision(fragment=None):
    payload = complete_decision_payload()

    def merge(target, source):
        for name, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(name), dict):
                merge(target[name], value)
            else:
                target[name] = deepcopy(value)

    if fragment is not None:
        merge(payload, fragment)
    return payload


class BenchmarkRegistryTests(unittest.TestCase):
    def seed_payload(self):
        return json.loads(SEED.read_text(encoding="utf-8"))

    def write_payload(self, root, payload, name="registry.json"):
        path = Path(root, name)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_seed_has_only_the_fifty_two_verified_route_observations(self):
        registry = BenchmarkRegistry.load(SEED)
        self.assertEqual(13, len(registry.sources))
        self.assertEqual(52, len(registry.observations))
        self.assertEqual(
            {
                "arc-prize-gpt-5-6": 45,
                "automation-bench": 4,
                "artificial-analysis-gpt-5-6": 3,
            },
            Counter(item.source_id for item in registry.observations),
        )

    def test_operational_registry_and_bootstrap_work_with_stdlib_only(self):
        script = """
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, %r)
from model_router.registry import BenchmarkRegistry, promote_candidate
from model_router.state import RuntimeState
references = Path(%r)
assert len(BenchmarkRegistry.load(references / 'benchmark-registry.json').observations) == 52
with tempfile.TemporaryDirectory() as tmp:
    runtime = RuntimeState(Path(tmp) / 'runtime', seed_root=references)
    runtime.bootstrap()
    assert len(runtime.benchmark_registry().observations) == 52
    destination = Path(tmp) / 'promoted' / 'benchmark-registry.json'
    promote_candidate(references / 'benchmark-registry.json', destination)
    assert len(BenchmarkRegistry.load(destination).observations) == 52
print('stdlib-operational-ok')
""" % (str(SCRIPTS), str(REFERENCES))
        completed = subprocess.run(
            [sys.executable, "-S", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("stdlib-operational-ok", completed.stdout.strip())

    def test_arc_seed_preserves_the_official_rounded_matrix(self):
        registry = BenchmarkRegistry.load(SEED)
        actual = {
            (item.route.model, item.route.effort.value, item.metric.name):
            item.metric.value
            for item in registry.observations
            if item.source_id == "arc-prize-gpt-5-6"
        }
        rows = {
            ("gpt-5.6-sol", "max"): (96.5, 92.5, 7.8),
            ("gpt-5.6-sol", "xhigh"): (97.5, 90.0, 7.0),
            ("gpt-5.6-sol", "high"): (97.0, 85.4, 2.1),
            ("gpt-5.6-sol", "medium"): (92.5, 67.1, 1.1),
            ("gpt-5.6-sol", "low"): (74.5, 42.5, 0.3),
            ("gpt-5.6-terra", "max"): (96.5, 83.9, 0.8),
            ("gpt-5.6-terra", "xhigh"): (94.0, 74.2, 0.7),
            ("gpt-5.6-terra", "high"): (92.0, 67.1, 0.5),
            ("gpt-5.6-terra", "medium"): (77.0, 37.5, 0.1),
            ("gpt-5.6-terra", "low"): (60.2, 18.8, 0.0),
            ("gpt-5.6-luna", "max"): (88.0, 59.5, 0.2),
            ("gpt-5.6-luna", "xhigh"): (87.7, 47.6, 0.0),
            ("gpt-5.6-luna", "high"): (76.5, 29.3, 0.1),
            ("gpt-5.6-luna", "medium"): (56.5, 7.4, 0.2),
            ("gpt-5.6-luna", "low"): (34.2, 5.1, 0.2),
        }
        metric_names = (
            "arc-agi-1-public-eval",
            "arc-agi-2-public-eval",
            "arc-agi-3-public-demo",
        )
        expected = {
            (model, effort, metric_name): value
            for (model, effort), values in rows.items()
            for metric_name, value in zip(metric_names, values)
        }
        self.assertEqual(expected, actual)

    def test_score_and_cost_remain_separate_typed_measurements(self):
        registry = BenchmarkRegistry.load(SEED)
        automation = {
            item.route.key: (
                item.metric.name,
                item.metric.value,
                item.metric.unit,
                item.cost.value,
                item.cost.unit,
                item.topology,
                item.evaluated_at,
            )
            for item in registry.observations
            if item.source_id == "automation-bench"
        }
        self.assertEqual(
            {
                "gpt-5.6-sol:max:single": (
                    "task-completed-correctly", 18.1, "percent",
                    3.46, "USD/task", "single", None,
                ),
                "gpt-5.6-sol:xhigh:single": (
                    "task-completed-correctly", 17.0, "percent",
                    1.99, "USD/task", "single", None,
                ),
                "gpt-5.6-terra:max:single": (
                    "task-completed-correctly", 15.2, "percent",
                    2.12, "USD/task", "single", None,
                ),
                "gpt-5.6-luna:max:single": (
                    "task-completed-correctly", 14.9, "percent",
                    2.18, "USD/task", "single", None,
                ),
            },
            automation,
        )
        analysis = {
            item.route.key: (
                item.metric.name,
                item.metric.value,
                item.metric.unit,
                item.cost.value,
                item.cost.unit,
                item.topology,
            )
            for item in registry.observations
            if item.source_id == "artificial-analysis-gpt-5-6"
        }
        self.assertEqual(
            {
                "gpt-5.6-sol:max:single": (
                    "artificial-analysis-intelligence-index", 59,
                    "index-points", 1.04, "USD/task", "unspecified",
                ),
                "gpt-5.6-terra:max:single": (
                    "artificial-analysis-intelligence-index", 55,
                    "index-points", 0.55, "USD/task", "unspecified",
                ),
                "gpt-5.6-luna:max:single": (
                    "artificial-analysis-intelligence-index", 51,
                    "index-points", 0.21, "USD/task", "unspecified",
                ),
            },
            analysis,
        )

    def test_sources_without_verified_values_have_no_observations(self):
        registry = BenchmarkRegistry.load(SEED)
        observed = {item.source_id for item in registry.observations}
        self.assertEqual(
            {
                "arc-prize-gpt-5-6",
                "automation-bench",
                "artificial-analysis-gpt-5-6",
            },
            observed,
        )

    def test_active_observations_filter_tags_and_apply_source_precedence(self):
        registry = BenchmarkRegistry.load(SEED)
        operations = registry.active_observations(("operations",))
        self.assertTrue(operations)
        self.assertEqual(
            "automation-bench",
            operations[0].source_id,
        )
        self.assertTrue(
            all("operations" in item.profile_tags for item in operations)
        )
        mixed = registry.active_observations(("ood", "general-intelligence"))
        kinds = [registry.source(item.source_id).kind for item in mixed]
        ranks = {
            "primary-independent": 3,
            "independent-aggregator": 2,
            "vendor": 1,
        }
        self.assertEqual(
            sorted((ranks[kind] for kind in kinds), reverse=True),
            [ranks[kind] for kind in kinds],
        )

    def test_retired_and_quarantined_sources_never_influence_decision(self):
        payload = self.seed_payload()
        template = deepcopy(payload["observations"][0])
        for source_id, status in (
            ("swe-bench-verified", "retired"),
            ("quarantined-source", "quarantine"),
        ):
            if status == "quarantine":
                payload["sources"].append(
                    {
                        "id": source_id,
                        "owner": "Independent evaluator",
                        "url": "https://example.test/quarantine",
                        "status": status,
                        "kind": "primary-independent",
                        "limitations": ["under review"],
                    }
                )
            observation = deepcopy(template)
            observation["id"] = source_id + "-observation"
            observation["source_id"] = source_id
            payload["observations"].append(observation)
        with tempfile.TemporaryDirectory() as tmp:
            registry = BenchmarkRegistry.load(self.write_payload(tmp, payload))
        ids = {
            item.source_id
            for item in registry.active_observations(("ood",))
        }
        self.assertNotIn("swe-bench-verified", ids)
        self.assertNotIn("quarantined-source", ids)

    def test_loader_rejects_duplicate_json_keys_and_non_finite_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, document in (
                (
                    "duplicate.json",
                    '{"schema_version":"1.0.0","schema_version":"1.0.0",'
                    '"verified_at":"2026-07-16","sources":[],"observations":[]}',
                ),
                (
                    "nan.json",
                    SEED.read_text(encoding="utf-8").replace("96.5", "NaN", 1),
                ),
                (
                    "infinity.json",
                    SEED.read_text(encoding="utf-8").replace("96.5", "Infinity", 1),
                ),
            ):
                path = Path(tmp, name)
                path.write_text(document, encoding="utf-8")
                with self.subTest(name=name):
                    with self.assertRaises(RegistryError):
                        BenchmarkRegistry.load(path)

    def test_loader_rejects_duplicate_ids_observations_and_references(self):
        cases = []
        duplicate_source = self.seed_payload()
        duplicate_source["sources"].append(deepcopy(duplicate_source["sources"][0]))
        cases.append(duplicate_source)
        duplicate_id = self.seed_payload()
        clone = deepcopy(duplicate_id["observations"][0])
        clone["source_id"] = "automation-bench"
        duplicate_id["observations"].append(clone)
        cases.append(duplicate_id)
        duplicate_observation = self.seed_payload()
        clone = deepcopy(duplicate_observation["observations"][0])
        clone["id"] = "different-id"
        duplicate_observation["observations"].append(clone)
        cases.append(duplicate_observation)
        missing_reference = self.seed_payload()
        missing_reference["observations"][0]["source_id"] = "missing"
        cases.append(missing_reference)
        with tempfile.TemporaryDirectory() as tmp:
            for index, payload in enumerate(cases):
                with self.subTest(index=index):
                    with self.assertRaises(RegistryError):
                        BenchmarkRegistry.load(
                            self.write_payload(tmp, payload, "%d.json" % index)
                        )

    def test_loader_rejects_invalid_shapes_enums_units_and_scalars(self):
        mutations = (
            (("unexpected",), True),
            (("schema_version",), "2.0.0"),
            (("verified_at",), "2026/07/16"),
            (("sources", 0, "status"), "enabled"),
            (("sources", 0, "kind"), "blog"),
            (("sources", 0, "limitations"), "none"),
            (("observations", 0, "model"), "gpt-4"),
            (("observations", 0, "effort"), "extreme"),
            (("observations", 0, "topology"), "team"),
            (("observations", 0, "metric", "name"), "unknown-score"),
            (("observations", 0, "metric", "unit"), "ratio"),
            (("observations", 0, "metric", "value"), True),
            (("observations", 0, "metric", "value"), math.inf),
            (("observations", 0, "profile_tags"), "ood"),
            (("observations", 0, "harness"), ""),
            (("observations", 0, "evaluated_at"), "not-a-date"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for index, (path, value) in enumerate(mutations):
                payload = self.seed_payload()
                target = payload
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                with self.subTest(path=path, value=value):
                    with self.assertRaises(RegistryError):
                        BenchmarkRegistry.load(
                            self.write_payload(tmp, payload, "%d.json" % index)
                        )

    def test_registry_schema_is_draft_2020_12_and_accepts_seed(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(self.seed_payload())
        validate_registry_document(SEED.read_text(encoding="utf-8"), require_schema=True)

    def test_registry_schema_rejects_runtime_invalid_mutations(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        for path, value in (
            (("unexpected",), True),
            (("sources", 0, "status"), "enabled"),
            (("observations", 0, "metric", "value"), True),
            (("observations", 0, "metric", "unit"), "ratio"),
            (("observations", 0, "topology"), "team"),
        ):
            payload = self.seed_payload()
            target = payload
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            with self.subTest(path=path):
                self.assertFalse(validator.is_valid(payload))
        unsupported_model = self.seed_payload()
        unsupported_model["observations"][0]["model"] = "gpt-4"
        with self.assertRaises(RegistryError):
            validate_registry_document(json.dumps(unsupported_model))

    def test_schema_and_runtime_reject_shared_format_and_semantic_probes(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )
        mutations = benchmark_registry_contract_mutations(self.seed_payload())
        self.assertIn("phase-2", schema["$comment"])
        with tempfile.TemporaryDirectory() as tmp:
            for name in (
                "calendar-date",
                "credential-url",
                "whitespace-url",
                "missing-host-url",
                "semantic-duplicate-observation",
            ):
                payload = mutations[name]
                with self.subTest(name=name, layer="draft-format"):
                    if name == "semantic-duplicate-observation":
                        self.assertTrue(validator.is_valid(payload))
                    else:
                        self.assertFalse(validator.is_valid(payload))
                with self.subTest(name=name, layer="official-two-phase"):
                    with self.assertRaises(RegistryError):
                        validate_registry_document(
                            json.dumps(payload),
                            require_schema=True,
                        )
                with self.subTest(name=name, layer="runtime"):
                    with self.assertRaises(RegistryError):
                        BenchmarkRegistry.load(
                            self.write_payload(tmp, payload, name + ".json")
                        )

    def test_model_effort_and_topology_contract_rejects_impossible_evidence(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )
        mutations = benchmark_registry_contract_mutations(self.seed_payload())
        with tempfile.TemporaryDirectory() as tmp:
            for name in (
                "unsupported-model-effort",
                "ultra-single",
                "non-ultra-multi",
                "declared-unspecified",
            ):
                payload = mutations[name]
                with self.subTest(name=name, layer="static-schema"):
                    if name == "unsupported-model-effort":
                        self.assertTrue(validator.is_valid(payload))
                    else:
                        self.assertFalse(validator.is_valid(payload))
                with self.subTest(name=name, layer="official-dynamic-schema"):
                    with self.assertRaises(RegistryError):
                        validate_registry_document(
                            json.dumps(payload),
                            require_schema=True,
                        )
                with self.subTest(name=name, layer="runtime"):
                    with self.assertRaises(RegistryError):
                        BenchmarkRegistry.load(
                            self.write_payload(tmp, payload, name + ".json")
                        )

    def test_model_catalog_constraints_apply_only_to_active_evidence(self):
        payload = self.seed_payload()
        source_id = payload["observations"][0]["source_id"]
        for source in payload["sources"]:
            if source["id"] == source_id:
                source["status"] = "retired"
        payload["observations"][0].update(
            {"model": "historical-model", "effort": "low", "topology": "single"}
        )
        validate_registry_document(json.dumps(payload))
        with tempfile.TemporaryDirectory() as tmp:
            BenchmarkRegistry.load(self.write_payload(tmp, payload))

    def test_registry_containers_are_immutable_defensive_copies(self):
        metric_payload = {"name": "task-completed-correctly", "value": 1, "unit": "percent"}
        metric = ScalarMeasurement(**metric_payload)
        tags = ["operations"]
        observation = BenchmarkObservation(
            id="test-observation",
            source_id="automation-bench",
            benchmark="AutomationBench",
            dataset="held-out-private",
            harness="Zapier agent harness",
            route=self._route("gpt-5.6-luna", "low"),
            topology="single",
            profile_tags=tags,
            metric=metric,
            cost=None,
            evaluated_at=None,
        )
        tags.append("research")
        self.assertEqual(("operations",), observation.profile_tags)
        with self.assertRaises(FrozenInstanceError):
            observation.id = "changed"
        with self.assertRaises(FrozenInstanceError):
            metric.value = 2

    @staticmethod
    def _route(model, effort):
        from model_router.contracts import Effort, Route

        return Route(model, Effort(effort))

    def test_invalid_candidate_preserves_last_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            before = runtime.benchmark_registry_path.read_bytes()
            candidate = Path(tmp, "candidate.json")
            candidate.write_text('{"schema_version":"broken"}', encoding="utf-8")
            with self.assertRaises(RegistryError):
                promote_candidate(candidate, runtime.benchmark_registry_path)
            self.assertEqual(before, runtime.benchmark_registry_path.read_bytes())
            self.assertEqual([], list(runtime.registry_dir.glob("*.new-*")))

    def test_replace_failure_preserves_snapshot_and_cleans_temporary(self):
        import model_router.registry as registry_module

        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            before = runtime.benchmark_registry_path.read_bytes()
            real_replace = registry_module.replace_file

            def fail_destination_replace(source, destination, **kwargs):
                if Path(destination) == runtime.benchmark_registry_path:
                    raise OSError("replace failure")
                return real_replace(source, destination, **kwargs)

            with mock.patch(
                "model_router.registry.replace_file",
                side_effect=fail_destination_replace,
            ):
                with self.assertRaises(RegistryError):
                    promote_candidate(SEED, runtime.benchmark_registry_path)
            self.assertEqual(before, runtime.benchmark_registry_path.read_bytes())
            self.assertEqual([], list(runtime.registry_dir.glob("*.new-*")))

    def test_promotion_pre_replace_stage_failures_preserve_snapshot(self):
        import model_router.registry as registry_module

        real_prepare = registry_module._prepare_temporary
        phase_payloads = {
            b"mutation-in-progress\n",
            b"outcome-confirmed\n",
            b"commit-outcome-unknown\n",
        }
        stages = ["write", "file-fsync"]
        if POSIX:
            stages.insert(0, "fchmod")
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
                runtime.bootstrap()
                before = runtime.benchmark_registry_path.read_bytes()

                def fail_only_candidate_prepare(destination, payload):
                    if payload in phase_payloads:
                        return real_prepare(destination, payload)
                    if stage == "fchmod":
                        target = "model_router.registry.os.fchmod"
                    elif stage == "write":
                        target = "model_router.registry._write_all"
                    else:
                        target = "model_router.registry._fsync_file"
                    with mock.patch(
                        target,
                        side_effect=OSError(stage + " failure"),
                    ):
                        return real_prepare(destination, payload)

                with mock.patch(
                    "model_router.registry._prepare_temporary",
                    side_effect=fail_only_candidate_prepare,
                ):
                    with self.assertRaises(RegistryError):
                        promote_candidate(SEED, runtime.benchmark_registry_path)
                self.assertEqual(
                    before,
                    runtime.benchmark_registry_path.read_bytes(),
                )
                self.assertEqual(
                    [],
                    list(runtime.registry_dir.glob("*.new-*"))
                    + list(runtime.registry_dir.glob("*.backup-*")),
                )
                self.assertEqual(
                    b"outcome-confirmed\n",
                    Path(
                        str(runtime.benchmark_registry_path)
                        + ".commit-outcome-unknown"
                    ).read_bytes(),
                )

    def test_directory_fsync_failure_rolls_back_registry_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            before = runtime.benchmark_registry_path.read_bytes()
            candidate_payload = self.seed_payload()
            candidate_payload["verified_at"] = "2026-07-17"
            candidate = self.write_payload(tmp, candidate_payload, "candidate.json")
            with mock.patch(
                "model_router.registry._fsync_directory",
                side_effect=[None, None, OSError("commit fsync"), None, None],
            ):
                with self.assertRaises(RegistryError):
                    promote_candidate(candidate, runtime.benchmark_registry_path)
            self.assertEqual(before, runtime.benchmark_registry_path.read_bytes())
            self.assertEqual(
                [],
                list(runtime.registry_dir.glob("*.new-*"))
                + list(runtime.registry_dir.glob("*.backup-*")),
            )

    def test_unknown_registry_commit_outcome_blocks_subsequent_promotions(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            before = runtime.benchmark_registry_path.read_bytes()
            candidate_payload = self.seed_payload()
            candidate_payload["verified_at"] = "2026-07-17"
            candidate = self.write_payload(tmp, candidate_payload, "candidate.json")
            calls = 0

            def fail_commit_and_rollback(_path):
                nonlocal calls
                calls += 1
                if calls <= 2:
                    return None
                raise OSError("directory durability unavailable")

            with mock.patch(
                "model_router.registry._fsync_directory",
                side_effect=fail_commit_and_rollback,
            ):
                with self.assertRaisesRegex(
                    RegistryError,
                    "commit-outcome-unknown",
                ):
                    promote_candidate(candidate, runtime.benchmark_registry_path)
            self.assertEqual(before, runtime.benchmark_registry_path.read_bytes())
            self.assertTrue(
                Path(
                    str(runtime.benchmark_registry_path)
                    + ".commit-outcome-unknown"
                ).is_file()
            )
            self.assertEqual([], list(runtime.registry_dir.glob("*.new-*")))
            with self.assertRaisesRegex(
                RegistryError,
                "commit-outcome-unknown",
            ):
                promote_candidate(candidate, runtime.benchmark_registry_path)

    def test_registry_commit_requires_a_durable_known_outcome_witness(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            candidate_payload = self.seed_payload()
            candidate_payload["verified_at"] = "2026-07-17"
            candidate = self.write_payload(
                tmp,
                candidate_payload,
                "candidate.json",
            )
            guard = Path(
                str(runtime.benchmark_registry_path)
                + ".commit-outcome-unknown"
            )

            with mock.patch(
                "model_router.registry._mark_mutation_guard_confirmed",
                side_effect=OSError("phase transition failure"),
            ), mock.patch(
                "model_router.registry._clear_mutation_guard",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    RegistryError,
                    "commit-outcome-unknown",
                ):
                    promote_candidate(
                        candidate,
                        runtime.benchmark_registry_path,
                    )

            self.assertEqual(
                "2026-07-17",
                BenchmarkRegistry.load(
                    runtime.benchmark_registry_path
                ).verified_at,
            )
            self.assertEqual(b"commit-outcome-unknown\n", guard.read_bytes())
            with self.assertRaisesRegex(
                RegistryError,
                "commit-outcome-unknown",
            ):
                promote_candidate(candidate, runtime.benchmark_registry_path)

    def test_registry_phase_failure_is_unknown_and_confirmed_phase_is_durable(self):
        with self.subTest(confirmation="phase-failed"), tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            candidate_payload = self.seed_payload()
            candidate_payload["verified_at"] = "2026-07-17"
            candidate = self.write_payload(
                tmp,
                candidate_payload,
                "cleanup-candidate.json",
            )
            guard = Path(
                str(runtime.benchmark_registry_path)
                + ".commit-outcome-unknown"
            )
            with mock.patch(
                "model_router.registry._mark_mutation_guard_confirmed",
                side_effect=OSError("phase transition failure"),
            ):
                with self.assertRaisesRegex(
                    RegistryError,
                    "commit-outcome-unknown",
                ):
                    promote_candidate(
                        candidate,
                        runtime.benchmark_registry_path,
                    )
            self.assertEqual(
                "2026-07-17",
                BenchmarkRegistry.load(
                    runtime.benchmark_registry_path
                ).verified_at,
            )
            self.assertEqual(b"commit-outcome-unknown\n", guard.read_bytes())
            with self.assertRaisesRegex(
                RegistryError,
                "commit-outcome-unknown",
            ):
                promote_candidate(candidate, runtime.benchmark_registry_path)

        with self.subTest(confirmation="phase"), tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            candidate_payload = self.seed_payload()
            candidate_payload["verified_at"] = "2026-07-17"
            candidate = self.write_payload(
                tmp,
                candidate_payload,
                "phase-candidate.json",
            )
            guard = Path(
                str(runtime.benchmark_registry_path)
                + ".commit-outcome-unknown"
            )
            with mock.patch(
                "model_router.registry._clear_mutation_guard",
                return_value=False,
            ):
                promote_candidate(candidate, runtime.benchmark_registry_path)
            self.assertEqual(
                "2026-07-17",
                BenchmarkRegistry.load(
                    runtime.benchmark_registry_path
                ).verified_at,
            )
            self.assertTrue(guard.exists())
            self.assertEqual(b"outcome-confirmed\n", guard.read_bytes())

            promote_candidate(candidate, runtime.benchmark_registry_path)
            self.assertTrue(guard.exists())
            self.assertEqual(b"outcome-confirmed\n", guard.read_bytes())

    def test_absent_registry_witness_still_requires_directory_durability(self):
        import model_router.registry as registry_module

        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp, "absent-registry-witness")
            with mock.patch(
                "model_router.registry._fsync_directory",
                side_effect=OSError("directory durability unavailable"),
            ):
                self.assertFalse(
                    registry_module._clear_mutation_guard(absent)
                )

    def test_registry_restores_pending_witness_when_unknown_marker_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            candidate_payload = self.seed_payload()
            candidate_payload["verified_at"] = "2026-07-17"
            candidate = self.write_payload(
                tmp,
                candidate_payload,
                "unknown-marker-failure.json",
            )
            guard = Path(
                str(runtime.benchmark_registry_path)
                + ".commit-outcome-unknown"
            )

            with mock.patch(
                "model_router.registry._mark_mutation_guard_confirmed",
                side_effect=OSError("phase transition failure"),
            ), mock.patch(
                "model_router.registry._mark_commit_outcome_unknown",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    RegistryError,
                    "commit-outcome-unknown",
                ):
                    promote_candidate(
                        candidate,
                        runtime.benchmark_registry_path,
                    )

            self.assertTrue(guard.exists())
            self.assertEqual(b"mutation-in-progress\n", guard.read_bytes())
            with self.assertRaisesRegex(
                RegistryError,
                "commit-outcome-unknown",
            ):
                promote_candidate(candidate, runtime.benchmark_registry_path)

    @unittest.skipUnless(FILE_SYMLINK_AVAILABLE, "file symlinks unavailable")
    def test_promotion_rejects_symlink_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "target.json")
            target.write_bytes(SEED.read_bytes())
            link = Path(tmp, "link.json")
            link.symlink_to(target)
            with self.assertRaises(RegistryError):
                promote_candidate(SEED, link)
            self.assertEqual(SEED.read_bytes(), target.read_bytes())

    @unittest.skipUnless(FILE_SYMLINK_AVAILABLE, "file symlinks unavailable")
    def test_promotion_rejects_symlink_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp, "candidate.json")
            candidate.symlink_to(SEED)
            destination = Path(tmp, "destination.json")
            with self.assertRaises(RegistryError):
                promote_candidate(candidate, destination)
            self.assertFalse(destination.exists())

    def test_errors_do_not_leak_hostile_paths_or_chain_content(self):
        secret = "client-secret-directory"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, secret, "registry.json")
            with self.assertRaises(RegistryError) as raised:
                BenchmarkRegistry.load(path)
        rendered = "".join(
            traceback.format_exception(
                type(raised.exception),
                raised.exception,
                raised.exception.__traceback__,
            )
        )
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, rendered)

    def test_hostile_iterable_exceptions_are_chained_without_secret_content(self):
        secret = "sensitive-iterable-content"

        class ExplodingIterable:
            def __iter__(self):
                raise RuntimeError(secret)

        with self.assertRaises(RegistryError) as raised:
            BenchmarkSource(
                id="source",
                owner="Owner",
                url="https://example.test/source",
                status="active",
                kind="vendor",
                limitations=ExplodingIterable(),
            )
        rendered = "".join(
            traceback.format_exception(
                type(raised.exception),
                raised.exception,
                raised.exception.__traceback__,
            )
        )
        self.assertNotIn(secret, rendered)


class RuntimeStateTests(unittest.TestCase):
    def test_state_dataclasses_preserve_the_planned_constructor_contract(self):
        route = BenchmarkRegistryTests._route("gpt-5.6-luna", "low")
        observation = Observation(
            project_path="/project",
            profile_id="software",
            profile_version="1.0.0",
            archetype="bounded-change",
            route=route,
            model_version="gpt-5.6-luna-2026-07-09",
            engine_version="0.1.0",
            duration_seconds=1.0,
            input_tokens=1,
            output_tokens=1,
            validation_status="pass",
            metrics={},
            failure_class=None,
            escalations=0,
            stop_reason="approved",
        )
        self.assertIsNone(observation.observed_cost_usd)
        stored = StoredObservation(
            project_hash="a" * 64,
            profile_id="software",
            profile_version="1.0.0",
            archetype="bounded-change",
            route=route,
            model_version="gpt-5.6-luna-2026-07-09",
            engine_version="0.1.0",
            duration_seconds=1.0,
            input_tokens=1,
            output_tokens=1,
            validation_status="pass",
            metrics={},
            failure_class=None,
        )
        self.assertIsNone(stored.observed_cost_usd)
        self.assertEqual(0, stored.escalations)
        self.assertIsNone(stored.stop_reason)

    def test_bootstrap_creates_private_valid_snapshots_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            self.assertEqual(
                52,
                len(BenchmarkRegistry.load(runtime.benchmark_registry_path).observations),
            )
            self.assertTrue(runtime.model_registry_path.is_file())
            before = runtime.model_registry_path.read_bytes()
            runtime.bootstrap()
            self.assertEqual(before, runtime.model_registry_path.read_bytes())
            if os.name == "posix":
                for directory in (
                    runtime.registry_dir,
                    runtime.runs_dir,
                    runtime.telemetry_dir,
                ):
                    self.assertEqual(
                        stat.S_IRWXU,
                        stat.S_IMODE(directory.stat().st_mode),
                    )

    def test_telemetry_uses_an_exact_allowlist_and_omits_sensitive_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            runtime.append_observation(
                observation_fixture(project_path="/secret/client")
            )
            line = runtime.observations_path.read_text(encoding="utf-8")
            payload = json.loads(line)
            self.assertEqual(SAFE_OBSERVATION_FIELDS, frozenset(payload))
            self.assertNotIn("/secret/client", line)
            self.assertNotIn("prompt", line.lower())
            self.assertNotIn("response", line.lower())
            self.assertIn("project_hash", line)

    def test_project_hash_is_salted_stable_and_runtime_local(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            one = RuntimeState(Path(first), seed_root=REFERENCES)
            two = RuntimeState(Path(second), seed_root=REFERENCES)
            one.bootstrap()
            two.bootstrap()
            self.assertEqual(one.project_hash("/client/project"), one.project_hash("/client/project"))
            self.assertNotEqual(one.project_hash("/client/project"), two.project_hash("/client/project"))
            self.assertEqual(64, len(one.project_hash("/client/project")))

    def test_observation_copies_metrics_and_is_frozen(self):
        metrics = {"required_checks_passed": 1.0}
        observation = observation_fixture(metrics=metrics)
        metrics["required_checks_passed"] = 0.0
        self.assertEqual(1.0, observation.metrics["required_checks_passed"])
        with self.assertRaises(TypeError):
            observation.metrics["new"] = 1.0
        with self.assertRaises(FrozenInstanceError):
            observation.input_tokens = 0

    def test_observation_rejects_invalid_numeric_and_enum_values(self):
        cases = (
            {"duration_seconds": True},
            {"duration_seconds": math.inf},
            {"input_tokens": 1.0},
            {"output_tokens": -1},
            {"observed_cost_usd": math.nan},
            {"validation_status": "unknown"},
            {"failure_class": "mystery"},
            {"escalations": True},
            {"metrics": {"metric": False}},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises((StateError, ValueError)):
                    observation_fixture(**overrides)

    def test_structural_labels_cannot_smuggle_freeform_content(self):
        for overrides in (
            {"profile_id": "copy the full prompt"},
            {"archetype": "/secret/client"},
            {"model_version": "response: confidential"},
            {"stop_reason": "the user said confidential text"},
            {"metrics": {"raw response": 1.0}},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(StateError):
                    observation_fixture(**overrides)

    def test_concurrent_append_produces_complete_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            errors = []

            def append(index):
                try:
                    runtime.append_observation(
                        observation_fixture(
                            archetype="bounded-change-%d" % index,
                        )
                    )
                except Exception as error:
                    errors.append(error)

            threads = [threading.Thread(target=append, args=(index,)) for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual([], errors)
            lines = runtime.observations_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(20, len(lines))
            self.assertTrue(all(type(json.loads(line)) is dict for line in lines))

    def test_local_observations_ignore_only_an_incomplete_final_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            runtime.append_observation(observation_fixture(project_path="/project/a"))
            with runtime.observations_path.open("ab") as handle:
                handle.write(b'{"project_hash":"partial')
            stored = runtime.local_observations("/project/a")
            self.assertEqual(1, len(stored))
            self.assertEqual("gpt-5.6-terra:medium:single", stored[0].route.key)

    def test_local_observations_reject_corrupt_complete_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            runtime.observations_path.write_text("{broken}\n", encoding="utf-8")
            with self.assertRaises(StateError):
                runtime.local_observations("/project/a")

    def test_write_decision_is_atomic_private_and_rejects_hostile_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            path = runtime.write_decision(
                "run-001",
                _complete_decision(),
            )
            stored = json.loads(path.read_text(encoding="utf-8"))
            integrity = stored.pop("integrity")
            self.assertEqual("hmac-sha256", integrity["algorithm"])
            self.assertRegex(integrity["tag"], r"^[0-9a-f]{64}$")
            self.assertEqual("stopped", stored["status"])
            self.assertEqual(
                "gpt-5.6-terra:medium:single",
                stored["selection"]["ideal"],
            )
            self.assertEqual([], list(path.parent.glob("*.new-*")))
            if os.name == "posix":
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            for run_id in ("../escape", ".", "..", "run/name", "", " run"):
                with self.subTest(run_id=run_id):
                    with self.assertRaises(StateError):
                        runtime.write_decision(run_id, {"selection": {}})
            with self.assertRaises(StateError):
                runtime.write_decision("run-002", {"prompt": "sensitive"})
            with self.assertRaises(StateError):
                runtime.write_decision(
                    "run-003",
                    {"execution": {"response": "sensitive"}},
                )
            with self.assertRaises(StateError):
                runtime.write_decision(
                    "run-004",
                    {"fingerprint": {"project": "/secret/client"}},
                )

    def test_read_decision_rejects_incomplete_envelopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            payload = {
                "schema_version": "1.0.0",
                "selection": {"ideal": "gpt-5.6-terra:medium:single"},
            }
            with self.assertRaises(StateError):
                runtime.write_decision("round-trip", payload)
            self.assertFalse((runtime.runs_dir / "round-trip").exists())

    def test_concurrent_key_creation_is_single_and_keeps_racing_decision_auditable(self):
        import model_router.state as state_module
        from support import complete_decision_payload

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runtime"
            initial = RuntimeState(root, seed_root=REFERENCES)
            initial.bootstrap()
            initial.decision_key_path.unlink()
            state_a = RuntimeState(root, seed_root=REFERENCES)
            state_b = RuntimeState(root, seed_root=REFERENCES)
            key_path = state_a.decision_key_path
            barrier = threading.Barrier(2)
            exists_calls = 0
            exists_lock = threading.Lock()
            release_b = threading.Event()
            a_done = threading.Event()
            errors = []
            real_exists = Path.exists
            real_atomic = state_module._atomic_write_bytes
            real_tokens = state_module.secrets.token_bytes

            def synchronized_exists(path):
                nonlocal exists_calls
                if Path(path) == key_path:
                    with exists_lock:
                        exists_calls += 1
                        current = exists_calls
                    if current <= 2:
                        barrier.wait(timeout=5)
                        return False
                return real_exists(path)

            def deterministic_tokens(size):
                if size == 32 and threading.current_thread().name in ("key-a", "key-b"):
                    label = threading.current_thread().name[-1].encode("ascii")
                    return label * size
                return real_tokens(size)

            def delayed_atomic(path, payload, mode=0o600):
                if Path(path) == key_path and threading.current_thread().name == "key-b":
                    if not release_b.wait(timeout=5):
                        raise AssertionError("key-b was not released")
                return real_atomic(path, payload, mode)

            def bootstrap(label, runtime):
                try:
                    runtime.bootstrap()
                except Exception as error:
                    errors.append(error)
                finally:
                    if label == "a":
                        a_done.set()

            with mock.patch.object(Path, "exists", synchronized_exists), mock.patch.object(
                state_module.secrets, "token_bytes", deterministic_tokens
            ), mock.patch.object(state_module, "_atomic_write_bytes", delayed_atomic):
                thread_b = threading.Thread(
                    target=bootstrap,
                    args=("b", state_b),
                    name="key-b",
                )
                thread_a = threading.Thread(
                    target=bootstrap,
                    args=("a", state_a),
                    name="key-a",
                )
                thread_b.start()
                thread_a.start()
                self.assertTrue(a_done.wait(timeout=5))
                decision = state_a.write_decision(
                    "concurrent-key",
                    complete_decision_payload(),
                )
                release_b.set()
                thread_a.join(timeout=5)
                thread_b.join(timeout=5)

            self.assertFalse(thread_a.is_alive())
            self.assertFalse(thread_b.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(
                state_a._ensure_decision_key(),
                state_b._ensure_decision_key(),
            )
            self.assertEqual(
                "stopped",
                state_a.read_decision(decision.parent.name)["status"],
            )

    @unittest.skipUnless(POSIX, "POSIX hard-link key publication only")
    def test_key_publication_fault_never_exposes_a_partial_destination(self):
        import model_router.state as state_module

        real_link = state_module.os.link
        for phase in ("before-publication", "after-publication"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "runtime"
                runtime = RuntimeState(root, seed_root=REFERENCES)
                runtime.bootstrap()
                runtime.decision_key_path.unlink()

                def fail_publication(source, destination, **kwargs):
                    if phase == "after-publication":
                        real_link(source, destination, **kwargs)
                    raise OSError(phase)

                with mock.patch.object(
                    state_module.os,
                    "link",
                    side_effect=fail_publication,
                ), self.assertRaises(StateError):
                    runtime.bootstrap()

                if phase == "before-publication":
                    self.assertFalse(runtime.decision_key_path.exists())
                    published_key = None
                else:
                    published_key = runtime.decision_key_path.read_bytes()
                    self.assertEqual(32, len(published_key))

                fresh = RuntimeState(root, seed_root=REFERENCES)
                fresh.bootstrap()
                recovered_key = fresh.decision_key_path.read_bytes()
                self.assertEqual(32, len(recovered_key))
                if published_key is not None:
                    self.assertEqual(published_key, recovered_key)
                self.assertEqual(
                    [],
                    list(root.glob(".decision-hmac-key.tmp-*")),
                )

    def test_bootstrap_recovers_an_orphaned_key_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runtime"
            runtime = RuntimeState(root, seed_root=REFERENCES)
            runtime.bootstrap()
            runtime.decision_key_path.unlink()
            orphan = root / ".decision-hmac-key.tmp-orphan"
            orphan.write_bytes(b"partial")
            os.chmod(orphan, 0o600)

            fresh = RuntimeState(root, seed_root=REFERENCES)
            fresh.bootstrap()

            self.assertFalse(orphan.exists())
            self.assertEqual(32, len(fresh.decision_key_path.read_bytes()))

    @unittest.skipUnless(POSIX, "POSIX descriptor durability only")
    def test_key_publication_observes_file_and_root_fsync_order(self):
        import model_router.state as state_module

        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp) / "runtime", seed_root=REFERENCES)
            runtime.bootstrap()
            runtime.decision_key_path.unlink()
            events = []
            real_fsync = state_module.os.fsync
            real_link = state_module.os.link
            real_unlink = state_module.os.unlink

            def observe_fsync(descriptor):
                metadata = os.fstat(descriptor)
                kind = "file-fsync" if stat.S_ISREG(metadata.st_mode) else "root-fsync"
                events.append((kind, descriptor, metadata.st_size))
                return real_fsync(descriptor)

            def observe_link(source, destination, **kwargs):
                events.append(("link", source, destination, dict(kwargs)))
                return real_link(source, destination, **kwargs)

            def observe_unlink(name, *args, **kwargs):
                if str(name).startswith(".decision-hmac-key.tmp-"):
                    events.append(("unlink-temp", name, dict(kwargs)))
                return real_unlink(name, *args, **kwargs)

            with mock.patch.object(
                state_module.os,
                "fsync",
                side_effect=observe_fsync,
            ), mock.patch.object(
                state_module.os,
                "link",
                side_effect=observe_link,
            ), mock.patch.object(
                state_module.os,
                "unlink",
                side_effect=observe_unlink,
            ):
                runtime.bootstrap()

            kinds = [event[0] for event in events]
            self.assertEqual(
                [
                    "file-fsync",
                    "link",
                    "root-fsync",
                    "unlink-temp",
                    "root-fsync",
                ],
                kinds[:5],
            )
            self.assertEqual(32, events[0][2])
            link = events[1]
            self.assertTrue(link[1].startswith(".decision-hmac-key.tmp-"))
            self.assertEqual(".decision-hmac-key", link[2])
            self.assertEqual(link[3]["src_dir_fd"], link[3]["dst_dir_fd"])
            self.assertFalse(link[3]["follow_symlinks"])
            self.assertEqual(link[3]["src_dir_fd"], events[2][1])
            self.assertEqual(link[3]["src_dir_fd"], events[4][1])
            self.assertEqual(link[3]["src_dir_fd"], events[3][2]["dir_fd"])

    def test_staged_write_and_file_fsync_faults_never_publish_key(self):
        import model_router.state as state_module

        for boundary in ("stage-write", "stage-fsync"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                runtime = RuntimeState(Path(tmp) / "runtime", seed_root=REFERENCES)
                runtime.bootstrap()
                runtime.decision_key_path.unlink()
                if boundary == "stage-write":
                    def fail_stage_write(descriptor, payload):
                        os.write(descriptor, payload[:5])
                        raise OSError("stage write interrupted")

                    patcher = mock.patch.object(
                        state_module,
                        "_write_all",
                        side_effect=fail_stage_write,
                    )
                else:
                    patcher = mock.patch.object(
                        state_module,
                        "_fsync_file",
                        side_effect=OSError("stage fsync failed"),
                    )
                with patcher, self.assertRaises(StateError):
                    runtime.bootstrap()

                self.assertFalse(runtime.decision_key_path.exists())
                self.assertEqual(
                    [],
                    list(runtime.root.glob(".decision-hmac-key.tmp-*")),
                )
                runtime.bootstrap()
                self.assertEqual(32, len(runtime.decision_key_path.read_bytes()))

    @unittest.skipUnless(POSIX, "POSIX descriptor durability only")
    def test_root_fsync_faults_after_link_and_unlink_preserve_published_key(self):
        import model_router.state as state_module

        for root_boundary in (1, 2):
            with self.subTest(root_boundary=root_boundary), tempfile.TemporaryDirectory() as tmp:
                runtime = RuntimeState(Path(tmp) / "runtime", seed_root=REFERENCES)
                runtime.bootstrap()
                runtime.decision_key_path.unlink()
                real_fsync = state_module.os.fsync
                root_calls = 0

                def fail_selected_root_fsync(descriptor):
                    nonlocal root_calls
                    metadata = os.fstat(descriptor)
                    if stat.S_ISDIR(metadata.st_mode):
                        root_calls += 1
                        if root_calls == root_boundary:
                            raise OSError("root fsync failed")
                    return real_fsync(descriptor)

                with mock.patch.object(
                    state_module.os,
                    "fsync",
                    side_effect=fail_selected_root_fsync,
                ), self.assertRaises(StateError):
                    runtime.bootstrap()

                published = runtime.decision_key_path.read_bytes()
                self.assertEqual(32, len(published))
                fresh = RuntimeState(runtime.root, seed_root=REFERENCES)
                fresh.bootstrap()
                self.assertEqual(published, fresh.decision_key_path.read_bytes())
                self.assertEqual(
                    [],
                    list(runtime.root.glob(".decision-hmac-key.tmp-*")),
                )

    def test_unlink_faults_leave_only_recoverable_reserved_orphans(self):
        import model_router.state as state_module

        for persistent in (False, True):
            with self.subTest(persistent=persistent), tempfile.TemporaryDirectory() as tmp:
                runtime = RuntimeState(Path(tmp) / "runtime", seed_root=REFERENCES)
                runtime.bootstrap()
                runtime.decision_key_path.unlink()
                unrelated = runtime.root / "keep.txt"
                unrelated.write_bytes(b"keep")
                real_unlink = state_module.os.unlink
                failures = 0

                def fail_temporary_unlink(name, *args, **kwargs):
                    nonlocal failures
                    if str(name).startswith(".decision-hmac-key.tmp-"):
                        failures += 1
                        if persistent or failures == 1:
                            raise OSError("temporary unlink failed")
                    return real_unlink(name, *args, **kwargs)

                with mock.patch.object(
                    state_module.os,
                    "unlink",
                    side_effect=fail_temporary_unlink,
                ), self.assertRaises(StateError):
                    runtime.bootstrap()

                published = runtime.decision_key_path.read_bytes()
                self.assertEqual(32, len(published))
                expected_orphans = 1 if persistent else 0
                self.assertEqual(
                    expected_orphans,
                    len(list(runtime.root.glob(".decision-hmac-key.tmp-*"))),
                )
                fresh = RuntimeState(runtime.root, seed_root=REFERENCES)
                fresh.bootstrap()
                self.assertEqual(published, fresh.decision_key_path.read_bytes())
                self.assertEqual(b"keep", unrelated.read_bytes())
                self.assertEqual(
                    [],
                    list(runtime.root.glob(".decision-hmac-key.tmp-*")),
                )

    @unittest.skipUnless(FILE_SYMLINK_AVAILABLE, "file symlinks unavailable")
    def test_orphan_cleanup_runs_under_key_lock_and_preserves_unrelated_targets(self):
        import model_router.state as state_module

        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp) / "runtime", seed_root=REFERENCES)
            runtime.bootstrap()
            final_key = runtime.decision_key_path.read_bytes()
            unrelated = runtime.root / "keep.txt"
            unrelated.write_bytes(b"keep")
            external = Path(tmp) / "external"
            external.write_bytes(b"external")
            orphan = runtime.root / ".decision-hmac-key.tmp-file"
            orphan.write_bytes(b"partial")
            orphan.chmod(0o600)
            orphan_link = runtime.root / ".decision-hmac-key.tmp-link"
            orphan_link.symlink_to(external)
            lock_identity = (
                runtime.decision_key_lock_path.stat().st_dev,
                runtime.decision_key_lock_path.stat().st_ino,
            )
            lock_held = False
            removed = []
            real_flock = state_module.fcntl.flock
            real_unlink = state_module.os.unlink

            def observe_flock(descriptor, operation):
                nonlocal lock_held
                metadata = os.fstat(descriptor)
                is_key_lock = (metadata.st_dev, metadata.st_ino) == lock_identity
                result = real_flock(descriptor, operation)
                if is_key_lock and operation & state_module.fcntl.LOCK_EX:
                    lock_held = True
                elif is_key_lock and operation & state_module.fcntl.LOCK_UN:
                    lock_held = False
                return result

            def observe_unlink(name, *args, **kwargs):
                if str(name).startswith(".decision-hmac-key.tmp-"):
                    self.assertTrue(lock_held)
                    removed.append(str(name))
                return real_unlink(name, *args, **kwargs)

            with mock.patch.object(
                state_module.fcntl,
                "flock",
                side_effect=observe_flock,
            ), mock.patch.object(
                state_module.os,
                "unlink",
                side_effect=observe_unlink,
            ):
                self.assertEqual(final_key, runtime._ensure_decision_key())

            self.assertEqual(
                {orphan.name, orphan_link.name},
                set(removed),
            )
            self.assertFalse(orphan.exists())
            self.assertFalse(orphan_link.exists())
            self.assertEqual(final_key, runtime.decision_key_path.read_bytes())
            self.assertEqual(b"keep", unrelated.read_bytes())
            self.assertEqual(b"external", external.read_bytes())

    @unittest.skipUnless(POSIX, "POSIX ownership and modes only")
    def test_audit_rejects_integrity_key_that_lost_private_mode(self):
        import model_router.state as state_module
        from support import passing_report, run_args, service_fixture

        service = service_fixture(child_reports=(passing_report(),))
        result = service.run(**run_args())
        original_key = service.state.decision_key_path.read_bytes()
        os.chmod(str(service.state.decision_key_path), 0o644)

        with self.assertRaises(StateError):
            service.state.read_decision(result.decision_path.parent.name)

        service.state.bootstrap()
        self.assertEqual(original_key, service.state.decision_key_path.read_bytes())
        self.assertEqual(
            0o600,
            stat.S_IMODE(service.state.decision_key_path.stat().st_mode),
        )
        self.assertEqual(
            "pass",
            service.state.read_decision(result.decision_path.parent.name)["status"],
        )
        if hasattr(os, "getuid"):
            with mock.patch.object(
                state_module.os,
                "getuid",
                return_value=os.getuid() + 1,
            ), self.assertRaises(StateError):
                service.state.read_decision(result.decision_path.parent.name)

    @unittest.skipUnless(DIRECTORY_SYMLINK_AVAILABLE, "directory symlinks unavailable")
    def test_read_decision_rejects_hostile_ids_symlinks_and_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            payload = _complete_decision()
            target = runtime.write_decision("read-target", payload)

            for run_id in ("../escape", ".", "..", "run/name", "", " run"):
                with self.subTest(run_id=run_id):
                    with self.assertRaises(StateError):
                        runtime.read_decision(run_id)

            (runtime.runs_dir / "read-link").symlink_to(target.parent)
            with self.assertRaises(StateError):
                runtime.read_decision("read-link")

            target.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "selection": {"ideal": "route"},
                        "prompt": "PRIVATE SENTENCE",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(StateError):
                runtime.read_decision("read-target")

    def test_decision_accepts_only_the_new_terminal_reason_codes(self):
        terminal_codes = (
            "executor-technical-failure",
            "validator-technical-failure",
            "verifier-budget-exhausted",
            "verifier-provenance-invalid",
            "partial-mutation-recovery-failed",
            "partial-mutation-recovery-invalid",
            "partial-mutation-recovery-verified",
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            for index, reason in enumerate(terminal_codes):
                with self.subTest(reason=reason):
                    stored = runtime.write_decision(
                        "terminal-reason-%d" % index,
                        _complete_decision({
                            "status": "blocked",
                            "stop_reason": reason,
                            "validation": {"stop_reason": reason},
                        }),
                    )
                    raw = json.loads(stored.read_text(encoding="utf-8"))
                    self.assertEqual(reason, raw["stop_reason"])

            for index, reason in enumerate(("blocked", "no-measurable-gain", "private-cause")):
                with self.subTest(forbidden=reason):
                    with self.assertRaises(StateError):
                        runtime.write_decision(
                            "forbidden-reason-%d" % index,
                            _complete_decision({
                                "status": "blocked",
                                "stop_reason": reason,
                                "validation": {"stop_reason": reason},
                            }),
                        )

    def test_read_decision_rejects_coordinated_allowlisted_tampering(self):
        from support import passing_report, run_args, service_fixture

        service = service_fixture(child_reports=(passing_report(),))
        result = service.run(**run_args())
        original = result.decision_path.read_bytes()
        payload = json.loads(original.decode("utf-8"))
        payload["status"] = "blocked"
        payload["stop_reason"] = "user-stopped"
        payload["validation"]["status"] = "blocked"
        payload["validation"]["failure_class"] = "external_block"
        payload["validation"]["stop_reason"] = "user-stopped"
        result.decision_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(StateError):
            service.state.read_decision(result.decision_path.parent.name)

        result.decision_path.write_bytes(original + b" ")
        with self.assertRaises(StateError):
            service.state.read_decision(result.decision_path.parent.name)

    @unittest.skipUnless(DIRECTORY_SYMLINK_AVAILABLE, "directory symlinks unavailable")
    def test_read_decision_rejects_runtime_root_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "runtime"
            moved = parent / "moved-runtime"
            runtime = RuntimeState(root, seed_root=REFERENCES)
            runtime.bootstrap()
            runtime.write_decision(
                "root-swap",
                _complete_decision(),
            )
            root.rename(moved)
            root.symlink_to(moved, target_is_directory=True)

            with self.assertRaises(StateError):
                runtime.read_decision("root-swap")

    def test_decision_integrity_key_is_private_and_size_limit_is_exact(self):
        import model_router.state as state_module
        from support import passing_report, run_args, service_fixture

        service = service_fixture(child_reports=(passing_report(),))
        result = service.run(**run_args())
        key_path = service.state.root / ".decision-hmac-key"

        self.assertTrue(key_path.is_file())
        if os.name == "posix":
            self.assertEqual(0o600, stat.S_IMODE(key_path.stat().st_mode))
        self.assertNotIn(
            key_path.read_bytes(),
            result.decision_path.read_bytes(),
        )

        exact_size = result.decision_path.stat().st_size
        with mock.patch.object(
            state_module,
            "MAX_DECISION_BYTES",
            exact_size,
            create=True,
        ):
            service.state.read_decision(result.decision_path.parent.name)
        with mock.patch.object(
            state_module,
            "MAX_DECISION_BYTES",
            exact_size - 1,
            create=True,
        ):
            with self.assertRaises(StateError):
                service.state.read_decision(result.decision_path.parent.name)

    def test_decision_envelope_rejects_aliases_free_text_and_embedded_paths(self):
        probes = (
            {"audit_summary": "PROMPT_SENTINEL"},
            {"user_prompt_copy": "PROMPT_SENTINEL"},
            {"file content": "FILE_CONTENT_SENTINEL"},
            {"reasoning": "REASONING_SENTINEL"},
            {"requestFingerprint": {"archetype": "bounded-change"}},
            {
                "schema_version": "1.0.0",
                "selection": {
                    "ideal": "project /private/client/path",
                },
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            for index, fragment in enumerate(probes):
                with self.subTest(index=index):
                    with self.assertRaises(StateError):
                        runtime.write_decision(
                            "privacy-%d" % index,
                            _complete_decision(fragment),
                        )
            path = runtime.write_decision(
                "complete-compatible",
                _complete_decision(),
            )
            self.assertTrue(path.is_file())

    def test_decision_envelope_rejects_sentinels_in_structural_value_slots(self):
        payload = _complete_decision({
            "evidence_ids": ["PROMPT_SENTINEL"],
            "stop_reason": "REASONING_SENTINEL",
            "catalog": {"evidence_ids": ["FILE_CONTENT_SENTINEL"]},
        })
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            with self.assertRaises(StateError):
                runtime.write_decision("value-slot-sentinels", payload)
            rendered = "".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in runtime.runs_dir.glob("**/*")
                if path.is_file()
            )
            for sentinel in (
                "PROMPT_SENTINEL",
                "REASONING_SENTINEL",
                "FILE_CONTENT_SENTINEL",
            ):
                self.assertNotIn(sentinel, rendered)

    def test_decision_log_hashes_every_open_fingerprint_and_metric_slot(self):
        project_hash_secret = "0123456789abcdef" * 4
        open_values = (
            "promptsentinel",
            "confidential",
            "privateclientpath",
            "hunter2",
            "camelCase",
            "segredoç",
            "singleword",
        )
        payload = _complete_decision({
            "fingerprint": {
                "project_hash": project_hash_secret,
                "archetype": open_values[0],
                "required_tools": list(open_values[1:]),
                "validation_checks": [
                    {"id": open_values[3], "category": open_values[4], "required": True}
                ],
            },
            "catalog": {
                "model_registry_version": open_values[5],
                "benchmark_registry_version": open_values[6],
            },
            "validation": {"metrics": {open_values[1]: 1.0}},
        })
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            path = runtime.write_decision("hashed-open-slots", payload)
            raw = path.read_bytes()
            self.assertNotIn(project_hash_secret.encode("ascii"), raw)
            for value in open_values:
                self.assertNotIn(value.encode("utf-8"), raw)
            stored = json.loads(raw)
            self.assertRegex(
                stored["fingerprint"]["project_hash"],
                r"^sha256:project-hash:[0-9a-f]{64}$",
            )
            self.assertRegex(
                stored["fingerprint"]["archetype"],
                r"^sha256:archetype:[0-9a-f]{64}$",
            )
            self.assertTrue(
                all(
                    item.startswith("sha256:required-tool:")
                    for item in stored["fingerprint"]["required_tools"]
                )
            )
            check = stored["fingerprint"]["validation_checks"][0]
            self.assertRegex(check["id"], r"^sha256:validation-check-id:[0-9a-f]{64}$")
            self.assertRegex(
                check["category"],
                r"^sha256:validation-check-category:[0-9a-f]{64}$",
            )
            metric_name = next(iter(stored["validation"]["metrics"]))
            self.assertRegex(metric_name, r"^sha256:metric-name:[0-9a-f]{64}$")
            self.assertRegex(
                stored["catalog"]["model_registry_version"],
                r"^sha256:model-registry-version:[0-9a-f]{64}$",
            )
            self.assertRegex(
                stored["catalog"]["benchmark_registry_version"],
                r"^sha256:benchmark-registry-version:[0-9a-f]{64}$",
            )

    def test_decision_log_rejects_unknown_registered_profile_model_and_benchmark_ids(self):
        probes = (
            {"profile": {"id": "confidential", "version": "1.0.0"}},
            {"fingerprint": {"primary_profile": "privateclientpath"}},
            {"catalog": {"model_ids": ["hunter2"]}},
            {"catalog": {"benchmark_ids": ["promptsentinel"]}},
            {"selection": {"ideal": {"model": "confidential", "effort": "low"}}},
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            for index, fragment in enumerate(probes):
                payload = _complete_decision(fragment)
                with self.subTest(index=index):
                    with self.assertRaises(StateError):
                        runtime.write_decision("unknown-id-%d" % index, payload)

    def test_known_registered_ids_remain_readable_in_decision_log(self):
        payload = _complete_decision({
            "profile": {"id": "software", "version": "1.0.0"},
            "catalog": {
                "model_ids": ["gpt-5.6-terra"],
                "benchmark_ids": ["automation-bench"],
                "evidence_ids": ["arc-sol-max-agi2"],
            },
            "selection": {"ideal": "gpt-5.6-terra:medium:single"},
        })
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            stored = json.loads(
                runtime.write_decision("known-ids", payload).read_text(encoding="utf-8")
            )
            stored.pop("integrity")
            self.assertEqual(payload["profile"], stored["profile"])
            self.assertEqual(payload["catalog"], stored["catalog"])
            self.assertEqual(
                payload["selection"]["ideal"],
                stored["selection"]["ideal"],
            )

    def test_decision_routes_accept_the_complete_catalog_matrix_in_both_encodings(self):
        catalog = load_model_catalog(REFERENCES / "model-registry.json")
        routes = tuple(
            Route(model.slug, effort)
            for model in catalog.models
            for effort in model.supported_efforts
        )
        self.assertEqual(17, len(routes))
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            for index, route in enumerate(routes):
                encodings = (
                    ("string", route.key),
                    (
                        "object",
                        {
                            "model": route.model,
                            "effort": route.effort.value,
                            "topology": route.topology.value,
                        },
                    ),
                )
                for encoding, value in encodings:
                    with self.subTest(route=route.key, encoding=encoding):
                        stored = json.loads(
                            runtime.write_decision(
                                "valid-route-%02d-%s" % (index, encoding),
                                _complete_decision({
                                    "selection": {"ideal": value},
                                }),
                            ).read_text(encoding="utf-8")
                        )
                        self.assertEqual(value, stored["selection"]["ideal"])

    def test_decision_routes_reject_catalog_effort_and_topology_mismatches(self):
        catalog = load_model_catalog(REFERENCES / "model-registry.json")
        invalid_routes = []
        for model in catalog.models:
            supported = frozenset(model.supported_efforts)
            invalid_routes.extend(
                (
                    "unsupported-effort",
                    model.slug,
                    effort.value,
                    Route(model.slug, effort).topology.value,
                )
                for effort in Effort
                if effort not in supported
            )
            invalid_routes.extend(
                (
                    "wrong-topology",
                    model.slug,
                    effort.value,
                    "single" if Route(model.slug, effort).topology.value == "multi" else "multi",
                )
                for effort in model.supported_efforts
            )
        first_model = catalog.models[0]
        first_effort = first_model.supported_efforts[0]
        invalid_routes.extend(
            (
                (
                    "unknown-model",
                    first_model.slug + "-unknown",
                    first_effort.value,
                    Route(first_model.slug, first_effort).topology.value,
                ),
                (
                    "unknown-effort",
                    first_model.slug,
                    "not-an-effort",
                    "single",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            for index, (kind, model, effort, topology) in enumerate(invalid_routes):
                encodings = (
                    ("string", "%s:%s:%s" % (model, effort, topology)),
                    (
                        "object",
                        {
                            "model": model,
                            "effort": effort,
                            "topology": topology,
                        },
                    ),
                )
                for encoding, value in encodings:
                    with self.subTest(kind=kind, value=value, encoding=encoding):
                        with self.assertRaises(StateError):
                            runtime.write_decision(
                                "invalid-route-%02d-%s" % (index, encoding),
                                _complete_decision({
                                    "selection": {"ideal": value},
                                }),
                            )

    def test_decision_envelope_accepts_structural_task8_projection(self):
        route = "gpt-5.6-terra:medium:single"
        payload = {
            "schema_version": "1.0.0",
            "fingerprint": {
                "project_hash": "a" * 64,
                "primary_profile": "software",
                "secondary_profiles": ["strategy"],
                "archetype": "bounded-change",
                "novelty": "medium",
                "ambiguity": "low",
                "reasoning_depth": "medium",
                "context_load": "small",
                "tool_dependency": "high",
                "urgency": "normal",
                "cost_tolerance": "default",
                "latency_tolerance": "default",
                "verifiability": "strong",
                "impact": "low",
                "reversibility": "easy",
                "external_effects": False,
                "external_effects_authorized": False,
                "decomposability": "limited",
                "independent_fronts": 1,
                "parallel_writes": False,
                "worktree_isolated": False,
                "required_tools": ["shell"],
                "validation_checks": [
                    {"id": "tests", "category": "tests", "required": True}
                ],
            },
            "profile": {"id": "software", "version": "1.0.0"},
            "catalog": {
                "source": "seed",
                "schema_version": "1.0.0",
                "model_registry_version": "1.0.0",
                "benchmark_registry_version": "1.0.0",
                "verified_at": "2026-07-16",
                "model_ids": ["gpt-5.6-terra"],
                "benchmark_ids": ["automation-bench"],
                "evidence_ids": ["arc-sol-max-agi2"],
            },
            "eliminated": [
                {"route": "gpt-5.6-luna:low:single", "reason_code": "risk-floor"}
            ],
            "selection": {
                "economic": route,
                "ideal": route,
                "maximum_safety": route,
                "frontier": [route],
                "rationale_codes": {"ideal": "verified-quality"},
            },
            "decision": {"selected": route},
            "evidence_ids": ["arc-sol-max-agi2"],
            "history": [
                {
                    "route": route,
                    "validation_status": "pass",
                    "failure_class": None,
                    "reason_code": "initial-route",
                    "evidence_ids": ["b" * 64],
                    "escalation": 0,
                }
            ],
            "validation": {
                "status": "pass",
                "failure_class": None,
                "stop_reason": "approved",
                "required_checks_passed": 1,
                "metrics": {"required_checks_passed": 1.0},
                "evidence_ids": ["b" * 64],
                "requires_independent_verifier": False,
            },
            "status": "pass",
            "stop_reason": "approved",
            "budget": {
                "attempts": 1,
                "spent_tokens": 100,
                "elapsed_seconds": 2.5,
                "input_tokens": 80,
                "output_tokens": 20,
            },
            "timestamps": {
                "started_at": "2026-07-17T00:00:00Z",
                "finished_at": "2026-07-17T00:00:02Z",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            path = runtime.write_decision("task8-projection", payload)
            expected = deepcopy(payload)

            def typed_digest(kind, value):
                digest = hashlib.sha256(
                    ("ag-model-router:v1:%s:" % kind).encode("ascii")
                    + value.encode("utf-8")
                ).hexdigest()
                return "sha256:%s:%s" % (kind, digest)

            fingerprint = expected["fingerprint"]
            fingerprint["project_hash"] = typed_digest(
                "project-hash",
                fingerprint["project_hash"],
            )
            fingerprint["archetype"] = typed_digest(
                "archetype",
                fingerprint["archetype"],
            )
            fingerprint["required_tools"] = [
                typed_digest("required-tool", item)
                for item in fingerprint["required_tools"]
            ]
            for check in fingerprint["validation_checks"]:
                check["id"] = typed_digest("validation-check-id", check["id"])
                check["category"] = typed_digest(
                    "validation-check-category",
                    check["category"],
                )
            expected["history"][0]["evidence_ids"] = [
                "sha256:evidence:" + "b" * 64
            ]
            metric_value = expected["validation"]["metrics"].pop(
                "required_checks_passed"
            )
            expected["validation"]["metrics"] = {
                typed_digest("metric-name", "required_checks_passed"): metric_value
            }
            expected["validation"]["evidence_ids"] = [
                "sha256:evidence:" + "b" * 64
            ]
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored.pop("integrity")
            self.assertEqual(expected, stored)

    def test_directory_fsync_failure_removes_decision_and_run_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            with mock.patch(
                "model_router.state._fsync_directory",
                side_effect=[
                    None,
                    None,
                    OSError("commit fsync"),
                    None,
                    None,
                    None,
                ],
            ):
                with self.assertRaises(StateError):
                    runtime.write_decision(
                        "fsync-failure",
                        _complete_decision(),
                    )
            self.assertFalse((runtime.runs_dir / "fsync-failure").exists())
            self.assertEqual([], list(runtime.runs_dir.glob("**/*.new-*")))

    def test_active_decision_writer_is_transient_busy_and_retryable(self):
        import model_router.state as state_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer = RuntimeState(root, seed_root=REFERENCES)
            contender = RuntimeState(root, seed_root=REFERENCES)
            writer.bootstrap()
            entered = threading.Event()
            release = threading.Event()
            writer_errors = []
            real_prepare = state_module._prepare_decision_file

            def hold_first_writer(path, payload):
                if not entered.is_set():
                    entered.set()
                    if not release.wait(timeout=5):
                        raise RuntimeError("writer release timed out")
                return real_prepare(path, payload)

            def write_first():
                try:
                    writer.write_decision(
                        "concurrent-writer",
                        _complete_decision(),
                    )
                except Exception as error:
                    writer_errors.append(error)

            with mock.patch(
                "model_router.state._prepare_decision_file",
                side_effect=hold_first_writer,
            ):
                thread = threading.Thread(target=write_first)
                thread.start()
                self.assertTrue(entered.wait(timeout=5))
                try:
                    try:
                        contender.write_decision(
                            "concurrent-contender",
                            _complete_decision(),
                        )
                    except StateError as error:
                        during = str(error)
                    else:
                        during = "accepted"
                    blocked_during = contender._mutation_blocked
                    unknown_during = contender.unknown_outcome_path.exists()
                finally:
                    release.set()
                    thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], writer_errors)
            marker_after = contender.unknown_outcome_path.exists()
            try:
                contender.write_decision(
                    "concurrent-retry",
                    _complete_decision(),
                )
            except StateError as error:
                retry = str(error)
            else:
                retry = None
            self.assertEqual(
                {
                    "during": "runtime mutation is busy",
                    "blocked_during": False,
                    "unknown_during": False,
                    "marker_after": False,
                    "retry": None,
                },
                {
                    "during": during,
                    "blocked_during": blocked_during,
                    "unknown_during": unknown_during,
                    "marker_after": marker_after,
                    "retry": retry,
                },
            )

    def test_stale_advisory_lock_file_after_process_exit_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimeState(root, seed_root=REFERENCES)
            runtime.bootstrap()
            lock_path = root / ".runtime-mutation.lock"
            script = """
import os
import sys
sys.path.insert(0, %r)
from model_router.portable_flock import fcntl
path = %r
descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(descriptor, fcntl.LOCK_EX)
os._exit(0)
""" % (str(SCRIPTS), str(lock_path))
            completed = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(lock_path.exists())
            fresh = RuntimeState(root, seed_root=REFERENCES)
            path = fresh.write_decision(
                "after-stale-lock",
                _complete_decision(),
            )
            self.assertTrue(path.is_file())
            self.assertFalse(fresh._mutation_blocked)
            self.assertFalse(fresh.unknown_outcome_path.exists())

    def test_crashed_writer_witness_becomes_durable_unknown_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimeState(root, seed_root=REFERENCES)
            runtime.bootstrap()
            lock_path = root / ".runtime-mutation.lock"
            witness_path = root / ".mutation-in-progress"
            script = """
import os
import sys
sys.path.insert(0, %r)
from model_router.portable_flock import fcntl
lock_path = %r
witness_path = %r
descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(descriptor, fcntl.LOCK_EX)
witness = os.open(witness_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.write(witness, b'mutation-in-progress\\n')
os.fsync(witness)
os.close(witness)
if os.name == 'posix':
    directory = os.open(os.path.dirname(witness_path), os.O_RDONLY)
    os.fsync(directory)
    os.close(directory)
os._exit(0)
""" % (str(SCRIPTS), str(lock_path), str(witness_path))
            completed = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(lock_path.exists())
            self.assertTrue(witness_path.exists())
            fresh = RuntimeState(root, seed_root=REFERENCES)
            with self.assertRaisesRegex(StateError, "commit-outcome-unknown"):
                fresh.write_decision(
                    "after-crashed-writer",
                    _complete_decision(),
                )
            self.assertTrue(fresh._mutation_blocked)
            self.assertTrue(fresh.unknown_outcome_path.exists())
            self.assertFalse((fresh.runs_dir / "after-crashed-writer").exists())

    def test_confirmed_commit_witness_cleanup_failure_never_poison_fresh_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimeState(root, seed_root=REFERENCES)
            runtime.bootstrap()
            with mock.patch(
                "model_router.state._clear_runtime_witness",
                return_value=False,
            ):
                try:
                    path = runtime.write_decision(
                        "confirmed-before-cleanup-failure",
                        _complete_decision(),
                    )
                except StateError as error:
                    self.fail("confirmed commit returned error: %s" % error)
            self.assertTrue(path.is_file())
            self.assertEqual(
                b"outcome-confirmed\n",
                runtime.mutation_witness_path.read_bytes(),
            )
            self.assertFalse(runtime._mutation_blocked)
            self.assertFalse(runtime.unknown_outcome_path.exists())

            fresh = RuntimeState(root, seed_root=REFERENCES)
            retry = fresh.write_decision(
                "after-confirmed-cleanup-failure",
                _complete_decision(),
            )
            self.assertTrue(retry.is_file())
            self.assertFalse(fresh._mutation_blocked)
            self.assertFalse(fresh.unknown_outcome_path.exists())
            self.assertTrue(fresh.mutation_witness_path.exists())
            self.assertEqual(
                b"outcome-confirmed\n",
                fresh.mutation_witness_path.read_bytes(),
            )

    def test_decision_commit_requires_a_durable_known_outcome_witness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimeState(root, seed_root=REFERENCES)
            runtime.bootstrap()
            with mock.patch(
                "model_router.state._mark_runtime_witness_confirmed",
                side_effect=OSError("phase transition failure"),
            ), mock.patch(
                "model_router.state._clear_runtime_witness",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    StateError,
                    "commit-outcome-unknown",
                ):
                    runtime.write_decision(
                        "unconfirmed-finalization",
                        _complete_decision(),
                    )

            self.assertTrue(
                (
                    runtime.runs_dir
                    / "unconfirmed-finalization"
                    / "decision.json"
                ).is_file()
            )
            self.assertTrue(runtime._mutation_blocked)
            self.assertEqual(
                b"mutation-in-progress\n",
                runtime.mutation_witness_path.read_bytes(),
            )
            self.assertEqual(
                b"commit-outcome-unknown\n",
                runtime.unknown_outcome_path.read_bytes(),
            )

            fresh = RuntimeState(root, seed_root=REFERENCES)
            with self.assertRaisesRegex(StateError, "commit-outcome-unknown"):
                fresh.write_decision(
                    "blocked-after-unconfirmed-finalization",
                    _complete_decision(),
                )
            self.assertTrue(fresh._mutation_blocked)

    def test_decision_phase_failure_returns_unknown_and_blocks_fresh_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimeState(root, seed_root=REFERENCES)
            runtime.bootstrap()
            with mock.patch(
                "model_router.state._mark_runtime_witness_confirmed",
                side_effect=OSError("phase transition failure"),
            ):
                with self.assertRaisesRegex(
                    StateError,
                    "commit-outcome-unknown",
                ):
                    runtime.write_decision(
                        "phase-unconfirmed",
                        _complete_decision(),
                    )
            self.assertTrue(
                (
                    runtime.runs_dir
                    / "phase-unconfirmed"
                    / "decision.json"
                ).is_file()
            )
            self.assertTrue(runtime._mutation_blocked)
            self.assertTrue(runtime.mutation_witness_path.exists())
            self.assertTrue(runtime.unknown_outcome_path.exists())

            fresh = RuntimeState(root, seed_root=REFERENCES)
            with self.assertRaisesRegex(StateError, "commit-outcome-unknown"):
                fresh.write_decision(
                    "blocked-after-phase-failure",
                    _complete_decision(),
                )

    def test_absent_runtime_witness_still_requires_directory_durability(self):
        import model_router.state as state_module

        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp, "absent-runtime-witness")
            with mock.patch(
                "model_router.state._fsync_directory",
                side_effect=OSError("directory durability unavailable"),
            ):
                self.assertFalse(
                    state_module._clear_runtime_witness(absent)
                )

    def test_decision_restores_pending_witness_when_unknown_marker_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimeState(root, seed_root=REFERENCES)
            runtime.bootstrap()

            with mock.patch(
                "model_router.state._mark_runtime_witness_confirmed",
                side_effect=OSError("phase transition failure"),
            ), mock.patch.object(
                runtime,
                "_record_unknown_outcome",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    StateError,
                    "commit-outcome-unknown",
                ):
                    runtime.write_decision(
                        "unknown-marker-failure",
                        _complete_decision(),
                    )

            self.assertTrue(runtime._mutation_blocked)
            self.assertFalse(runtime.unknown_outcome_path.exists())
            self.assertEqual(
                b"mutation-in-progress\n",
                runtime.mutation_witness_path.read_bytes(),
            )
            fresh = RuntimeState(root, seed_root=REFERENCES)
            with self.assertRaisesRegex(StateError, "commit-outcome-unknown"):
                fresh.write_decision(
                    "blocked-after-unknown-marker-failure",
                    _complete_decision(),
                )

    def test_decision_pre_replace_stage_failures_leave_no_run_artifacts(self):
        import model_router.state as state_module

        real_prepare = state_module._prepare_decision_file
        real_replace = state_module.replace_file
        stages = ["write", "file-fsync", "replace"]
        if POSIX:
            stages.insert(0, "fchmod")
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
                runtime.bootstrap()
                if stage == "replace":
                    def fail_decision_replace(source, destination, **kwargs):
                        if Path(destination).name == "decision.json":
                            raise OSError("replace failure")
                        return real_replace(source, destination, **kwargs)

                    patcher = mock.patch(
                        "model_router.state.replace_file",
                        side_effect=fail_decision_replace,
                    )
                else:
                    def fail_decision_prepare(path, payload):
                        if stage == "fchmod":
                            target = "model_router.state.os.fchmod"
                        elif stage == "write":
                            target = "model_router.state._write_all"
                        else:
                            target = "model_router.state._fsync_file"
                        with mock.patch(
                            target,
                            side_effect=OSError(stage + " failure"),
                        ):
                            return real_prepare(path, payload)

                    patcher = mock.patch(
                        "model_router.state._prepare_decision_file",
                        side_effect=fail_decision_prepare,
                    )
                with patcher:
                    with self.assertRaises(StateError):
                        runtime.write_decision(
                            "stage-" + stage,
                            _complete_decision(),
                        )
                self.assertFalse((runtime.runs_dir / ("stage-" + stage)).exists())
                self.assertEqual([], list(runtime.runs_dir.glob("**/*.new-*")))

    def test_unknown_decision_commit_outcome_blocks_subsequent_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()

            calls = 0

            def fail_commit_and_rollback(_path):
                nonlocal calls
                calls += 1
                if calls <= 2:
                    return None
                raise OSError("directory durability unavailable")

            with mock.patch(
                "model_router.state._fsync_directory",
                side_effect=fail_commit_and_rollback,
            ):
                with self.assertRaisesRegex(StateError, "commit-outcome-unknown"):
                    runtime.write_decision(
                        "unknown-outcome",
                        _complete_decision(),
                    )
            with self.assertRaisesRegex(StateError, "commit-outcome-unknown"):
                runtime.write_decision(
                    "blocked-after-unknown",
                    _complete_decision(),
                )
            with self.assertRaisesRegex(StateError, "commit-outcome-unknown"):
                runtime.append_observation(observation_fixture())

    def test_unknown_outcome_guard_survives_marker_rewrite_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp), seed_root=REFERENCES)
            runtime.bootstrap()
            directory_calls = 0

            def fail_commit_and_rollback(_path):
                nonlocal directory_calls
                directory_calls += 1
                if directory_calls <= 2:
                    return None
                raise OSError("directory durability unavailable")

            def fail_marker_creation():
                runtime._mutation_blocked = True

            with mock.patch(
                "model_router.state._fsync_directory",
                side_effect=fail_commit_and_rollback,
            ), mock.patch.object(
                runtime,
                "_record_unknown_outcome",
                side_effect=fail_marker_creation,
            ):
                with self.assertRaisesRegex(StateError, "commit-outcome-unknown"):
                    runtime.write_decision(
                        "marker-rewrite-failure",
                        _complete_decision(),
                    )

            fresh = RuntimeState(Path(tmp), seed_root=REFERENCES)
            with self.assertRaisesRegex(StateError, "commit-outcome-unknown"):
                fresh.append_observation(observation_fixture())

    @unittest.skipUnless(FILE_SYMLINK_AVAILABLE, "file symlinks unavailable")
    def test_runtime_rejects_symlinked_registry_salt_and_telemetry_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(Path(tmp, "runtime"), seed_root=REFERENCES)
            runtime.bootstrap()

            salt_target = Path(tmp, "salt-target")
            salt_target.write_bytes(b"x" * 32)
            runtime.salt_path.unlink()
            runtime.salt_path.symlink_to(salt_target)
            with self.assertRaises(StateError):
                runtime.project_hash("/project")
            self.assertEqual(b"x" * 32, salt_target.read_bytes())

            runtime.salt_path.unlink()
            runtime.bootstrap()
            telemetry_target = Path(tmp, "telemetry-target")
            telemetry_target.write_text("preserve", encoding="utf-8")
            runtime.observations_path.symlink_to(telemetry_target)
            with self.assertRaises(StateError):
                runtime.append_observation(observation_fixture())
            self.assertEqual("preserve", telemetry_target.read_text(encoding="utf-8"))

            runtime.observations_path.unlink()
            registry_target = Path(tmp, "registry-target")
            registry_target.write_bytes(runtime.benchmark_registry_path.read_bytes())
            runtime.benchmark_registry_path.unlink()
            runtime.benchmark_registry_path.symlink_to(registry_target)
            with self.assertRaises(StateError):
                runtime.bootstrap()


if __name__ == "__main__":
    unittest.main()
