import json
import sys
import unittest
from collections.abc import Mapping
from dataclasses import MISSING, fields, replace
from pathlib import Path
from types import SimpleNamespace

# ruff: noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from model_router.contracts import ContractError, Effort, Route, TaskRequest, Topology
from model_router import __version__
from model_router.contracts import (
    ApprovalPolicy,
    ChildReport,
    EliminatedRoute,
    EvidenceItem,
    FailureClass,
    RouteAssessment,
    RouteDecision,
    SandboxMode,
    TextEnum,
    ValidationCheck,
    ValidationResult,
)
from support import (
    INVALID_BOOLEAN_VALUES,
    invalid_child_report_payloads,
    invalid_task_payloads,
    valid_child_report_payload,
    valid_eliminated_route_payload,
    valid_evidence_payload,
    valid_route_assessment_payload,
    valid_route_decision_payload,
    valid_route_payload,
    valid_task_payload,
    valid_validation_check_payload,
    valid_validation_result_payload,
    with_value,
    without_value,
)


class ExplodingMapping(Mapping):
    def __getitem__(self, key):
        raise RuntimeError("boom while reading mapping")

    def __iter__(self):
        raise RuntimeError("boom while iterating mapping")

    def __len__(self):
        return 1


class HostileKey:
    def __str__(self):
        raise RuntimeError("boom while formatting hostile key")

    def __repr__(self):
        raise RuntimeError("boom while representing hostile key")


class ExplodingIterable:
    def __iter__(self):
        raise RuntimeError("boom while iterating")


