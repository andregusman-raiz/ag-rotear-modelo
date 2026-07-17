import json
import math
import sys
import tempfile
import traceback
import unittest
from collections.abc import Mapping as RuntimeMapping
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Mapping, get_type_hints

# ruff: noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1]
SKILL_ROOT = SCRIPTS.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))

from model_router.contracts import Effort, Route
from model_router.gates import GateResult, apply_gates
from model_router.model_registry import (
    CatalogError,
    CatalogSnapshot,
    ModelSpec,
    catalog_from_json,
    discover_catalog,
    generate_candidates,
)
from support import RecordingRunner, profile_fixture, request_fixture


class ModelRegistryTests(unittest.TestCase):
    def fixture_payload(self):
        return json.loads(
            (FIXTURES / "models-cache.json").read_text(encoding="utf-8")
        )

    def seed_path(self):
        return SKILL_ROOT / "references" / "model-registry.json"

    def test_fixture_exposes_seventeen_valid_routes(self):
        catalog = catalog_from_json(self.fixture_payload())
        routes = generate_candidates(catalog)
        self.assertEqual(17, len(routes))
        self.assertNotIn(
            "gpt-5.6-luna:ultra:multi",
            {route.key for route in routes},
        )

    def test_parser_preserves_observed_model_and_effort_order(self):
        catalog = catalog_from_json(self.fixture_payload())
        self.assertEqual(
            ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
            tuple(model.slug for model in catalog.models),
        )
        self.assertEqual(
            (
                Effort.LOW,
                Effort.MEDIUM,
                Effort.HIGH,
                Effort.XHIGH,
                Effort.MAX,
                Effort.ULTRA,
            ),
            catalog.models[0].supported_efforts,
        )
        self.assertEqual(
            tuple(
                Route("gpt-5.6-sol", effort)
                for effort in catalog.models[0].supported_efforts
            ),
            generate_candidates(catalog)[:6],
        )

    def test_parser_accepts_direct_list_and_filters_non_target_slugs(self):
        payload = self.fixture_payload()["models"]
        payload.insert(0, {"slug": "gpt-5.5-codex"})
        catalog = catalog_from_json(payload)
        self.assertEqual(
            ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
            tuple(model.slug for model in catalog.models),
        )

    def test_live_shape_preserves_string_multi_agent_versions(self):
        catalog = catalog_from_json(self.fixture_payload())
        self.assertEqual(
            ("v2", "v2", "v1"),
            tuple(model.multi_agent_version for model in catalog.models),
        )

    def test_parser_preserves_defaults_priority_visibility_and_api_support(self):
        catalog = catalog_from_json(self.fixture_payload())
        sol, terra, luna = catalog.models
        self.assertEqual(Effort.LOW, sol.default_effort)
        self.assertEqual(Effort.MEDIUM, terra.default_effort)
        self.assertEqual(Effort.MEDIUM, luna.default_effort)
        self.assertEqual((1, 2, 3), tuple(model.priority for model in catalog.models))
        self.assertTrue(all(model.supported_in_api for model in catalog.models))
        self.assertTrue(all(model.visibility == "list" for model in catalog.models))
        self.assertIs(sol, catalog.best_capability_model)

    def test_duplicate_target_slug_is_rejected(self):
        payload = self.fixture_payload()
        payload["models"].append(deepcopy(payload["models"][0]))
        with self.assertRaisesRegex(CatalogError, r"slug.*duplicate"):
            catalog_from_json(payload)

    def test_invalid_efforts_and_effort_shapes_are_rejected_without_coercion(self):
        cases = []
        for path, value in (
            (("default_reasoning_level",), "minimal"),
            (("default_reasoning_level",), 1),
            (("supported_reasoning_levels", 0, "effort"), "minimal"),
            (("supported_reasoning_levels", 0, "effort"), 1),
            (("supported_reasoning_levels",), "low"),
            (("supported_reasoning_levels", 0), "low"),
        ):
            payload = self.fixture_payload()
            target = payload["models"][0]
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            cases.append(payload)

        missing_default = self.fixture_payload()
        missing_default["models"][0]["default_reasoning_level"] = "ultra"
        missing_default["models"][0]["supported_reasoning_levels"] = [
            {"effort": "low"}
        ]
        cases.append(missing_default)

        duplicate_effort = self.fixture_payload()
        duplicate_effort["models"][0]["supported_reasoning_levels"].append(
            {"effort": "low"}
        )
        cases.append(duplicate_effort)

        for payload in cases:
            with self.subTest(payload=payload["models"][0]):
                with self.assertRaises(CatalogError):
                    catalog_from_json(payload)

    def test_invalid_scalar_types_and_non_finite_prices_are_rejected(self):
        field_values = (
            ("slug", 5),
            ("display_name", " "),
            ("supported_in_api", 1),
            ("visibility", 1),
            ("priority", True),
            ("priority", 1.0),
            ("priority", "1"),
            ("multi_agent_version", 2),
            ("multi_agent_version", "v3"),
            ("input_price_per_million", True),
            ("input_price_per_million", "5"),
            ("input_price_per_million", math.inf),
            ("input_price_per_million", math.nan),
            ("output_price_per_million", -1),
        )
        for field_name, value in field_values:
            payload = self.fixture_payload()
            payload["models"][0][field_name] = value
            with self.subTest(field_name=field_name, value=value):
                with self.assertRaises(CatalogError):
                    catalog_from_json(payload)

    def test_invalid_container_and_supported_tool_shapes_are_rejected(self):
        invalid_payloads = (
            None,
            (),
            {"models": {}},
            {"models": ["gpt-5.6-sol"]},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(CatalogError):
                    catalog_from_json(payload)

        for tools in ("shell", [""], [1], ["shell", "shell"]):
            payload = self.fixture_payload()
            payload["models"][0]["supported_tools"] = tools
            with self.subTest(tools=tools):
                with self.assertRaises(CatalogError):
                    catalog_from_json(payload)

    def test_supported_tools_distinguish_known_empty_from_absent_metadata(self):
        payload = self.fixture_payload()
        payload["models"][0]["supported_tools"] = []
        catalog = catalog_from_json(payload)
        self.assertEqual((), catalog.models[0].supported_tools)
        self.assertTrue(catalog.models[0].supported_tools_known)
        self.assertEqual((), catalog.models[1].supported_tools)
        self.assertFalse(catalog.models[1].supported_tools_known)

    def test_model_and_snapshot_copy_inputs_and_are_frozen(self):
        efforts = [Effort.LOW, Effort.MEDIUM]
        tools = ["shell"]
        model = ModelSpec(
            slug="gpt-5.6-luna",
            display_name="GPT-5.6-Luna",
            default_effort=Effort.MEDIUM,
            supported_efforts=efforts,
            supported_in_api=True,
            visibility="list",
            priority=3,
            multi_agent_version="v1",
            input_price_per_million=1,
            output_price_per_million=6,
            supported_tools=tools,
            supported_tools_known=True,
        )
        models = [model]
        snapshot = CatalogSnapshot("live", "2026-07-16T12:00:00Z", models)
        efforts.append(Effort.HIGH)
        tools.append("web")
        models.clear()
        self.assertEqual((Effort.LOW, Effort.MEDIUM), model.supported_efforts)
        self.assertEqual(("shell",), model.supported_tools)
        self.assertEqual((model,), snapshot.models)
        with self.assertRaises(FrozenInstanceError):
            model.priority = 9
        with self.assertRaises(FrozenInstanceError):
            snapshot.source = "cache"

    def test_live_catalog_precedes_cache_and_seed_and_uses_argv_tuple(self):
        runner = RecordingRunner(live_payload=self.fixture_payload())
        catalog = discover_catalog(
            codex_bin="codex",
            cache_path=FIXTURES / "models-cache.json",
            seed_path=self.seed_path(),
            runner=runner,
        )
        self.assertEqual("live", catalog.source)
        self.assertEqual(("codex", "debug", "models"), runner.calls[0])
        self.assertEqual(1, len(runner.calls))

    def test_fallback_order_is_live_cache_bundled_seed(self):
        cache_runner = RecordingRunner(
            live_payload=self.fixture_payload(),
            live_returncode=1,
        )
        cached = discover_catalog(
            "codex",
            FIXTURES / "models-cache.json",
            self.seed_path(),
            cache_runner,
        )
        self.assertEqual("cache", cached.source)
        self.assertEqual([("codex", "debug", "models")], cache_runner.calls)

        with tempfile.TemporaryDirectory() as tmp:
            invalid_cache = Path(tmp, "invalid.json")
            invalid_cache.write_text("[]", encoding="utf-8")
            bundled_runner = RecordingRunner(
                live_payload=self.fixture_payload(),
                bundled_payload=self.fixture_payload(),
                live_returncode=1,
            )
            bundled = discover_catalog(
                "codex",
                invalid_cache,
                self.seed_path(),
                bundled_runner,
            )
        self.assertEqual("bundled", bundled.source)
        self.assertEqual(
            [
                ("codex", "debug", "models"),
                ("codex", "debug", "models", "--bundled"),
            ],
            bundled_runner.calls,
        )

        seed_runner = RecordingRunner(
            live_payload=self.fixture_payload(),
            bundled_payload=self.fixture_payload(),
            live_returncode=1,
            bundled_returncode=1,
        )
        seeded = discover_catalog(
            "codex",
            FIXTURES / "missing-cache.json",
            self.seed_path(),
            seed_runner,
        )
        self.assertEqual("seed", seeded.source)

    def test_seed_prices_enrich_live_without_replacing_observed_capability(self):
        live_payload = self.fixture_payload()
        sol = live_payload["models"][0]
        sol["supported_reasoning_levels"] = [{"effort": "low"}]
        sol["supported_in_api"] = False
        sol["supported_tools"] = ["shell"]
        live_payload["models"] = [sol]
        catalog = discover_catalog(
            "codex",
            FIXTURES / "models-cache.json",
            self.seed_path(),
            RecordingRunner(live_payload),
        )
        model = catalog.models[0]
        self.assertEqual("live", catalog.source)
        self.assertEqual((Effort.LOW,), model.supported_efforts)
        self.assertFalse(model.supported_in_api)
        self.assertEqual(("shell",), model.supported_tools)
        self.assertTrue(model.supported_tools_known)
        self.assertEqual(5, model.input_price_per_million)
        self.assertEqual(30, model.output_price_per_million)

    def test_seed_records_official_pricing_provenance_and_values(self):
        payload = json.loads(self.seed_path().read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "currency": "USD",
                "unit": "per 1M tokens",
                "verified_at": "2026-07-16",
                "sources": [
                    "https://developers.openai.com/api/docs/models",
                    "https://openai.com/index/gpt-5-6/",
                ],
            },
            payload["pricing_provenance"],
        )
        prices = {
            model["slug"]: (
                model["input_price_per_million"],
                model["output_price_per_million"],
            )
            for model in payload["models"]
        }
        self.assertEqual(
            {
                "gpt-5.6-luna": (1, 6),
                "gpt-5.6-terra": (2.5, 15),
                "gpt-5.6-sol": (5, 30),
            },
            prices,
        )

    def test_discovery_normalizes_failures_without_leaking_sensitive_content(self):
        secret = "sk-secret-sensitive-payload"
        runner = RecordingRunner(
            live_payload=RuntimeError(secret),
            bundled_payload=RuntimeError(secret),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CatalogError) as raised:
                discover_catalog(
                    "codex",
                    Path(tmp, "missing-cache-%s.json" % secret),
                    Path(tmp, "missing-seed-%s.json" % secret),
                    runner,
                )
        message = str(raised.exception)
        formatted = "".join(
            traceback.format_exception(
                type(raised.exception),
                raised.exception,
                raised.exception.__traceback__,
            )
        )
        self.assertNotIn(secret, message)
        self.assertNotIn(secret, formatted)
        self.assertIn("live", message)
        self.assertIn("cache", message)
        self.assertIn("bundled", message)
        self.assertIn("seed", message)


