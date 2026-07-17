import itertools
import math
import sys
import threading
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
REFERENCES = SCRIPTS.parent / "references"
PROFILES = REFERENCES / "profiles"
sys.path.insert(0, str(SCRIPTS))

from model_router.contracts import (  # noqa: E402
    ChildReport,
    Effort,
    EvidenceItem,
    FailureClass,
    Route,
    ValidationCheck,
)
from model_router.escalation import (  # noqa: E402
    BudgetError,
    BudgetLedger,
    EscalationDecision,
    EscalationError,
    ProgressError,
    ProgressTracker,
    choose_next_route,
    safest_route,
)
from model_router.profiles import load_profiles  # noqa: E402
from model_router.validators import (  # noqa: E402
    ValidationError,
    validate_child_report,
)
from support import (  # noqa: E402
    FakeClock,
    assessment,
    child_report_with_categories,
    decision_fixture,
    partial_operation_report,
    request_fixture,
    request_for,
    route_fixture,
    ultra_viable_fixture,
    verifier_pass_report,
)


class ValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profiles = load_profiles(PROFILES)

    def test_all_six_profiles_enforce_their_required_categories(self):
        self.assertEqual(6, len(self.profiles))
        for profile in self.profiles.values():
            with self.subTest(profile=profile.id):
                report = child_report_with_categories(profile.validator_categories)
                result = validate_child_report(
                    profile, request_for(profile.id), report
                )
                self.assertEqual("pass", result.status)

    def test_required_request_categories_extend_profile_categories(self):
        profile = self.profiles["software"]
        request = request_fixture(
            validation_checks=[
                {"id": "tests", "category": "tests", "required": True},
                {"id": "audit", "category": "external-audit", "required": True},
            ]
        )
        report = child_report_with_categories(profile.validator_categories)
        result = validate_child_report(profile, request, report)
        self.assertEqual("fail", result.status)
        self.assertEqual(FailureClass.COVERAGE, result.failure_class)
        self.assertEqual(1.0, result.metrics["missing_checks"])

    def test_missing_is_coverage_and_failed_uses_structured_or_depth_class(self):
        profile = self.profiles["research"]
        missing = child_report_with_categories(("sources",))
        result = validate_child_report(profile, request_for("research"), missing)
        self.assertEqual(FailureClass.COVERAGE, result.failure_class)

        categories = profile.validator_categories
        failed = child_report_with_categories(
            categories, status="pass", passed=False,
        )
        result = validate_child_report(profile, request_for("research"), failed)
        self.assertEqual(FailureClass.DEPTH, result.failure_class)

        structured = ChildReport(
            "fail", "technical failure", failed.evidence, failed.metrics,
            FailureClass.CAPACITY, None,
        )
        result = validate_child_report(
            profile, request_for("research"), structured
        )
        self.assertEqual(FailureClass.CAPACITY, result.failure_class)

    def test_fail_status_never_becomes_pass_and_blocked_is_external(self):
        profile = self.profiles["software"]
        report = child_report_with_categories(
            profile.validator_categories, status="fail",
        )
        result = validate_child_report(profile, request_for("software"), report)
        self.assertEqual("fail", result.status)
        self.assertEqual(FailureClass.DEPTH, result.failure_class)

        blocked = child_report_with_categories(
            profile.validator_categories, status="blocked",
            failure_class=FailureClass.RISK,
        )
        result = validate_child_report(profile, request_for("software"), blocked)
        self.assertEqual("blocked", result.status)
        self.assertEqual(FailureClass.EXTERNAL_BLOCK, result.failure_class)

    def test_duplicate_conflicting_unknown_evidence_and_check_ids_are_rejected(self):
        profile = self.profiles["software"]
        request = request_for("software")
        valid = child_report_with_categories(profile.validator_categories)
        duplicate = ChildReport(
            "pass", valid.deliverable,
            valid.evidence + (valid.evidence[0],), valid.metrics, None, None,
        )
        with self.assertRaisesRegex(ValidationError, "duplicate evidence category"):
            validate_child_report(profile, request, duplicate)

        conflicting_item = EvidenceItem(
            valid.evidence[0].category, False, "conflicting result", 1
        )
        conflicting = ChildReport(
            "pass", valid.deliverable,
            valid.evidence + (conflicting_item,), valid.metrics, None, None,
        )
        with self.assertRaisesRegex(ValidationError, "conflicting"):
            validate_child_report(profile, request, conflicting)

        unknown = ChildReport(
            "pass", valid.deliverable,
            valid.evidence + (EvidenceItem("unknown-extra", True, "extra", 0),),
            valid.metrics, None, None,
        )
        with self.assertRaisesRegex(ValidationError, "unknown evidence"):
            validate_child_report(profile, request, unknown)

        checks = request.validation_checks + (
            ValidationCheck(
                request.validation_checks[0].id, "other-category", True
            ),
        )
        duplicate_id_request = replace(request, validation_checks=checks)
        with self.assertRaisesRegex(ValidationError, "duplicate validation check id"):
            validate_child_report(profile, duplicate_id_request, valid)

    def test_metrics_are_declared_finite_numbers_and_hostile_inputs_fail(self):
        profile = self.profiles["software"]
        request = request_for("software")
        report = child_report_with_categories(profile.validator_categories)
        for bad_metrics in (
            {"required_checks_passed": True},
            {"required_checks_passed": math.nan},
            {"required_checks_passed": math.inf},
            {"unknown_metric": 1.0},
            {1: 1.0},
        ):
            with self.subTest(metrics=bad_metrics):
                object.__setattr__(report, "metrics", bad_metrics)
                with self.assertRaises(ValidationError):
                    validate_child_report(profile, request, report)
        for field, bad in (
            ("profile", object()),
            ("request", object()),
            ("report", object()),
            ("independent_report", object()),
        ):
            with self.subTest(field=field):
                args = {
                    "profile": profile,
                    "request": request,
                    "report": child_report_with_categories(
                        profile.validator_categories
                    ),
                    "independent_report": None,
                }
                args[field] = bad
                with self.assertRaises(ValidationError):
                    validate_child_report(**args)

    def test_critical_report_requires_valid_independent_verifier(self):
        profile = self.profiles["software"]
        request = request_for("software")
        request = replace(request, impact="critical")
        report = child_report_with_categories(profile.validator_categories)

        pending = validate_child_report(profile, request, report)
        self.assertEqual("needs-verifier", pending.status)
        self.assertTrue(pending.requires_independent_verifier)

        approved = validate_child_report(
            profile,
            request,
            report,
            verifier_pass_report(),
            primary_execution_id="thread-primary",
            independent_execution_id="thread-verifier",
        )
        self.assertEqual("pass", approved.status)
        self.assertIn(
            "independent-verifier",
            {item.category for item in approved.evidence},
        )

        missing_marker = child_report_with_categories(("tests",), metrics={})
        result = validate_child_report(
            profile,
            request,
            report,
            missing_marker,
            primary_execution_id="thread-primary",
            independent_execution_id="thread-verifier",
        )
        self.assertEqual("fail", result.status)
        self.assertEqual(FailureClass.RISK, result.failure_class)

        failed = validate_child_report(
            profile,
            request,
            report,
            verifier_pass_report(passed=False),
            primary_execution_id="thread-primary",
            independent_execution_id="thread-verifier",
        )
        self.assertEqual("fail", failed.status)
        self.assertEqual(FailureClass.RISK, failed.failure_class)

    def test_verifier_cannot_auto_attest_same_execution(self):
        profile = self.profiles["software"]
        request = replace(request_for("software"), impact="critical")
        report = child_report_with_categories(profile.validator_categories)
        self_attested = ChildReport(
            "pass", report.deliverable,
            report.evidence
            + (EvidenceItem("independent-verifier", True, "self attested", 0),),
            report.metrics, None, None,
        )
        with self.assertRaisesRegex(ValidationError, "main report"):
            validate_child_report(profile, request, self_attested)
        result = validate_child_report(
            profile,
            request,
            report,
            report,
            primary_execution_id="same-thread",
            independent_execution_id="same-thread",
        )
        self.assertNotEqual("pass", result.status)

    def test_independent_verifier_requires_distinct_nonblank_execution_ids(self):
        profile = self.profiles["software"]
        request = replace(request_for("software"), impact="critical")
        report = child_report_with_categories(profile.validator_categories)
        verifier = verifier_pass_report()
        invalid = (
            (None, None),
            ("thread-primary", None),
            (None, "thread-verifier"),
            ("same-thread", "same-thread"),
            ("", "thread-verifier"),
            ("thread-primary", "  "),
            (1, "thread-verifier"),
        )
        for primary_id, verifier_id in invalid:
            with self.subTest(primary_id=primary_id, verifier_id=verifier_id):
                result = validate_child_report(
                    profile,
                    request,
                    report,
                    verifier,
                    primary_execution_id=primary_id,
                    independent_execution_id=verifier_id,
                )
                self.assertEqual("fail", result.status)
                self.assertEqual(FailureClass.RISK, result.failure_class)

        result = validate_child_report(
            profile,
            request,
            report,
            verifier,
            primary_execution_id="thread-primary",
            independent_execution_id="thread-verifier",
        )
        self.assertEqual("pass", result.status)

    def test_partial_operation_always_stops_after_read_only_recovery_check(self):
        profile = self.profiles["operations"]
        request = request_for("operations")
        report = partial_operation_report()
        pending = validate_child_report(profile, request, report)
        self.assertEqual("needs-verifier", pending.status)
        self.assertEqual(FailureClass.RISK, pending.failure_class)

        recovered = validate_child_report(
            profile,
            request,
            report,
            verifier_pass_report(),
            primary_execution_id="thread-primary",
            independent_execution_id="thread-recovery",
        )
        self.assertEqual("blocked", recovered.status)
        self.assertEqual(FailureClass.RISK, recovered.failure_class)
        self.assertEqual("partial-mutation-recovery-verified", recovered.stop_reason)

        failed_recovery = validate_child_report(
            profile,
            request,
            report,
            verifier_pass_report(status="fail"),
            primary_execution_id="thread-primary",
            independent_execution_id="thread-recovery",
        )
        self.assertEqual("blocked", failed_recovery.status)
        self.assertEqual(FailureClass.RISK, failed_recovery.failure_class)

    def test_blocked_partial_operation_still_requires_recovery_verifier(self):
        profile = self.profiles["operations"]
        request = request_for("operations")
        report = child_report_with_categories(
            ("before-state",),
            status="blocked",
            failure_class=FailureClass.EXTERNAL_BLOCK,
            metrics={"partial_mutation": 1.0},
        )
        pending = validate_child_report(profile, request, report)
        self.assertEqual("needs-verifier", pending.status)
        self.assertTrue(pending.requires_independent_verifier)
        result = validate_child_report(
            profile,
            request,
            report,
            verifier_pass_report(),
            primary_execution_id="thread-primary",
            independent_execution_id="thread-recovery",
        )
        self.assertEqual("blocked", result.status)
        self.assertEqual(FailureClass.RISK, result.failure_class)

    def test_negative_partial_mutation_is_rejected(self):
        profile = self.profiles["operations"]
        request = request_for("operations")
        report = child_report_with_categories(
            profile.validator_categories,
            metrics={
                "required_checks_passed": float(len(profile.validator_categories)),
                "partial_mutation": -1.0,
            },
        )
        with self.assertRaisesRegex(ValidationError, "partial_mutation"):
            validate_child_report(profile, request, report)