class ContractTests(unittest.TestCase):
    def valid_payload(self):
        return {
            "schema_version": "1.0.0",
            "primary_profile": "software",
            "secondary_profiles": [],
            "archetype": "bounded-change",
            "novelty": "low",
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
            "acceptance_criteria": ["todos os checks obrigatórios passam"],
            "validation_checks": [
                {"id": "tests", "category": "tests", "required": True}
            ],
        }

    def test_task_request_parses_all_structural_fields(self):
        request = TaskRequest.from_dict(self.valid_payload())
        self.assertEqual("software", request.primary_profile)
        self.assertEqual(("shell",), request.required_tools)
        self.assertTrue(request.strongly_verifiable)

    def test_external_effects_without_authorization_remain_explicit(self):
        payload = self.valid_payload()
        payload["external_effects"] = True
        request = TaskRequest.from_dict(payload)
        self.assertTrue(request.external_effects)
        self.assertFalse(request.external_effects_authorized)

    def test_invalid_enum_is_rejected(self):
        payload = self.valid_payload()
        payload["novelty"] = "impossible"
        with self.assertRaisesRegex(ContractError, "novelty"):
            TaskRequest.from_dict(payload)

    def test_ultra_route_has_multi_topology(self):
        route = Route(model="gpt-5.6-sol", effort=Effort.ULTRA)
        self.assertEqual(Topology.MULTI, route.topology)
        self.assertEqual("gpt-5.6-sol:ultra:multi", route.key)

    def test_public_version_is_initial_contract_version(self):
        self.assertEqual("0.1.0", __version__)

    def test_task_request_round_trips_with_field_names_unchanged(self):
        payload = self.valid_payload()
        self.assertEqual(payload, TaskRequest.from_dict(payload).to_dict())

    def test_route_round_trips_and_text_enums_parse_values(self):
        payload = {"model": "gpt-5.6-sol", "effort": "xhigh"}
        self.assertEqual(payload, Route.from_dict(payload).to_dict())
        self.assertEqual(
            SandboxMode.READ_ONLY,
            SandboxMode.parse("sandbox_mode", "read-only"),
        )
        self.assertEqual(
            ApprovalPolicy.ON_REQUEST,
            ApprovalPolicy.parse("approval_policy", "on-request"),
        )

    def test_validation_check_round_trips_with_field_names_unchanged(self):
        payload = {"id": "tests", "category": "tests", "required": True}
        self.assertEqual(payload, ValidationCheck.from_dict(payload).to_dict())

    def test_child_report_parses_and_round_trips(self):
        payload = {
            "status": "fail",
            "deliverable": "falha reproduzida",
            "evidence": [
                {
                    "category": "tests",
                    "passed": False,
                    "summary": "um teste falhou",
                    "exit_code": 1,
                }
            ],
            "metrics": {"failed_tests": 1.0},
            "failure_class": "coverage",
            "next_hint": "adicionar cobertura",
        }

        report = ChildReport.from_dict(payload)

        self.assertEqual(FailureClass.COVERAGE, report.failure_class)
        self.assertEqual((EvidenceItem("tests", False, "um teste falhou", 1),), report.evidence)
        self.assertEqual(payload, report.to_dict())

    def test_child_report_rejects_unknown_status(self):
        payload = {
            "status": "unknown",
            "deliverable": "",
            "evidence": [],
            "metrics": {},
            "failure_class": None,
            "next_hint": None,
        }
        with self.assertRaisesRegex(ContractError, "status"):
            ChildReport.from_dict(payload)

    def test_child_report_direct_construction_rejects_unknown_status(self):
        with self.assertRaisesRegex(ContractError, "status"):
            ChildReport("unknown", "", (), {}, None, None)

    def test_validation_result_requires_failure_class_for_fail_and_blocked(self):
        with self.assertRaisesRegex(ContractError, "failure_class"):
            ValidationResult("fail", (), {}, None, None)
        with self.assertRaisesRegex(ContractError, "failure_class"):
            ValidationResult("blocked", (), {}, None, "blocked")

    def test_validation_result_factories_and_serialization_preserve_contract(self):
        evidence = EvidenceItem("tests", True, "passed", 0)
        passed = ValidationResult.passed(
            (item for item in (evidence,)),
            {"passed_checks": 1.0},
        )
        blocked = ValidationResult.blocked(
            (evidence,),
            FailureClass.EXTERNAL_BLOCK,
        )
        result = ValidationResult.failed(
            FailureClass.COVERAGE,
            missing=("lint",),
            failed=("tests", "typecheck"),
        )
        payload = result.to_dict()

        self.assertEqual(
            {"missing_checks": 1.0, "failed_checks": 2.0},
            result.metrics,
        )
        self.assertEqual((evidence,), passed.evidence)
        self.assertEqual("pass", passed.status)
        self.assertEqual("blocked", blocked.status)
        self.assertEqual(result, ValidationResult.from_dict(payload))
        self.assertEqual("coverage", result.to_feedback()["failure_class"])

    def test_validation_result_independent_verifier_factory_sets_flag(self):
        result = ValidationResult.needs_independent_verifier()
        self.assertEqual("needs-verifier", result.status)
        self.assertTrue(result.requires_independent_verifier)

    def test_integral_json_numbers_normalize_to_python_integers(self):
        task_payload = self.valid_payload()
        task_payload["independent_fronts"] = 2.0
        request = TaskRequest.from_dict(task_payload)
        direct_request = replace(request, independent_fronts=3.0)

        evidence_payload = valid_evidence_payload()
        evidence_payload["exit_code"] = 0.0
        evidence = EvidenceItem.from_dict(evidence_payload)
        direct_evidence = EvidenceItem("tests", True, "passed", 1.0)

        self.assertIs(type(request.independent_fronts), int)
        self.assertEqual(2, request.independent_fronts)
        self.assertIs(type(direct_request.independent_fronts), int)
        self.assertEqual(3, direct_request.independent_fronts)
        self.assertIs(type(evidence.exit_code), int)
        self.assertEqual(0, evidence.exit_code)
        self.assertIs(type(direct_evidence.exit_code), int)
        self.assertEqual(1, direct_evidence.exit_code)

    def test_route_assessment_round_trips_and_checks_quality_floor(self):
        assessment = self.make_assessment(
            Route("gpt-5.6-sol", Effort.HIGH),
            capabilities=("coding", "tools"),
        )
        floor = SimpleNamespace(required_capabilities=("coding",))

        self.assertTrue(assessment.meets_quality_floor(floor))
        self.assertEqual(assessment, RouteAssessment.from_dict(assessment.to_dict()))

    def test_route_decision_converts_assessments_to_routes_and_round_trips(self):
        economic = self.make_assessment(Route("gpt-5.6-sol", Effort.MEDIUM))
        ideal = self.make_assessment(Route("gpt-5.6-pro", Effort.MAX))
        maximum_safety = self.make_assessment(Route("gpt-5.6-pro", Effort.ULTRA))
        eliminated = EliminatedRoute(Route("gpt-5.6-mini", Effort.LOW), "quality floor")

        decision = RouteDecision.from_assessments(
            (economic, ideal, maximum_safety),
            economic,
            ideal,
            maximum_safety,
            eliminated=(eliminated,),
            rationale={"ideal": "best verified quality"},
        )

        self.assertEqual(economic.route, decision.economic)
        self.assertEqual(ideal.route, decision.ideal)
        self.assertEqual(maximum_safety.route, decision.maximum_safety)
        self.assertEqual(decision, RouteDecision.from_dict(decision.to_dict()))

    def test_route_decision_rejects_selection_outside_frontier(self):
        frontier = (self.make_assessment(Route("gpt-5.6-sol", Effort.MEDIUM)),)
        outside = self.make_assessment(Route("gpt-5.6-pro", Effort.MAX))
        with self.assertRaisesRegex(ContractError, "frontier"):
            RouteDecision.from_assessments(frontier, frontier[0], outside, frontier[0])

    def make_assessment(self, route, capabilities=("coding",)):
        return RouteAssessment(
            route=route,
            evidence_grade=2,
            quality_signal=0.9,
            quality_basis="validated",
            expected_cost=1.5,
            expected_latency=2.5,
            residual_risk=0.1,
            sample_size=3,
            comparable_groups=("software",),
            capabilities=tuple(capabilities),
            prior_roles=("executor",),
            provisional=False,
            evidence_ids=("evidence-1",),
        )


