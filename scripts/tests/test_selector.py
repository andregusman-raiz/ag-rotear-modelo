import itertools
import math
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from model_router.contracts import (  # noqa: E402
    Effort,
    EliminatedRoute,
    Route,
)
from model_router.evidence import EvidenceError, assess_routes  # noqa: E402
from model_router.profiles import QualityFloor  # noqa: E402
from model_router.selector import (  # noqa: E402
    SelectionError,
    pareto_frontier,
    select_routes,
)
from support import (  # noqa: E402
    assessment,
    benchmark_observation_fixture,
    benchmark_registry_fixture,
    benchmark_source_fixture,
    catalog_fixture,
    empty_registry_fixture,
    external_fixture,
    local_fixture,
    profile_fixture,
    request_fixture,
    route_fixture,
    stored_observation_fixture,
    two_incompatible_benchmarks_fixture,
)


class EvidenceTests(unittest.TestCase):
    def assess(self, candidates=None, request=None, profile=None, local=(), registry=None, catalog=None):
        return assess_routes(
            candidates=route_fixture() if candidates is None else candidates,
            request=request_fixture() if request is None else request,
            profile=profile_fixture() if profile is None else profile,
            local_observations=local,
            benchmark_registry=empty_registry_fixture() if registry is None else registry,
            catalog=catalog_fixture() if catalog is None else catalog,
        )

    def test_local_exact_precedes_external_and_uses_wilson_and_medians(self):
        route = Route("gpt-5.6-luna", Effort.MEDIUM)
        observations = local_fixture(route.key, passes=8, failures=2)
        result = self.assess(
            candidates=(route,),
            local=observations,
            registry=external_fixture(favoring=route.key),
        )[0]
        self.assertEqual("local-exact", result.quality_basis)
        self.assertEqual(5, result.evidence_grade)
        self.assertEqual(10, result.sample_size)
        self.assertAlmostEqual(0.4902, result.quality_signal, places=3)
        self.assertAlmostEqual(0.245, result.expected_cost, places=3)
        self.assertAlmostEqual(14.5, result.expected_latency)
        self.assertAlmostEqual(1.0 - result.quality_signal, result.residual_risk)

    def test_local_exact_requires_every_identity_dimension(self):
        candidate = Route("gpt-5.6-luna", Effort.MEDIUM)
        mismatches = (
            {"profile_id": "data"},
            {"profile_version": "2.0.0"},
            {"archetype": "other-archetype"},
            {"route": Route("gpt-5.6-luna", Effort.HIGH)},
            {"engine_version": "9.9.9"},
            {"model_version": "different-model-version"},
        )
        for overrides in mismatches:
            with self.subTest(overrides=overrides):
                observed = stored_observation_fixture(**overrides)
                result = self.assess(candidates=(candidate,), local=(observed,))[0]
                self.assertEqual("official-prior", result.quality_basis)

    def test_local_versions_and_projects_are_never_pooled(self):
        route = Route("gpt-5.6-luna", Effort.MEDIUM)
        observations = (
            stored_observation_fixture(route=route, validation_status="pass"),
            stored_observation_fixture(
                route=route,
                project_hash="b" * 64,
                validation_status="fail",
                failure_class="capacity",
                stop_reason="validation-fail",
            ),
            stored_observation_fixture(
                route=route,
                model_version="gpt-5.6-luna-2026-06-01",
                validation_status="fail",
                failure_class="capacity",
                stop_reason="validation-fail",
            ),
        )
        result = self.assess(candidates=(route,), local=observations)[0]
        self.assertEqual(1, result.sample_size)
        self.assertGreaterEqual(result.quality_signal, 0.0)
        self.assertLessEqual(result.quality_signal, 1.0)

    def test_paired_local_routes_share_only_the_environment_cohort(self):
        left = Route("gpt-5.6-luna", Effort.MEDIUM)
        right = Route("gpt-5.6-terra", Effort.MEDIUM)
        observations = (
            stored_observation_fixture(route=left),
            stored_observation_fixture(
                route=right,
                model_version="gpt-5.6-terra-2026-07-16",
            ),
        )
        results = self.assess(
            candidates=(left, right), local=observations
        )
        self.assertEqual(results[0].comparable_groups, results[1].comparable_groups)

    def test_non_pass_fail_local_status_is_not_quality_evidence(self):
        route = Route("gpt-5.6-luna", Effort.MEDIUM)
        observed = stored_observation_fixture(
            route=route,
            validation_status="blocked",
            failure_class="external_block",
            stop_reason="external-dependency",
        )
        self.assertEqual(
            "official-prior",
            self.assess(candidates=(route,), local=(observed,))[0].quality_basis,
        )

    def test_external_precedence_is_exact_then_domain_then_aggregator_then_vendor(self):
        route = Route("gpt-5.6-luna", Effort.MEDIUM)
        sources = (
            benchmark_source_fixture("vendor-source", kind="vendor"),
            benchmark_source_fixture("aggregator-source", kind="independent-aggregator"),
            benchmark_source_fixture("domain-source", kind="primary-independent"),
            benchmark_source_fixture("exact-source", kind="primary-independent"),
        )
        observations = (
            benchmark_observation_fixture("vendor-observation", "vendor-source", route=route, metric_value=99.0),
            benchmark_observation_fixture("aggregator-observation", "aggregator-source", route=route, metric_value=98.0),
            benchmark_observation_fixture(
                "domain-observation", "domain-source", route=route,
                topology="unspecified", topology_declared=False, metric_value=97.0,
            ),
            benchmark_observation_fixture("exact-observation", "exact-source", route=route, metric_value=10.0),
        )
        result = self.assess(
            candidates=(route,),
            registry=benchmark_registry_fixture(sources, observations),
        )[0]
        self.assertEqual("external-exact", result.quality_basis)
        self.assertEqual(("exact-observation",), result.evidence_ids)

    def test_primary_observation_without_exact_archetype_is_domain_evidence(self):
        route = Route("gpt-5.6-luna", Effort.MEDIUM)
        observation = benchmark_observation_fixture(
            route=route,
            profile_tags=("professional-work",),
        )
        result = self.assess(
            candidates=(route,),
            registry=benchmark_registry_fixture(observations=(observation,)),
        )[0]
        self.assertEqual("external-domain", result.quality_basis)

    def test_other_archetype_cannot_change_exact_normalization_or_sample(self):
        exact_route = Route("gpt-5.6-luna", Effort.MEDIUM)
        other_route = Route("gpt-5.6-terra", Effort.MEDIUM)
        registry = benchmark_registry_fixture(
            observations=(
                benchmark_observation_fixture(
                    "exact-bounded",
                    route=exact_route,
                    metric_value=10.0,
                    profile_tags=("professional-work", "bounded-change"),
                ),
                benchmark_observation_fixture(
                    "other-archetype",
                    route=other_route,
                    metric_value=100.0,
                    profile_tags=("professional-work", "other-archetype"),
                ),
            )
        )
        result = self.assess(candidates=(exact_route,), registry=registry)[0]
        self.assertEqual("external-exact", result.quality_basis)
        self.assertEqual(0.5, result.quality_signal)
        self.assertEqual(0.5, result.residual_risk)
        self.assertEqual(1, result.sample_size)

    def test_domain_profile_tag_identity_is_canonical_and_not_partial(self):
        target = Route("gpt-5.6-luna", Effort.MEDIUM)
        same_domain = Route("gpt-5.6-terra", Effort.MEDIUM)
        partial_domain = Route("gpt-5.6-sol", Effort.MEDIUM)
        profile = profile_fixture(
            benchmark_tags=("professional-work", "tool-use")
        )
        registry = benchmark_registry_fixture(
            observations=(
                benchmark_observation_fixture(
                    "domain-target",
                    route=target,
                    metric_value=10.0,
                    profile_tags=("tool-use", "professional-work"),
                ),
                benchmark_observation_fixture(
                    "domain-same-permuted",
                    route=same_domain,
                    metric_value=20.0,
                    profile_tags=("professional-work", "tool-use"),
                ),
                benchmark_observation_fixture(
                    "domain-partial",
                    route=partial_domain,
                    metric_value=100.0,
                    profile_tags=("professional-work",),
                ),
            )
        )
        result = self.assess(
            candidates=(target,), profile=profile, registry=registry
        )[0]
        self.assertEqual("external-domain", result.quality_basis)
        self.assertEqual(0.0, result.quality_signal)
        self.assertEqual(2, result.sample_size)

    def test_quarantine_and_retired_sources_never_influence_selection(self):
        route = Route("gpt-5.6-luna", Effort.MEDIUM)
        for status in ("quarantine", "retired"):
            with self.subTest(status=status):
                source = benchmark_source_fixture(status=status)
                registry = benchmark_registry_fixture(
                    (source,),
                    (benchmark_observation_fixture(route=route),),
                )
                result = self.assess(candidates=(route,), registry=registry)[0]
                self.assertEqual("official-prior", result.quality_basis)

    def test_source_status_change_takes_effect_without_stale_evidence(self):
        route = Route("gpt-5.6-luna", Effort.MEDIUM)
        observation = benchmark_observation_fixture(route=route)
        active = benchmark_registry_fixture(
            (benchmark_source_fixture(status="active"),), (observation,)
        )
        retired = benchmark_registry_fixture(
            (benchmark_source_fixture(status="retired"),), (observation,)
        )
        self.assertEqual("external-exact", self.assess(candidates=(route,), registry=active)[0].quality_basis)
        self.assertEqual("official-prior", self.assess(candidates=(route,), registry=retired)[0].quality_basis)

    def test_external_normalization_is_only_within_exact_comparable_group(self):
        left = Route("gpt-5.6-luna", Effort.MEDIUM)
        right = Route("gpt-5.6-terra", Effort.MEDIUM)
        observations = (
            benchmark_observation_fixture("left", route=left, metric_value=10.0),
            benchmark_observation_fixture("right", route=right, metric_value=20.0),
            benchmark_observation_fixture(
                "other-harness", route=left, harness="incompatible-harness-v2", metric_value=100.0
            ),
            benchmark_observation_fixture(
                "other-dataset", route=left, dataset="incompatible-dataset-v2", metric_value=100.0
            ),
            benchmark_observation_fixture(
                "other-metric", route=left,
                metric_name="artificial-analysis-intelligence-index", metric_value=59.0,
            ),
            benchmark_observation_fixture(
                "other-protocol", route=left, topology="unspecified",
                topology_declared=False, metric_value=100.0,
            ),
        )
        result = self.assess(
            candidates=(left,),
            registry=benchmark_registry_fixture(observations=observations),
        )[0]
        self.assertEqual(2, result.sample_size)
        self.assertEqual(0.0, result.quality_signal)
        self.assertEqual(1, len(result.comparable_groups))

    def test_zero_range_external_cohort_is_neutral_and_deterministic(self):
        left = Route("gpt-5.6-luna", Effort.MEDIUM)
        right = Route("gpt-5.6-terra", Effort.MEDIUM)
        registry = benchmark_registry_fixture(
            observations=(
                benchmark_observation_fixture("left", route=left, metric_value=50.0),
                benchmark_observation_fixture("right", route=right, metric_value=50.0),
            )
        )
        self.assertEqual(0.5, self.assess(candidates=(left,), registry=registry)[0].quality_signal)

    def test_missing_external_cost_remains_unknown(self):
        route = Route("gpt-5.6-luna", Effort.MEDIUM)
        registry = benchmark_registry_fixture(
            observations=(benchmark_observation_fixture(route=route, cost=None),)
        )
        result = self.assess(candidates=(route,), registry=registry)[0]
        self.assertIsNone(result.expected_cost)
        self.assertIsNone(result.expected_latency)

    def test_incompatible_benchmarks_are_not_summed(self):
        route = Route("gpt-5.6-luna", Effort.MEDIUM)
        result = self.assess(
            candidates=(route,), registry=two_incompatible_benchmarks_fixture()
        )[0]
        self.assertNotEqual("summed-cross-benchmark", result.quality_basis)
        self.assertEqual(1, len(result.comparable_groups))

    def test_cold_start_ood_uses_best_available_capability_at_moderate_effort(self):
        request = request_fixture(novelty="ood", verifiability="weak")
        assessments = self.assess(request=request)
        decision = select_routes(assessments, profile_fixture())
        self.assertEqual(catalog_fixture().best_capability_model.slug, decision.ideal.model)
        self.assertEqual(Effort.MEDIUM, decision.ideal.effort)

    def test_cold_start_survives_absent_and_partial_prices(self):
        for catalog in (catalog_fixture(prices=False), catalog_fixture(partial_prices=True)):
            with self.subTest(catalog=catalog):
                assessments = self.assess(catalog=catalog)
                decision = select_routes(assessments, profile_fixture())
                self.assertIn(decision.economic.key, {item.route.key for item in assessments})
                self.assertTrue(all(item.expected_cost is None for item in assessments))

    def test_cold_start_falls_back_when_requested_effort_is_unavailable(self):
        base = catalog_fixture().models[2]
        constrained = replace(
            base,
            default_effort=Effort.HIGH,
            supported_efforts=(Effort.HIGH, Effort.MAX),
        )
        catalog = catalog_fixture(models=(constrained,))
        candidates = (Route(constrained.slug, Effort.HIGH), Route(constrained.slug, Effort.MAX))
        result = self.assess(
            candidates=candidates,
            request=request_fixture(novelty="ood", verifiability="weak"),
            catalog=catalog,
        )
        decision = select_routes(result, profile_fixture())
        self.assertEqual(Effort.HIGH, decision.ideal.effort)

    def test_cold_start_roles_always_exist_for_candidate_subset(self):
        candidate = (Route("gpt-5.6-terra", Effort.MAX),)
        result = self.assess(candidates=candidate)
        self.assertEqual(
            {"economic", "ideal", "maximum-safety"}, set(result[0].prior_roles)
        )
        decision = select_routes(result, profile_fixture())
        self.assertEqual(candidate[0], decision.economic)
        self.assertEqual(candidate[0], decision.ideal)
        self.assertEqual(candidate[0], decision.maximum_safety)

    def test_balanced_prior_uses_dynamic_median_capability_price_model(self):
        result = self.assess(request=request_fixture(verifiability="mixed", reversibility="partial"))
        decision = select_routes(result, profile_fixture())
        self.assertEqual("gpt-5.6-terra", decision.ideal.model)

    def test_easy_strongly_verifiable_prior_reuses_economic_route(self):
        result = self.assess(request=request_fixture(verifiability="strong", reversibility="easy"))
        decision = select_routes(result, profile_fixture())
        self.assertEqual(decision.economic, decision.ideal)

    def test_assess_routes_rejects_hostile_inputs(self):
        valid = {
            "candidates": route_fixture(),
            "request": request_fixture(),
            "profile": profile_fixture(),
            "local_observations": (),
            "benchmark_registry": empty_registry_fixture(),
            "catalog": catalog_fixture(),
        }
        mutations = (
            ("candidates", list(route_fixture())),
            ("candidates", (item for item in route_fixture())),
            ("candidates", {"route": route_fixture()[0]}),
            ("local_observations", []),
            ("local_observations", ("not-observation",)),
            ("request", object()),
            ("profile", object()),
            ("benchmark_registry", object()),
            ("catalog", object()),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                payload = dict(valid)
                payload[field] = value
                with self.assertRaises(EvidenceError):
                    assess_routes(**payload)


class ParetoSelectionTests(unittest.TestCase):
    def frontier_fixture(self):
        return (
            assessment("gpt-5.6-luna", "low", quality=0.72, cost=0.20, latency=0.20, risk=0.28),
            assessment("gpt-5.6-terra", "medium", quality=0.85, cost=0.50, latency=0.50, risk=0.15),
            assessment("gpt-5.6-sol", "max", quality=0.94, cost=1.00, latency=1.00, risk=0.06),
        )

    def test_publishes_economic_ideal_and_maximum_safety(self):
        decision = select_routes(self.frontier_fixture(), profile_fixture())
        self.assertEqual("gpt-5.6-luna:low:single", decision.economic.key)
        self.assertEqual("gpt-5.6-terra:medium:single", decision.ideal.key)
        self.assertEqual("gpt-5.6-sol:max:single", decision.maximum_safety.key)

    def test_dominated_route_is_removed(self):
        weak = assessment("gpt-5.6-luna", "high", quality=0.70, cost=0.80, latency=0.80, risk=0.30)
        frontier = pareto_frontier(self.frontier_fixture() + (weak,))
        self.assertNotIn(weak.route.key, {item.route.key for item in frontier})

    def test_dominance_requires_all_four_known_axes(self):
        unknown_cost = assessment("gpt-5.6-luna", "medium", quality=0.90, cost=None, latency=0.30, risk=0.10)
        known = assessment("gpt-5.6-terra", "medium", quality=0.91, cost=0.40, latency=0.30, risk=0.10)
        self.assertEqual(2, len(pareto_frontier((unknown_cost, known))))

    def test_incompatible_cohorts_never_create_dominance_or_float_ranking(self):
        worse_key = replace(
            assessment("gpt-5.6-luna", "medium", quality=0.1, cost=9.0, latency=9.0, risk=0.9),
            comparable_groups=("cohort-a",),
        )
        stronger_numbers = replace(
            assessment("gpt-5.6-terra", "medium", quality=0.9, cost=1.0, latency=1.0, risk=0.1),
            comparable_groups=("cohort-b",),
        )
        frontier = pareto_frontier((worse_key, stronger_numbers))
        self.assertEqual(2, len(frontier))
        decision = select_routes(frontier, profile_fixture())
        self.assertEqual(worse_key.route, decision.economic)
        self.assertEqual(worse_key.route, decision.ideal)
        self.assertEqual(worse_key.route, decision.maximum_safety)

    def test_partial_overlap_cannot_remove_route_without_exact_partition_survivor(self):
        a = replace(
            assessment("gpt-5.6-sol", "medium", quality=0.9, cost=1.0, latency=1.0, risk=0.1),
            comparable_groups=("X",),
        )
        b = replace(
            assessment("gpt-5.6-terra", "medium", quality=0.8, cost=2.0, latency=2.0, risk=0.2),
            comparable_groups=("X", "Y"),
        )
        c = replace(
            assessment("gpt-5.6-luna", "medium", quality=0.7, cost=3.0, latency=3.0, risk=0.3),
            comparable_groups=("Y",),
        )
        expected = tuple(sorted(item.route.key for item in (a, b, c)))
        for permutation in itertools.permutations((a, b, c)):
            with self.subTest(order=tuple(item.route.key for item in permutation)):
                frontier = pareto_frontier(permutation)
                self.assertEqual(expected, tuple(item.route.key for item in frontier))

    def test_exact_partition_chain_has_comparable_surviving_dominator(self):
        a = replace(
            assessment("gpt-5.6-sol", "high", quality=0.9, cost=1.0, latency=1.0, risk=0.1),
            comparable_groups=("Y", "X"),
        )
        b = replace(
            assessment("gpt-5.6-terra", "high", quality=0.8, cost=2.0, latency=2.0, risk=0.2),
            comparable_groups=("X", "Y"),
        )
        c = replace(
            assessment("gpt-5.6-luna", "high", quality=0.7, cost=3.0, latency=3.0, risk=0.3),
            comparable_groups=("X", "Y"),
        )
        outsider = replace(
            assessment("gpt-5.6-luna", "xhigh", quality=0.1, cost=9.0, latency=9.0, risk=0.9),
            comparable_groups=("Z",),
        )
        expected = tuple(sorted((a.route.key, outsider.route.key)))
        for permutation in itertools.permutations((a, b, c, outsider)):
            frontier = pareto_frontier(permutation)
            self.assertEqual(expected, tuple(item.route.key for item in frontier))

    def test_overlap_graph_cycle_cannot_propagate_dominance(self):
        a = replace(
            assessment("gpt-5.6-sol", "xhigh", quality=0.9, cost=1.0, latency=1.0, risk=0.1),
            comparable_groups=("X", "Y"),
        )
        b = replace(
            assessment("gpt-5.6-terra", "xhigh", quality=0.8, cost=2.0, latency=2.0, risk=0.2),
            comparable_groups=("Y", "Z"),
        )
        c = replace(
            assessment("gpt-5.6-luna", "xhigh", quality=0.7, cost=3.0, latency=3.0, risk=0.3),
            comparable_groups=("Z", "X"),
        )
        expected = tuple(sorted(item.route.key for item in (a, b, c)))
        for permutation in itertools.permutations((a, b, c)):
            frontier = pareto_frontier(permutation)
            self.assertEqual(expected, tuple(item.route.key for item in frontier))

    def test_permutation_invariance_and_sorted_immutable_frontier(self):
        expected = tuple(item.route.key for item in pareto_frontier(self.frontier_fixture()))
        for permutation in itertools.permutations(self.frontier_fixture()):
            with self.subTest(order=tuple(item.route.key for item in permutation)):
                actual = pareto_frontier(permutation)
                self.assertIsInstance(actual, tuple)
                self.assertEqual(expected, tuple(item.route.key for item in actual))
                self.assertEqual(
                    select_routes(self.frontier_fixture(), profile_fixture()).to_dict(),
                    select_routes(permutation, profile_fixture()).to_dict(),
                )

    def test_single_route_can_fill_all_three_choices(self):
        only = (self.frontier_fixture()[0],)
        decision = select_routes(only, profile_fixture())
        self.assertEqual(decision.economic, decision.ideal)
        self.assertEqual(decision.ideal, decision.maximum_safety)

    def test_empty_and_duplicate_assessments_fail_safely(self):
        with self.assertRaisesRegex(SelectionError, "no viable frontier"):
            select_routes((), profile_fixture())
        duplicate = self.frontier_fixture()[0]
        with self.assertRaisesRegex(SelectionError, "duplicate"):
            pareto_frontier((duplicate, duplicate))

    def test_ties_and_zero_ranges_use_route_key_deterministically(self):
        tied = (
            assessment("gpt-5.6-terra", "low", quality=0.5, cost=1.0, latency=1.0, risk=0.5),
            assessment("gpt-5.6-luna", "low", quality=0.5, cost=1.0, latency=1.0, risk=0.5),
        )
        decision = select_routes(tied, profile_fixture())
        self.assertEqual("gpt-5.6-luna:low:single", decision.economic.key)
        self.assertEqual("gpt-5.6-luna:low:single", decision.ideal.key)
        self.assertEqual("gpt-5.6-luna:low:single", decision.maximum_safety.key)

    def test_economic_honors_capability_floor_and_unknown_cost_is_last(self):
        floor = QualityFloor(("correctness", "tool-use"))
        profile = profile_fixture(quality_floor=floor)
        cheap_missing = assessment(
            "gpt-5.6-luna", "low", quality=0.9, cost=0.1, latency=0.1,
            risk=0.1, capabilities=("correctness",),
        )
        unknown = assessment(
            "gpt-5.6-terra", "low", quality=0.9, cost=None, latency=0.1,
            risk=0.1, capabilities=("correctness", "tool-use"),
        )
        eligible = assessment(
            "gpt-5.6-sol", "low", quality=0.95, cost=0.8, latency=0.2,
            risk=0.05, capabilities=("correctness", "tool-use"),
        )
        decision = select_routes((cheap_missing, unknown, eligible), profile)
        self.assertEqual(eligible.route, decision.economic)

    def test_no_capability_floor_match_marks_economic_assessment_provisional(self):
        profile = profile_fixture(quality_floor=QualityFloor(("unobserved-capability",)))
        decision = select_routes(self.frontier_fixture(), profile)
        economic = next(item for item in decision.frontier if item.route == decision.economic)
        self.assertTrue(economic.provisional)

    def test_ideal_ignores_axes_not_comparable_across_entire_frontier(self):
        left = assessment("gpt-5.6-luna", "low", quality=0.8, cost=None, latency=0.2, risk=0.2)
        right = assessment("gpt-5.6-terra", "low", quality=0.8, cost=0.5, latency=0.2, risk=0.2)
        decision = select_routes((right, left), profile_fixture())
        self.assertEqual(left.route, decision.ideal)

    def test_safety_prefers_evidence_quality_risk_then_lower_cost(self):
        low_grade = assessment("gpt-5.6-luna", "low", quality=1.0, cost=0.1, latency=0.1, risk=0.2, evidence_grade=3)
        high_grade = assessment("gpt-5.6-terra", "low", quality=0.8, cost=0.5, latency=0.5, risk=0.05, evidence_grade=5)
        decision = select_routes((low_grade, high_grade), profile_fixture())
        self.assertEqual(high_grade.route, decision.maximum_safety)

    def test_eliminated_routes_and_reasons_are_preserved_immutably(self):
        eliminated = (EliminatedRoute(Route("gpt-5.6-luna", Effort.MAX), "quality floor"),)
        decision = select_routes(self.frontier_fixture(), profile_fixture(), eliminated)
        self.assertEqual(eliminated, decision.eliminated)
        self.assertEqual("quality floor", decision.eliminated[0].reason)
        with self.assertRaises(FrozenInstanceError):
            decision.eliminated[0].reason = "changed"
        with self.assertRaises(TypeError):
            decision.rationale["ideal"] = "changed"

    def test_official_prior_missing_roles_fails_explicitly(self):
        broken = (
            assessment(
                "gpt-5.6-luna", "low", quality_basis="official-prior",
                evidence_grade=1, capabilities=(), prior_roles=(), provisional=True,
            ),
        )
        with self.assertRaisesRegex(SelectionError, "official prior role"):
            select_routes(broken, profile_fixture())

    def test_hostile_iterables_and_numeric_states_are_rejected(self):
        for hostile in (list(self.frontier_fixture()), iter(self.frontier_fixture()), {"x": self.frontier_fixture()[0]}, "bad"):
            with self.subTest(hostile=type(hostile).__name__):
                with self.assertRaises(SelectionError):
                    pareto_frontier(hostile)
        for bad in (math.nan, math.inf, True, "0.5", -0.1):
            with self.subTest(bad=bad):
                corrupted = self.frontier_fixture()[0]
                object.__setattr__(corrupted, "expected_cost", bad)
                try:
                    with self.assertRaises(SelectionError):
                        pareto_frontier((corrupted,))
                finally:
                    object.__setattr__(corrupted, "expected_cost", 0.2)

    def test_eliminated_input_must_be_typed_unique_and_disjoint(self):
        assessments = self.frontier_fixture()
        with self.assertRaises(SelectionError):
            select_routes(assessments, profile_fixture(), [])
        item = EliminatedRoute(Route("gpt-5.6-luna", Effort.MAX), "risk floor")
        with self.assertRaisesRegex(SelectionError, "duplicate"):
            select_routes(assessments, profile_fixture(), (item, item))
        overlapping = EliminatedRoute(assessments[0].route, "route eliminated")
        with self.assertRaisesRegex(SelectionError, "both viable and eliminated"):
            select_routes(assessments, profile_fixture(), (overlapping,))


if __name__ == "__main__":
    unittest.main()