class BudgetTests(unittest.TestCase):
    def test_defaults_stop_at_five_attempts(self):
        ledger = BudgetLedger(clock=FakeClock())
        self.assertEqual(5, ledger.max_attempts)
        self.assertEqual(3600.0, ledger.max_seconds)
        for _ in range(5):
            self.assertTrue(ledger.can_start())
            ledger.record_attempt()
        self.assertFalse(ledger.can_start())
        self.assertEqual("attempt-limit", ledger.stop_reason())

    def test_time_boundary_regression_and_nonfinite_clock_fail_safe(self):
        clock = FakeClock(10.0)
        ledger = BudgetLedger(clock=clock)
        clock.advance(3599.999)
        self.assertTrue(ledger.can_start())
        clock.advance(0.001)
        self.assertFalse(ledger.can_start())
        self.assertEqual(0.0, ledger.remaining_seconds())
        self.assertEqual("time-limit", ledger.stop_reason())

        for invalid in (9.0, math.nan, math.inf):
            with self.subTest(invalid=invalid):
                bad_clock = FakeClock(10.0)
                bad_ledger = BudgetLedger(clock=bad_clock)
                bad_clock.set(invalid)
                self.assertFalse(bad_ledger.can_start())
                self.assertEqual("time-limit", bad_ledger.stop_reason())

    def test_constructor_rejects_invalid_limits_and_clock(self):
        invalid_cases = (
            {"max_attempts": True},
            {"max_attempts": 0},
            {"max_attempts": 1.5},
            {"max_seconds": True},
            {"max_seconds": 0},
            {"max_seconds": math.nan},
            {"max_seconds": math.inf},
            {"clock": 1},
            {"clock": lambda: True},
        )
        for kwargs in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(BudgetError):
                    BudgetLedger(**kwargs)

    def test_constructor_cannot_raise_global_attempt_or_time_caps(self):
        for kwargs in (
            {"max_attempts": 6},
            {"max_seconds": 3600.0001},
            {"max_attempts": 6, "max_seconds": 3601},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(BudgetError):
                    BudgetLedger(clock=FakeClock(), **kwargs)
        reduced = BudgetLedger(
            max_attempts=2,
            max_seconds=30,
            clock=FakeClock(),
        )
        self.assertEqual(2, reduced.max_attempts)
        self.assertEqual(30.0, reduced.max_seconds)

    def test_record_attempt_is_atomic_and_stop_priority_is_deterministic(self):
        ledger = BudgetLedger(max_attempts=5, clock=FakeClock())
        successes = []
        lock = threading.Lock()

        def record():
            try:
                ledger.record_attempt()
            except RuntimeError:
                return
            with lock:
                successes.append(1)

        threads = [threading.Thread(target=record) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(5, len(successes))
        self.assertEqual(5, ledger.attempts)
        self.assertEqual("attempt-limit", ledger.stop_reason())


class ProgressTests(unittest.TestCase):
    def test_deterministic_and_noisy_stagnation_thresholds(self):
        deterministic = ProgressTracker(
            directions={"required_checks_passed": "max"}, noisy=False
        )
        deterministic.record({"required_checks_passed": 4})
        deterministic.record({"required_checks_passed": 4})
        self.assertTrue(deterministic.stagnated)

        noisy = ProgressTracker(
            directions={"required_checks_passed": "max"}, noisy=True
        )
        noisy.record({"required_checks_passed": 4})
        noisy.record({"required_checks_passed": 4})
        self.assertFalse(noisy.stagnated)
        noisy.record({"required_checks_passed": 4})
        self.assertTrue(noisy.stagnated)

    def test_genuine_pareto_improvement_resets_stagnation(self):
        tracker = ProgressTracker(
            directions={"passed": "max", "failures": "min"}
        )
        tracker.record({"passed": 2, "failures": 2})
        tracker.record({"passed": 3, "failures": 3})
        self.assertTrue(tracker.stagnated)
        tracker.record({"passed": 3, "failures": 1})
        self.assertFalse(tracker.stagnated)
        tracker.record({"passed": 3})
        self.assertTrue(tracker.stagnated)

    def test_only_declared_finite_metrics_and_directions_are_accepted(self):
        for directions in ({}, {"x": "up"}, {1: "max"}, [], "x"):
            with self.subTest(directions=directions):
                with self.assertRaises(ProgressError):
                    ProgressTracker(directions=directions)
        with self.assertRaises(ProgressError):
            ProgressTracker(noisy=1)

        tracker = ProgressTracker(directions={"x": "max"})
        for metrics in (
            {"x": True}, {"x": math.nan}, {"x": math.inf},
            {"x": 1, "extra": 2}, [], "x",
        ):
            with self.subTest(metrics=metrics):
                with self.assertRaises(ProgressError):
                    tracker.record(metrics)
        self.assertFalse(tracker.stagnated)

    def test_constructor_and_records_are_mutation_and_order_safe(self):
        directions = {"b": "min", "a": "max"}
        tracker = ProgressTracker(directions=directions)
        directions["a"] = "min"
        first = {"b": 2, "a": 1}
        tracker.record(first)
        first["a"] = 100
        tracker.record({"a": 2, "b": 2})
        self.assertFalse(tracker.stagnated)


class EscalationTests(unittest.TestCase):
    def choose(self, failure, current, viable=None, decision=None, history=None, request=None):
        return choose_next_route(
            failure=failure,
            current=current,
            viable=route_fixture() if viable is None else viable,
            route_decision=decision_fixture() if decision is None else decision,
            history=(current,) if history is None else history,
            request=request_fixture() if request is None else request,
        )

    def test_depth_increases_effort_same_model_but_never_enters_ultra(self):
        current = Route("gpt-5.6-terra", Effort.MEDIUM)
        decision = self.choose(FailureClass.DEPTH, current)
        self.assertEqual(Route(current.model, Effort.HIGH), decision.route)

        at_max = Route("gpt-5.6-terra", Effort.MAX)
        decision = self.choose(FailureClass.DEPTH, at_max)
        self.assertNotEqual(Effort.ULTRA, decision.route.effort)

        ultra = Route("gpt-5.6-terra", Effort.ULTRA)
        decision = self.choose(FailureClass.DEPTH, ultra)
        self.assertTrue(decision.route is None or decision.route.effort is not Effort.ULTRA)

    def test_capacity_prefers_a_different_model(self):
        current = Route("gpt-5.6-sol", Effort.LOW)
        same_model_max = next(
            item for item in decision_fixture().frontier
            if item.route.model == "gpt-5.6-sol"
        )
        other = assessment(
            "gpt-5.6-terra", "medium", quality=0.5, cost=0.5,
            latency=0.5, risk=0.5,
        )
        frontier = (same_model_max, other)
        route_decision = decision_fixture(
            maximum_safety=same_model_max.route, frontier=frontier
        )
        result = self.choose(
            FailureClass.CAPACITY, current,
            viable=(current, same_model_max.route, other.route),
            decision=route_decision,
        )
        self.assertEqual(other.route.model, result.route.model)

    def test_capacity_blocks_when_only_same_model_routes_remain(self):
        current = Route("gpt-5.6-sol", Effort.LOW)
        same_model = (
            current,
            Route(current.model, Effort.MEDIUM),
            Route(current.model, Effort.HIGH),
        )
        result = self.choose(
            FailureClass.CAPACITY,
            current,
            viable=same_model,
            history=(current,),
        )
        self.assertIsNone(result.route)
        self.assertEqual(
            "no-untried-route-with-plausible-gain",
            result.reason,
        )

    def test_coverage_uses_ultra_only_when_structurally_allowed(self):
        current = Route("gpt-5.6-sol", Effort.HIGH)
        allowed = self.choose(
            FailureClass.COVERAGE,
            current,
            viable=ultra_viable_fixture(),
            request=request_fixture(
                decomposability="high", independent_fronts=3
            ),
            history=(),
        )
        self.assertEqual(Effort.ULTRA, allowed.route.effort)

        blocked = self.choose(
            FailureClass.COVERAGE,
            current,
            viable=ultra_viable_fixture(),
            request=request_fixture(
                decomposability="limited", independent_fronts=3
            ),
            history=(),
        )
        self.assertTrue(blocked.route is None or blocked.route.effort is not Effort.ULTRA)

    def test_external_block_terminal_and_transient_retries_exactly_once(self):
        current = Route("gpt-5.6-luna", Effort.MEDIUM)
        blocked = self.choose(FailureClass.EXTERNAL_BLOCK, current)
        self.assertIsNone(blocked.route)
        self.assertEqual("external-dependency", blocked.reason)

        first = self.choose(FailureClass.TRANSIENT, current, history=(current,))
        self.assertEqual(current, first.route)
        self.assertTrue(first.retry_same_route)
        second = self.choose(
            FailureClass.TRANSIENT, current, history=(current, current)
        )
        self.assertIsNone(second.route)

    def test_never_returns_nonviable_or_repeats_quality_route(self):
        current = Route("gpt-5.6-luna", Effort.MEDIUM)
        viable = (
            current,
            Route("gpt-5.6-terra", Effort.MEDIUM),
        )
        result = self.choose(
            FailureClass.RISK, current, viable=viable,
            history=(current,),
        )
        self.assertIn(result.route, viable)
        self.assertNotEqual(current, result.route)

        exhausted = self.choose(
            FailureClass.RISK, current, viable=(current,), history=(current,)
        )
        self.assertIsNone(exhausted.route)

    def test_strict_inputs_duplicates_and_permutations(self):
        current = Route("gpt-5.6-luna", Effort.MEDIUM)
        valid = (
            current,
            Route("gpt-5.6-terra", Effort.MEDIUM),
            Route("gpt-5.6-sol", Effort.MAX),
        )
        expected = self.choose(
            FailureClass.RISK, current, viable=valid, history=(current,)
        ).route
        for permutation in itertools.permutations(valid):
            self.assertEqual(
                expected,
                self.choose(
                    FailureClass.RISK, current,
                    viable=permutation, history=(current,),
                ).route,
            )
        for invalid in (list(valid), iter(valid), {"route": current}, (current, current)):
            with self.subTest(invalid=type(invalid).__name__):
                with self.assertRaises(EscalationError):
                    self.choose(FailureClass.RISK, current, viable=invalid)

    def test_safest_route_does_not_compare_incompatible_float_axes(self):
        left = replace(
            assessment(
                "gpt-5.6-luna", "medium", quality=0.01, cost=99,
                latency=99, risk=0.99,
            ),
            comparable_groups=("cohort-a",),
        )
        right = replace(
            assessment(
                "gpt-5.6-terra", "medium", quality=0.99, cost=0.01,
                latency=0.01, risk=0.01,
            ),
            comparable_groups=("cohort-b",),
        )
        routes = (right.route, left.route)
        selected = safest_route(routes, (right, left))
        self.assertEqual(left.route, selected)

    def test_decision_is_frozen_and_reason_code_is_closed(self):
        route = Route("gpt-5.6-luna", Effort.MEDIUM)
        decision = EscalationDecision.retry(route, "single-transient-retry")
        with self.assertRaises(FrozenInstanceError):
            decision.reason = "changed"
        with self.assertRaises(EscalationError):
            EscalationDecision(route, "free-form-reason", False)


if __name__ == "__main__":
    unittest.main()