class StrictFromDictTests(unittest.TestCase):
    def assert_rejected(self, operation, expected_path):
        try:
            operation()
        except Exception as error:
            self.assertIsInstance(error, ContractError)
            self.assertIn(expected_path, str(error))
        else:
            self.fail("ContractError not raised for %s" % expected_path)

    def parser_cases(self):
        return (
            ("route", Route.from_dict, valid_route_payload, {"model", "effort"}),
            (
                "validation_check",
                ValidationCheck.from_dict,
                valid_validation_check_payload,
                {"id", "category", "required"},
            ),
            (
                "task_request",
                TaskRequest.from_dict,
                valid_task_payload,
                set(valid_task_payload()),
            ),
            (
                "evidence",
                EvidenceItem.from_dict,
                valid_evidence_payload,
                {"category", "passed", "summary"},
            ),
            (
                "child_report",
                ChildReport.from_dict,
                valid_child_report_payload,
                set(valid_child_report_payload()),
            ),
            (
                "validation_result",
                ValidationResult.from_dict,
                valid_validation_result_payload,
                set(valid_validation_result_payload()),
            ),
            (
                "route_assessment",
                RouteAssessment.from_dict,
                valid_route_assessment_payload,
                set(valid_route_assessment_payload()),
            ),
            (
                "eliminated_route",
                EliminatedRoute.from_dict,
                valid_eliminated_route_payload,
                set(valid_eliminated_route_payload()),
            ),
            (
                "route_decision",
                RouteDecision.from_dict,
                valid_route_decision_payload,
                set(valid_route_decision_payload()),
            ),
        )

    def test_all_public_from_dict_methods_require_mapping_payloads(self):
        for root, parser, _, _ in self.parser_cases():
            for invalid_payload in (None, [], "payload", 1, True):
                with self.subTest(root=root, payload=invalid_payload):
                    self.assert_rejected(
                        lambda parser=parser, payload=invalid_payload: parser(payload),
                        root,
                    )

    def test_mapping_read_failures_are_normalized_to_contract_error(self):
        self.assert_rejected(
            lambda: Route.from_dict(ExplodingMapping()),
            "route",
        )

    def test_hostile_extra_keys_use_fixed_safe_paths_top_level_and_nested(self):
        top_level = valid_route_payload()
        top_level[HostileKey()] = True
        nested = valid_task_payload()
        nested["validation_checks"][0][HostileKey()] = True
        numeric_mapping = valid_child_report_payload()
        numeric_mapping["metrics"] = {HostileKey(): 1.0}
        string_mapping = valid_route_decision_payload()
        string_mapping["rationale"] = {HostileKey(): "reason"}

        cases = (
            (Route.from_dict, top_level, "route.<key>"),
            (
                TaskRequest.from_dict,
                nested,
                "task_request.validation_checks[0].<key>",
            ),
            (
                ChildReport.from_dict,
                numeric_mapping,
                "child_report.metrics.<key>",
            ),
            (
                RouteDecision.from_dict,
                string_mapping,
                "route_decision.rationale.<key>",
            ),
        )
        for parser, payload, expected_path in cases:
            with self.subTest(path=expected_path):
                self.assert_rejected(
                    lambda parser=parser, payload=payload: parser(payload),
                    expected_path,
                )

    def test_semantic_strings_reject_whitespace_only_without_trimming_content(self):
        verifier_payload = valid_validation_result_payload()
        verifier_payload.update(
            {
                "status": "needs-verifier",
                "failure_class": "risk",
                "stop_reason": "\t",
                "requires_independent_verifier": True,
            }
        )
        whitespace_cases = (
            (Route.from_dict, with_value(valid_route_payload(), ("model",), " \t"), "route.model"),
            (
                ValidationCheck.from_dict,
                with_value(valid_validation_check_payload(), ("id",), "   "),
                "validation_check.id",
            ),
            (
                ValidationCheck.from_dict,
                with_value(valid_validation_check_payload(), ("category",), "\t"),
                "validation_check.category",
            ),
            (
                EvidenceItem.from_dict,
                with_value(valid_evidence_payload(), ("category",), "\n "),
                "evidence.category",
            ),
            (
                EvidenceItem.from_dict,
                with_value(valid_evidence_payload(), ("summary",), " \t"),
                "evidence.summary",
            ),
            (
                ChildReport.from_dict,
                with_value(valid_child_report_payload(), ("deliverable",), "   "),
                "child_report.deliverable",
            ),
            (
                ValidationResult.from_dict,
                verifier_payload,
                "validation_result.stop_reason",
            ),
            (
                RouteAssessment.from_dict,
                with_value(valid_route_assessment_payload(), ("quality_basis",), "\t"),
                "route_assessment.quality_basis",
            ),
            (
                RouteAssessment.from_dict,
                with_value(valid_route_assessment_payload(), ("capabilities",), [" "]),
                "route_assessment.capabilities[0]",
            ),
            (
                EliminatedRoute.from_dict,
                with_value(valid_eliminated_route_payload(), ("reason",), "\n"),
                "eliminated_route.reason",
            ),
            (
                RouteDecision.from_dict,
                with_value(valid_route_decision_payload(), ("rationale",), {"ideal": "\t"}),
                "route_decision.rationale.ideal",
            ),
            (
                RouteDecision.from_dict,
                with_value(valid_route_decision_payload(), ("rationale",), {" ": "reason"}),
                "route_decision.rationale",
            ),
        )
        for parser, payload, expected_path in whitespace_cases:
            with self.subTest(path=expected_path):
                self.assert_rejected(
                    lambda parser=parser, payload=payload: parser(payload),
                    expected_path,
                )

        route = Route.from_dict({"model": " gpt-5.6-sol ", "effort": "medium"})
        check = ValidationCheck.from_dict(
            {"id": " tests ", "category": " checks ", "required": True}
        )
        self.assertEqual(" gpt-5.6-sol ", route.model)
        self.assertEqual(" tests ", check.id)
        self.assertEqual(" checks ", check.category)

    def test_validation_result_from_dict_enforces_verifier_status_flag_coherence(self):
        invalid_cases = (
            ("pass", None, True),
            ("fail", "depth", True),
            ("blocked", "depth", True),
            ("needs-verifier", "depth", False),
        )
        for status, failure_class, requires_verifier in invalid_cases:
            payload = valid_validation_result_payload()
            payload.update(
                {
                    "status": status,
                    "failure_class": failure_class,
                    "requires_independent_verifier": requires_verifier,
                }
            )
            with self.subTest(status=status, requires_verifier=requires_verifier):
                self.assert_rejected(
                    lambda payload=payload: ValidationResult.from_dict(payload),
                    "validation_result.requires_independent_verifier",
                )

        payload = valid_validation_result_payload()
        payload.update(
            {
                "status": "needs-verifier",
                "failure_class": "depth",
                "stop_reason": "partial mutation",
                "requires_independent_verifier": True,
            }
        )
        result = ValidationResult.from_dict(payload)
        self.assertEqual(FailureClass.DEPTH, result.failure_class)

    def test_validation_result_from_dict_rejects_failure_metadata_on_pass(self):
        invalid_cases = (
            ("failure_class", "depth"),
            ("stop_reason", "stopped"),
        )
        for field_name, value in invalid_cases:
            payload = valid_validation_result_payload()
            payload[field_name] = value
            with self.subTest(field=field_name):
                self.assert_rejected(
                    lambda payload=payload: ValidationResult.from_dict(payload),
                    "validation_result." + field_name,
                )

    def test_all_public_from_dict_methods_reject_extra_fields(self):
        for root, parser, payload_factory, _ in self.parser_cases():
            payload = payload_factory()
            payload["unexpected"] = True
            with self.subTest(root=root):
                self.assert_rejected(
                    lambda parser=parser, payload=payload: parser(payload),
                    root + ".unexpected",
                )

    def test_all_public_from_dict_methods_reject_missing_required_fields(self):
        for root, parser, payload_factory, required in self.parser_cases():
            for field_name in required:
                payload = payload_factory()
                del payload[field_name]
                with self.subTest(root=root, field=field_name):
                    self.assert_rejected(
                        lambda parser=parser, payload=payload: parser(payload),
                        root + "." + field_name,
                    )

    def test_all_boolean_inputs_reject_strings_numbers_null_and_absence(self):
        boolean_cases = (
            (
                TaskRequest.from_dict,
                valid_task_payload,
                ("external_effects",),
                "task_request.external_effects",
            ),
            (
                TaskRequest.from_dict,
                valid_task_payload,
                ("external_effects_authorized",),
                "task_request.external_effects_authorized",
            ),
            (
                TaskRequest.from_dict,
                valid_task_payload,
                ("parallel_writes",),
                "task_request.parallel_writes",
            ),
            (
                TaskRequest.from_dict,
                valid_task_payload,
                ("worktree_isolated",),
                "task_request.worktree_isolated",
            ),
            (
                ValidationCheck.from_dict,
                valid_validation_check_payload,
                ("required",),
                "validation_check.required",
            ),
            (
                EvidenceItem.from_dict,
                valid_evidence_payload,
                ("passed",),
                "evidence.passed",
            ),
            (
                ValidationResult.from_dict,
                valid_validation_result_payload,
                ("requires_independent_verifier",),
                "validation_result.requires_independent_verifier",
            ),
            (
                RouteAssessment.from_dict,
                valid_route_assessment_payload,
                ("provisional",),
                "route_assessment.provisional",
            ),
        )
        for parser, payload_factory, path, expected_path in boolean_cases:
            for invalid_value in INVALID_BOOLEAN_VALUES:
                payload = with_value(payload_factory(), path, invalid_value)
                with self.subTest(path=expected_path, value=invalid_value):
                    self.assert_rejected(
                        lambda parser=parser, payload=payload: parser(payload),
                        expected_path,
                    )
            payload = without_value(payload_factory(), path)
            with self.subTest(path=expected_path, value="absent"):
                self.assert_rejected(
                    lambda parser=parser, payload=payload: parser(payload),
                    expected_path,
                )

    def test_task_request_runtime_rejects_every_schema_invalid_mutation(self):
        for expected_path, payload in invalid_task_payloads():
            with self.subTest(path=expected_path):
                self.assert_rejected(
                    lambda payload=payload: TaskRequest.from_dict(payload),
                    expected_path,
                )

    def test_child_report_runtime_rejects_every_schema_invalid_mutation(self):
        for expected_path, payload in invalid_child_report_payloads():
            with self.subTest(path=expected_path):
                self.assert_rejected(
                    lambda payload=payload: ChildReport.from_dict(payload),
                    expected_path,
                )

    def test_remaining_from_dict_methods_reject_types_empty_strings_and_nested_items(self):
        invalid_cases = (
            (Route.from_dict, with_value(valid_route_payload(), ("model",), ""), "route.model"),
            (Route.from_dict, with_value(valid_route_payload(), ("model",), 7), "route.model"),
            (
                Route.from_dict,
                with_value(valid_route_payload(), ("effort",), Effort.MEDIUM),
                "route.effort",
            ),
            (
                ValidationCheck.from_dict,
                with_value(valid_validation_check_payload(), ("id",), ""),
                "validation_check.id",
            ),
            (
                ValidationCheck.from_dict,
                with_value(valid_validation_check_payload(), ("category",), 1),
                "validation_check.category",
            ),
            (
                EvidenceItem.from_dict,
                with_value(valid_evidence_payload(), ("category",), ""),
                "evidence.category",
            ),
            (
                EvidenceItem.from_dict,
                with_value(valid_evidence_payload(), ("summary",), ""),
                "evidence.summary",
            ),
            (
                EvidenceItem.from_dict,
                with_value(valid_evidence_payload(), ("exit_code",), True),
                "evidence.exit_code",
            ),
            (
                EvidenceItem.from_dict,
                with_value(valid_evidence_payload(), ("exit_code",), "0"),
                "evidence.exit_code",
            ),
            (
                ValidationResult.from_dict,
                with_value(valid_validation_result_payload(), ("status",), "unknown"),
                "validation_result.status",
            ),
            (
                ValidationResult.from_dict,
                with_value(valid_validation_result_payload(), ("evidence",), {}),
                "validation_result.evidence",
            ),
            (
                ValidationResult.from_dict,
                with_value(valid_validation_result_payload(), ("metrics",), {"score": "1"}),
                "validation_result.metrics.score",
            ),
            (
                ValidationResult.from_dict,
                with_value(valid_validation_result_payload(), ("failure_class",), "unknown"),
                "validation_result.failure_class",
            ),
            (
                ValidationResult.from_dict,
                with_value(valid_validation_result_payload(), ("stop_reason",), ""),
                "validation_result.stop_reason",
            ),
            (
                RouteAssessment.from_dict,
                with_value(valid_route_assessment_payload(), ("route", "model"), ""),
                "route_assessment.route.model",
            ),
            (
                RouteAssessment.from_dict,
                with_value(valid_route_assessment_payload(), ("evidence_grade",), True),
                "route_assessment.evidence_grade",
            ),
            (
                RouteAssessment.from_dict,
                with_value(valid_route_assessment_payload(), ("quality_signal",), "0.9"),
                "route_assessment.quality_signal",
            ),
            (
                RouteAssessment.from_dict,
                with_value(valid_route_assessment_payload(), ("quality_basis",), ""),
                "route_assessment.quality_basis",
            ),
            (
                RouteAssessment.from_dict,
                with_value(valid_route_assessment_payload(), ("sample_size",), True),
                "route_assessment.sample_size",
            ),
            (
                RouteAssessment.from_dict,
                with_value(valid_route_assessment_payload(), ("capabilities",), [""]),
                "route_assessment.capabilities[0]",
            ),
            (
                EliminatedRoute.from_dict,
                with_value(valid_eliminated_route_payload(), ("route", "model"), ""),
                "eliminated_route.route.model",
            ),
            (
                EliminatedRoute.from_dict,
                with_value(valid_eliminated_route_payload(), ("reason",), ""),
                "eliminated_route.reason",
            ),
            (
                RouteDecision.from_dict,
                with_value(valid_route_decision_payload(), ("economic", "model"), ""),
                "route_decision.economic.model",
            ),
            (
                RouteDecision.from_dict,
                with_value(valid_route_decision_payload(), ("frontier",), {}),
                "route_decision.frontier",
            ),
            (
                RouteDecision.from_dict,
                with_value(valid_route_decision_payload(), ("rationale",), {"": "reason"}),
                "route_decision.rationale",
            ),
            (
                RouteDecision.from_dict,
                with_value(valid_route_decision_payload(), ("rationale",), {"ideal": ""}),
                "route_decision.rationale.ideal",
            ),
        )
        for parser, payload, expected_path in invalid_cases:
            with self.subTest(path=expected_path):
                self.assert_rejected(
                    lambda parser=parser, payload=payload: parser(payload),
                    expected_path,
                )