class GateTests(unittest.TestCase):
    def routes(self):
        return generate_candidates(
            catalog_from_json(
                json.loads(
                    (FIXTURES / "models-cache.json").read_text(encoding="utf-8")
                )
            )
        )

    def profile(self):
        return profile_fixture()

    def tool_catalog(self, supported_tools=None, known=False):
        tools = () if supported_tools is None else supported_tools
        return CatalogSnapshot(
            source="live",
            observed_at="2026-07-16T12:00:00Z",
            models=(
                ModelSpec(
                    slug="gpt-5.6-terra",
                    display_name="GPT-5.6-Terra",
                    default_effort=Effort.MEDIUM,
                    supported_efforts=(Effort.MEDIUM,),
                    supported_in_api=True,
                    visibility="list",
                    priority=2,
                    multi_agent_version="v2",
                    input_price_per_million=2.5,
                    output_price_per_million=15,
                    supported_tools=tools,
                    supported_tools_known=known,
                ),
            ),
        )

    def test_external_effect_without_authorization_blocks_all_routes(self):
        request = request_fixture(
            external_effects=True,
            external_effects_authorized=False,
        )
        result = apply_gates(
            request,
            self.routes(),
            self.profile(),
            budget_open=True,
        )
        self.assertEqual((), result.viable)
        self.assertIn("external-effects-not-authorized", result.global_blockers)

    def test_closed_budget_and_critical_task_without_validator_are_global_blocks(self):
        budget_result = apply_gates(
            request_fixture(),
            self.routes(),
            self.profile(),
            budget_open=False,
        )
        self.assertEqual(("budget-exhausted",), budget_result.global_blockers)

        critical_request = request_fixture(impact="critical")
        object.__setattr__(critical_request, "validation_checks", ())
        critical_result = apply_gates(
            critical_request,
            self.routes(),
            self.profile(),
            budget_open=True,
        )
        self.assertEqual(
            ("critical-task-without-validator",),
            critical_result.global_blockers,
        )

    def test_ultra_requires_decomposition_and_isolated_parallel_writes(self):
        request = request_fixture(
            decomposability="high",
            independent_fronts=2,
            parallel_writes=True,
            worktree_isolated=False,
        )
        result = apply_gates(
            request,
            self.routes(),
            self.profile(),
            budget_open=True,
        )
        self.assertFalse(any(route.effort is Effort.ULTRA for route in result.viable))
        self.assertIn(
            "parallel-write-without-worktree",
            set(result.eliminated.values()),
        )

        weak_decomposition = apply_gates(
            request_fixture(decomposability="limited", independent_fronts=1),
            self.routes(),
            self.profile(),
            budget_open=True,
        )
        self.assertIn(
            "ultra-without-useful-decomposition",
            set(weak_decomposition.eliminated.values()),
        )

        isolated = apply_gates(
            request_fixture(
                decomposability="high",
                independent_fronts=2,
                parallel_writes=True,
                worktree_isolated=True,
            ),
            self.routes(),
            self.profile(),
            budget_open=True,
        )
        self.assertTrue(any(route.effort is Effort.ULTRA for route in isolated.viable))

    def test_known_supported_tools_keep_route_viable(self):
        route = Route("gpt-5.6-terra", Effort.MEDIUM)
        result = apply_gates(
            request_fixture(required_tools=["shell"]),
            (route,),
            self.profile(),
            budget_open=True,
            catalog=self.tool_catalog(("shell",), known=True),
        )
        self.assertEqual((route,), result.viable)
        self.assertEqual({}, dict(result.eliminated))
        self.assertEqual({}, dict(result.unknown_evidence))

    def test_known_missing_tool_eliminates_route(self):
        route = Route("gpt-5.6-terra", Effort.MEDIUM)
        result = apply_gates(
            request_fixture(required_tools=["shell", "web"]),
            (route,),
            self.profile(),
            budget_open=True,
            catalog=self.tool_catalog(("shell",), known=True),
        )
        self.assertEqual((), result.viable)
        self.assertEqual(
            "required-tools-unsupported:web",
            result.eliminated[route.key],
        )
        self.assertNotIn(route.key, result.unknown_evidence)

    def test_absent_tool_metadata_keeps_route_viable_and_records_unknown(self):
        route = Route("gpt-5.6-terra", Effort.MEDIUM)
        without_catalog = apply_gates(
            request_fixture(required_tools=["shell"]),
            (route,),
            self.profile(),
            budget_open=True,
        )
        without_metadata = apply_gates(
            request_fixture(required_tools=["shell"]),
            (route,),
            self.profile(),
            budget_open=True,
            catalog=self.tool_catalog(known=False),
        )
        for result in (without_catalog, without_metadata):
            with self.subTest(result=result):
                self.assertEqual((route,), result.viable)
                self.assertEqual(
                    "supported-tools-not-observed",
                    result.unknown_evidence[route.key],
                )
                self.assertNotIn(route.key, result.eliminated)

    def test_gate_result_uses_immutable_defensive_mappings_and_no_score(self):
        viable_input = [Route("gpt-5.6-terra", Effort.MEDIUM)]
        eliminated_input = {
            Route("gpt-5.6-sol", Effort.ULTRA).key: "parallel-write-without-worktree"
        }
        unknown_input = {
            viable_input[0].key: "supported-tools-not-observed"
        }
        result = GateResult(
            viable_input,
            eliminated_input,
            (),
            unknown_input,
        )
        viable_input.clear()
        eliminated_input.clear()
        unknown_input.clear()
        self.assertEqual(1, len(result.viable))
        self.assertEqual(1, len(result.eliminated))
        self.assertEqual(1, len(result.unknown_evidence))
        self.assertIsInstance(result.eliminated, RuntimeMapping)
        self.assertIsInstance(result.unknown_evidence, RuntimeMapping)
        with self.assertRaises(TypeError):
            result.eliminated["new"] = "reason"
        with self.assertRaises(TypeError):
            result.unknown_evidence["new"] = "unknown"
        with self.assertRaises(FrozenInstanceError):
            result.viable = ()
        self.assertEqual(
            ("viable", "eliminated", "global_blockers", "unknown_evidence"),
            tuple(field.name for field in fields(GateResult)),
        )
        hints = get_type_hints(GateResult)
        self.assertEqual(Mapping[str, str], hints["eliminated"])
        self.assertEqual(Mapping[str, str], hints["unknown_evidence"])

    def test_apply_gates_never_marks_a_viable_route_as_eliminated(self):
        result = apply_gates(
            request_fixture(),
            self.routes(),
            self.profile(),
            budget_open=True,
        )
        viable_keys = {route.key for route in result.viable}
        self.assertTrue(viable_keys.isdisjoint(result.eliminated))


if __name__ == "__main__":
    unittest.main()