class DirectConstructionTests(unittest.TestCase):
    def assert_rejected(self, operation, expected_path):
        try:
            operation()
        except Exception as error:
            self.assertIsInstance(error, ContractError)
            self.assertIn(expected_path, str(error))
        else:
            self.fail("ContractError not raised for %s" % expected_path)

    def valid_objects(self):
        return {
            "route": Route.from_dict(valid_route_payload()),
            "check": ValidationCheck.from_dict(valid_validation_check_payload()),
            "task": TaskRequest.from_dict(valid_task_payload()),
            "evidence": EvidenceItem.from_dict(valid_evidence_payload()),
            "child": ChildReport.from_dict(valid_child_report_payload()),
            "validation": ValidationResult.from_dict(valid_validation_result_payload()),
            "assessment": RouteAssessment.from_dict(valid_route_assessment_payload()),
            "eliminated": EliminatedRoute.from_dict(valid_eliminated_route_payload()),
            "decision": RouteDecision.from_dict(valid_route_decision_payload()),
        }

    def test_invalid_direct_enum_construction_raises_contract_error(self):
        for enum_type in (Effort, Topology, SandboxMode, ApprovalPolicy, FailureClass):
            with self.subTest(enum=enum_type.__name__):
                self.assert_rejected(
                    lambda enum_type=enum_type: enum_type("not-valid"),
                    enum_type.__name__,
                )

    def test_text_enum_parse_reports_the_caller_field_path(self):
        self.assert_rejected(
            lambda: TextEnum.parse.__func__(Effort, "route.effort", "not-valid"),
            "route.effort",
        )

    def test_every_public_dataclass_rejects_invalid_direct_construction(self):
        objects = self.valid_objects()
        route = objects["route"]
        assessment = objects["assessment"]
        invalid_cases = (
            (lambda: Route("", Effort.MEDIUM), "route.model"),
            (lambda: Route("\t", Effort.MEDIUM), "route.model"),
            (lambda: Route("gpt-5.6-sol", "medium"), "route.effort"),
            (lambda: ValidationCheck("", "tests", True), "validation_check.id"),
            (lambda: ValidationCheck("tests", "tests", 1), "validation_check.required"),
            (lambda: replace(objects["task"], schema_version="2.0.0"), "task_request.schema_version"),
            (
                lambda: replace(objects["task"], external_effects_authorized="false"),
                "task_request.external_effects_authorized",
            ),
            (lambda: replace(objects["task"], independent_fronts=0), "task_request.independent_fronts"),
            (lambda: replace(objects["task"], independent_fronts=1.5), "task_request.independent_fronts"),
            (lambda: replace(objects["task"], validation_checks=()), "task_request.validation_checks"),
            (lambda: EvidenceItem("tests", "true", "summary", 0), "evidence.passed"),
            (lambda: EvidenceItem("tests", True, "summary", 0.5), "evidence.exit_code"),
            (lambda: ChildReport("pass", "", (), {}, None, None), "child_report.deliverable"),
            (lambda: ChildReport("pass", "ok", [], {}, None, None), "child_report.evidence"),
            (
                lambda: ValidationResult("pass", (), {}, "depth", None, False),
                "validation_result.failure_class",
            ),
            (
                lambda: replace(objects["assessment"], route="not-a-route"),
                "route_assessment.route",
            ),
            (
                lambda: replace(objects["assessment"], provisional=1),
                "route_assessment.provisional",
            ),
            (lambda: EliminatedRoute("not-a-route", "reason"), "eliminated_route.route"),
            (lambda: EliminatedRoute(route, ""), "eliminated_route.reason"),
            (
                lambda: RouteDecision(
                    Route("outside", Effort.LOW),
                    route,
                    route,
                    (assessment,),
                    (),
                    {},
                ),
                "route_decision.economic",
            ),
            (
                lambda: replace(objects["decision"], rationale={"ideal": ""}),
                "route_decision.rationale.ideal",
            ),
        )
        for operation, expected_path in invalid_cases:
            with self.subTest(path=expected_path):
                self.assert_rejected(operation, expected_path)

    def test_validation_result_verifier_flag_matches_status(self):
        failure = FailureClass.DEPTH
        invalid_cases = (
            ("pass", None, True),
            ("fail", failure, True),
            ("blocked", failure, True),
            ("needs-verifier", failure, False),
        )
        for status, failure_class, requires_verifier in invalid_cases:
            with self.subTest(status=status, requires_verifier=requires_verifier):
                self.assert_rejected(
                    lambda status=status, failure_class=failure_class, requires_verifier=requires_verifier: ValidationResult(
                        status,
                        (),
                        {},
                        failure_class,
                        None,
                        requires_verifier,
                    ),
                    "validation_result.requires_independent_verifier",
                )

        result = ValidationResult(
            "needs-verifier",
            (),
            {},
            failure,
            "partial mutation",
            True,
        )
        self.assertEqual(failure, result.failure_class)

    def test_validation_result_pass_rejects_failure_metadata(self):
        invalid_cases = (
            (
                lambda: ValidationResult(
                    "pass",
                    (),
                    {},
                    FailureClass.DEPTH,
                    None,
                    False,
                ),
                "validation_result.failure_class",
            ),
            (
                lambda: ValidationResult(
                    "pass",
                    (),
                    {},
                    None,
                    "stopped",
                    False,
                ),
                "validation_result.stop_reason",
            ),
        )
        for operation, expected_path in invalid_cases:
            with self.subTest(path=expected_path):
                self.assert_rejected(operation, expected_path)

    def test_validation_result_factories_normalize_invalid_inputs(self):
        invalid_cases = (
            (
                lambda: ValidationResult.passed(ExplodingIterable(), {}),
                "validation_result.evidence",
            ),
            (
                lambda: ValidationResult.passed((), ExplodingMapping()),
                "validation_result.metrics",
            ),
            (
                lambda: ValidationResult.blocked(
                    ExplodingIterable(),
                    FailureClass.EXTERNAL_BLOCK,
                ),
                "validation_result.evidence",
            ),
            (
                lambda: ValidationResult.failed(
                    FailureClass.COVERAGE,
                    missing=ExplodingIterable(),
                ),
                "validation_result.missing",
            ),
            (
                lambda: ValidationResult.failed(
                    FailureClass.COVERAGE,
                    failed=ExplodingIterable(),
                ),
                "validation_result.failed",
            ),
        )
        for operation, expected_path in invalid_cases:
            with self.subTest(path=expected_path):
                self.assert_rejected(operation, expected_path)


class ImmutabilityAndFrontierTests(unittest.TestCase):
    def test_child_report_metrics_are_copied_frozen_and_serialize_to_plain_dict(self):
        metrics = {"score": 1.0}
        report = ChildReport("pass", "validated", (), metrics, None, None)

        metrics["score"] = 2.0
        self.assertEqual(1.0, report.metrics["score"])
        with self.assertRaises(TypeError):
            report.metrics["score"] = 3.0

        serialized = report.to_dict()
        self.assertIs(type(serialized["metrics"]), dict)
        serialized["metrics"]["score"] = 4.0
        self.assertEqual(1.0, report.metrics["score"])

    def test_validation_result_metrics_are_copied_frozen_and_serialize_to_plain_dict(self):
        metrics = {"score": 1.0}
        result = ValidationResult("pass", (), metrics, None, None, False)

        metrics["score"] = 2.0
        self.assertEqual(1.0, result.metrics["score"])
        with self.assertRaises(TypeError):
            result.metrics["score"] = 3.0

        serialized = result.to_dict()
        self.assertIs(type(serialized["metrics"]), dict)
        serialized["metrics"]["score"] = 4.0
        self.assertEqual(1.0, result.metrics["score"])

    def test_route_decision_rationale_is_copied_frozen_and_serializes_to_plain_dict(self):
        assessment = RouteAssessment.from_dict(valid_route_assessment_payload())
        rationale = {"ideal": "verified"}
        decision = RouteDecision.from_assessments(
            (assessment,),
            assessment,
            assessment,
            assessment,
            rationale=rationale,
        )

        rationale["ideal"] = "mutated"
        self.assertEqual("verified", decision.rationale["ideal"])
        with self.assertRaises(TypeError):
            decision.rationale["ideal"] = "mutated again"

        serialized = decision.to_dict()
        self.assertIs(type(serialized["rationale"]), dict)
        serialized["rationale"]["ideal"] = "serialized mutation"
        self.assertEqual("verified", decision.rationale["ideal"])

    def test_from_assessments_materializes_one_shot_frontier_once(self):
        assessment = RouteAssessment.from_dict(valid_route_assessment_payload())
        frontier = (item for item in (assessment,))

        decision = RouteDecision.from_assessments(
            frontier,
            assessment,
            assessment,
            assessment,
        )

        self.assertEqual((assessment,), decision.frontier)
        self.assertEqual(assessment.route, decision.economic)


class SchemaTests(unittest.TestCase):
    def load_schema(self, filename):
        path = SCRIPTS.parent / "references" / filename
        with path.open(encoding="utf-8") as schema_file:
            return json.load(schema_file)

    def field_names(self, contract_type):
        return {item.name for item in fields(contract_type)}

    def required_field_names(self, contract_type):
        return {
            item.name
            for item in fields(contract_type)
            if item.default is MISSING and item.default_factory is MISSING
        }

    def test_task_request_schema_matches_required_dataclass_fields(self):
        schema = self.load_schema("task-request-schema.json")
        expected_fields = self.field_names(TaskRequest)

        self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(expected_fields, set(schema["properties"]))
        self.assertEqual(self.required_field_names(TaskRequest), set(schema["required"]))
        self.assertEqual(
            ["low", "medium", "high", "ood"],
            schema["properties"]["novelty"]["enum"],
        )
        checks = schema["properties"]["validation_checks"]["items"]
        self.assertFalse(checks["additionalProperties"])
        self.assertEqual(self.field_names(ValidationCheck), set(checks["properties"]))
        self.assertEqual(self.required_field_names(ValidationCheck), set(checks["required"]))

    def test_child_result_schema_matches_required_dataclass_fields(self):
        schema = self.load_schema("child-result-schema.json")

        self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(self.field_names(ChildReport), set(schema["properties"]))
        self.assertEqual(self.required_field_names(ChildReport), set(schema["required"]))
        self.assertEqual(
            ["pass", "fail", "blocked"],
            schema["properties"]["status"]["enum"],
        )
        evidence = schema["properties"]["evidence"]["items"]
        self.assertFalse(evidence["additionalProperties"])
        self.assertEqual(self.field_names(EvidenceItem), set(evidence["properties"]))
        self.assertEqual(self.required_field_names(EvidenceItem), set(evidence["required"]))


if __name__ == "__main__":
    unittest.main()
