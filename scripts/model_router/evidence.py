import hashlib
import math
import statistics
from collections import defaultdict
from datetime import date
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Tuple

from . import __version__
from .contracts import Effort, Route, RouteAssessment, TaskRequest
from .model_registry import CatalogSnapshot, ModelSpec
from .profiles import Profile
from .registry import (
    BenchmarkObservation,
    BenchmarkRegistry,
    BenchmarkSource,
)
from .state import StoredObservation


class EvidenceError(ValueError):
    pass


EVIDENCE_PRECEDENCE = MappingProxyType(
    {
        "retired": 0,
        "vendor": 1,
        "independent-aggregator": 2,
        "external-domain": 3,
        "external-exact": 4,
        "local-exact": 5,
    }
)

_METRIC_DIRECTIONS = MappingProxyType(
    {
        "arc-agi-1-public-eval": "max",
        "arc-agi-2-public-eval": "max",
        "arc-agi-3-public-demo": "max",
        "task-completed-correctly": "max",
        "artificial-analysis-intelligence-index": "max",
    }
)
_EFFORT_ORDER = (
    Effort.LOW,
    Effort.MEDIUM,
    Effort.HIGH,
    Effort.XHIGH,
    Effort.MAX,
    Effort.ULTRA,
)
_EFFORT_INDEX = {effort: index for index, effort in enumerate(_EFFORT_ORDER)}


def _typed_tuple(value, item_type, path: str, unique_key=None):
    if type(value) is not tuple:
        raise EvidenceError("%s must be a tuple" % path)
    seen = set()
    for index, item in enumerate(value):
        if type(item) is not item_type:
            raise EvidenceError(
                "%s[%d] must be %s" % (path, index, item_type.__name__)
            )
        if unique_key is not None:
            key = unique_key(item)
            if key in seen:
                raise EvidenceError("%s[%d] is a duplicate" % (path, index))
            seen.add(key)
    return tuple(value)


def _validate_inputs(
    candidates,
    request,
    profile,
    local_observations,
    benchmark_registry,
    catalog,
):
    routes = _typed_tuple(candidates, Route, "candidates", lambda item: item.key)
    if type(request) is not TaskRequest:
        raise EvidenceError("request must be TaskRequest")
    if type(profile) is not Profile:
        raise EvidenceError("profile must be Profile")
    local = _typed_tuple(
        local_observations,
        StoredObservation,
        "local_observations",
    )
    if type(benchmark_registry) is not BenchmarkRegistry:
        raise EvidenceError("benchmark_registry must be BenchmarkRegistry")
    if type(catalog) is not CatalogSnapshot:
        raise EvidenceError("catalog must be CatalogSnapshot")
    models = {model.slug: model for model in catalog.models}
    for index, route in enumerate(routes):
        model = models.get(route.model)
        if model is None:
            raise EvidenceError(
                "candidates[%d].model is absent from catalog" % index
            )
        if route.effort not in model.supported_efforts:
            raise EvidenceError(
                "candidates[%d].effort is absent from catalog" % index
            )
    return routes, local, models


def _model_version_matches(route: Route, value: str) -> bool:
    return value == route.model or value.startswith(route.model + "-")


def _local_group_key(observation: StoredObservation) -> Tuple[str, ...]:
    return (
        observation.project_hash,
        observation.profile_id,
        observation.profile_version,
        observation.archetype,
        observation.route.key,
        observation.engine_version,
        observation.model_version,
    )


def _local_comparable_key(observation: StoredObservation) -> Tuple[str, ...]:
    return (
        observation.project_hash,
        observation.profile_id,
        observation.profile_version,
        observation.archetype,
        observation.engine_version,
    )


def _local_group(
    observations: Tuple[StoredObservation, ...],
    route: Route,
    request: TaskRequest,
    profile: Profile,
) -> Tuple[StoredObservation, ...]:
    groups = defaultdict(list)
    for observation in observations:
        if observation.validation_status not in ("pass", "fail"):
            continue
        if observation.profile_id != profile.id:
            continue
        if observation.profile_version != profile.version:
            continue
        if observation.archetype != request.archetype:
            continue
        if observation.route != route:
            continue
        if observation.engine_version != __version__:
            continue
        if not _model_version_matches(route, observation.model_version):
            continue
        groups[_local_group_key(observation)].append(observation)
    if not groups:
        return ()
    ordered = sorted(
        groups.items(),
        key=lambda item: (
            item[0][-1],
            len(item[1]),
            item[0][0],
        ),
        reverse=True,
    )
    return tuple(ordered[0][1])


def _wilson_lower_bound(passes: int, total: int) -> float:
    if total <= 0:
        raise EvidenceError("local cohort must contain pass/fail evidence")
    z = 1.959963984540054
    proportion = passes / float(total)
    z_squared = z * z
    denominator = 1.0 + z_squared / total
    center = proportion + z_squared / (2.0 * total)
    margin = z * math.sqrt(
        (proportion * (1.0 - proportion) + z_squared / (4.0 * total))
        / total
    )
    return max(0.0, (center - margin) / denominator)


def _median_known(values: Iterable[Optional[float]]) -> Optional[float]:
    known = tuple(value for value in values if value is not None)
    return None if not known else float(statistics.median(known))


def _group_digest(parts: Tuple[str, ...]) -> str:
    document = "\x1f".join(parts).encode("utf-8")
    return "cohort:%s" % hashlib.sha256(document).hexdigest()


def _assessment_from_local(
    route: Route,
    group: Tuple[StoredObservation, ...],
    profile: Profile,
) -> RouteAssessment:
    passes = sum(item.validation_status == "pass" for item in group)
    quality = _wilson_lower_bound(passes, len(group))
    key = _local_comparable_key(group[0])
    return RouteAssessment(
        route=route,
        evidence_grade=EVIDENCE_PRECEDENCE["local-exact"],
        quality_signal=quality,
        quality_basis="local-exact",
        expected_cost=_median_known(item.observed_cost_usd for item in group),
        expected_latency=_median_known(item.duration_seconds for item in group),
        residual_risk=1.0 - quality,
        sample_size=len(group),
        comparable_groups=(_group_digest(key),),
        capabilities=profile.quality_floor.required_capabilities,
        prior_roles=(),
        provisional=False,
        evidence_ids=("local:%s:%s" % (group[0].engine_version, group[0].model_version),),
    )


def _metric_direction(metric_name: str) -> str:
    direction = _METRIC_DIRECTIONS.get(metric_name)
    if direction not in ("max", "min"):
        raise EvidenceError("benchmark metric direction is not declared")
    return direction


def _external_scope_key(
    observation: BenchmarkObservation,
    request: TaskRequest,
    profile: Profile,
) -> Tuple[str, ...]:
    if request.archetype in observation.profile_tags:
        return ("exact-archetype", request.archetype)
    compatible_tags = tuple(
        sorted(set(profile.benchmark_tags).intersection(observation.profile_tags))
    )
    if not compatible_tags:
        raise EvidenceError("domain evidence has no compatible profile tag")
    return ("domain",) + compatible_tags


def _external_group_key(
    observation: BenchmarkObservation,
    request: TaskRequest,
    profile: Profile,
) -> Tuple[str, ...]:
    return (
        observation.source_id,
        observation.benchmark,
        observation.metric.name,
        observation.metric.unit,
        _metric_direction(observation.metric.name),
        observation.harness,
        observation.dataset,
        observation.topology,
        "declared" if observation.topology_declared else "unspecified",
        observation.evaluated_at or "undated",
    ) + _external_scope_key(observation, request, profile)


def _external_basis(
    source: BenchmarkSource,
    observation: BenchmarkObservation,
    route: Route,
    request: TaskRequest,
) -> Tuple[str, int]:
    if source.kind == "vendor":
        basis = "vendor"
    elif source.kind == "independent-aggregator":
        basis = "independent-aggregator"
    elif (
        observation.topology_declared
        and observation.topology == route.topology.value
        and request.archetype in observation.profile_tags
    ):
        basis = "external-exact"
    else:
        basis = "external-domain"
    return basis, EVIDENCE_PRECEDENCE[basis]


def _date_rank(value: Optional[str]) -> int:
    return 0 if value is None else date.fromisoformat(value).toordinal()


def _normalized_metric(
    observation: BenchmarkObservation,
    group: Tuple[BenchmarkObservation, ...],
) -> float:
    direction = _metric_direction(observation.metric.name)
    values = tuple(float(item.metric.value) for item in group)
    low = min(values)
    high = max(values)
    if high == low:
        return 0.5
    raw = (float(observation.metric.value) - low) / (high - low)
    return raw if direction == "max" else 1.0 - raw


def _external_choice(
    route: Route,
    request: TaskRequest,
    profile: Profile,
    registry: BenchmarkRegistry,
):
    sources = {source.id: source for source in registry.sources}
    tags = frozenset(profile.benchmark_tags)
    active = tuple(
        observation
        for observation in registry.observations
        if sources[observation.source_id].status == "active"
        and tags.intersection(observation.profile_tags)
    )
    by_group = defaultdict(list)
    for observation in active:
        by_group[_external_group_key(observation, request, profile)].append(
            observation
        )
    choices = []
    for observation in active:
        if observation.route != route:
            continue
        if observation.topology_declared:
            if observation.topology != route.topology.value:
                continue
        elif route.effort is Effort.ULTRA:
            continue
        source = sources[observation.source_id]
        basis, grade = _external_basis(source, observation, route, request)
        group_key = _external_group_key(observation, request, profile)
        group = tuple(by_group[group_key])
        specificity = (
            (1 if request.archetype in observation.profile_tags else 0),
            len(tags.intersection(observation.profile_tags)),
        )
        choices.append(
            (
                -grade,
                -specificity[0],
                -specificity[1],
                -_date_rank(observation.evaluated_at),
                -len(group),
                _group_digest(group_key),
                observation.id,
                observation,
                group,
                basis,
                grade,
            )
        )
    if not choices:
        return None
    return sorted(choices, key=lambda item: item[:7])[0]


def _assessment_from_external(
    route: Route,
    profile: Profile,
    choice,
) -> RouteAssessment:
    observation = choice[7]
    group = choice[8]
    basis = choice[9]
    grade = choice[10]
    quality = _normalized_metric(observation, group)
    matching = tuple(item for item in group if item.route == route)
    costs = tuple(
        item.cost.value
        for item in matching
        if item.cost is not None
    )
    capabilities = (
        profile.quality_floor.required_capabilities
        if grade >= EVIDENCE_PRECEDENCE["external-domain"]
        else ()
    )
    return RouteAssessment(
        route=route,
        evidence_grade=grade,
        quality_signal=quality,
        quality_basis=basis,
        expected_cost=None if not costs else float(statistics.median(costs)),
        expected_latency=None,
        residual_risk=1.0 - quality,
        sample_size=len(group),
        comparable_groups=(choice[5],),
        capabilities=capabilities,
        prior_roles=(),
        provisional=True,
        evidence_ids=tuple(sorted(item.id for item in matching)),
    )


def _nearest_route(
    model: ModelSpec,
    desired: Effort,
    candidates: Tuple[Route, ...],
) -> Route:
    available = tuple(route for route in candidates if route.model == model.slug)
    if not available:
        raise EvidenceError("catalog model has no candidate route")
    return min(
        available,
        key=lambda route: (
            abs(_EFFORT_INDEX[route.effort] - _EFFORT_INDEX[desired]),
            _EFFORT_INDEX[route.effort],
            route.key,
        ),
    )


def _official_roles(
    request: TaskRequest,
    models: Mapping[str, ModelSpec],
    candidates: Tuple[Route, ...],
) -> Mapping[str, Route]:
    candidate_models = tuple(
        sorted(
            (model for model in models.values() if any(route.model == model.slug for route in candidates)),
            key=lambda item: (item.priority, item.slug),
        )
    )
    if not candidate_models:
        raise EvidenceError("official prior has no candidate model")
    fully_priced = tuple(
        model
        for model in candidate_models
        if model.input_price_per_million is not None
        and model.output_price_per_million is not None
    )
    if fully_priced:
        economic_model = min(
            fully_priced,
            key=lambda item: (
                item.input_price_per_million + item.output_price_per_million,
                item.priority,
                item.slug,
            ),
        )
    else:
        economic_model = max(
            candidate_models,
            key=lambda item: (item.priority, item.slug),
        )
    best_model = min(candidate_models, key=lambda item: (item.priority, item.slug))
    capability_price_order = tuple(
        sorted(
            candidate_models,
            key=lambda item: (
                item.priority,
                (
                    item.input_price_per_million + item.output_price_per_million
                    if item.input_price_per_million is not None
                    and item.output_price_per_million is not None
                    else math.inf
                ),
                item.slug,
            ),
        )
    )
    median_model = capability_price_order[len(capability_price_order) // 2]
    economic = _nearest_route(economic_model, economic_model.default_effort, candidates)
    best_routes = tuple(route for route in candidates if route.model == best_model.slug)
    single = tuple(route for route in best_routes if route.effort is not Effort.ULTRA)
    safety_pool = single or best_routes
    if not safety_pool:
        raise EvidenceError("official prior has no safety route")
    safety = max(
        safety_pool,
        key=lambda route: (_EFFORT_INDEX[route.effort], route.key),
    )
    if request.novelty == "ood" and not (
        request.strongly_verifiable and request.reversibility == "easy"
    ):
        ideal = _nearest_route(best_model, Effort.MEDIUM, candidates)
    elif request.strongly_verifiable and request.reversibility == "easy":
        ideal = economic
    else:
        ideal = _nearest_route(median_model, median_model.default_effort, candidates)
    return MappingProxyType(
        {"economic": economic, "ideal": ideal, "maximum-safety": safety}
    )


def _assessment_from_official(
    route: Route,
    roles: Mapping[str, Route],
) -> RouteAssessment:
    prior_roles = tuple(name for name, selected in roles.items() if selected == route)
    return RouteAssessment(
        route=route,
        evidence_grade=EVIDENCE_PRECEDENCE["vendor"],
        quality_signal=None,
        quality_basis="official-prior",
        expected_cost=None,
        expected_latency=None,
        residual_risk=None,
        sample_size=0,
        comparable_groups=(),
        capabilities=(),
        prior_roles=prior_roles,
        provisional=True,
        evidence_ids=("openai-gpt-5-6",),
    )


def assess_routes(
    candidates: Tuple[Route, ...],
    request: TaskRequest,
    profile: Profile,
    local_observations: Tuple[StoredObservation, ...],
    benchmark_registry: BenchmarkRegistry,
    catalog: CatalogSnapshot,
) -> Tuple[RouteAssessment, ...]:
    routes, local, models = _validate_inputs(
        candidates,
        request,
        profile,
        local_observations,
        benchmark_registry,
        catalog,
    )
    if not routes:
        return ()
    roles = _official_roles(request, models, routes)
    assessments = []
    for route in sorted(routes, key=lambda item: item.key):
        local_group = _local_group(local, route, request, profile)
        if local_group:
            assessments.append(_assessment_from_local(route, local_group, profile))
            continue
        external = _external_choice(route, request, profile, benchmark_registry)
        if external is not None:
            assessments.append(_assessment_from_external(route, profile, external))
            continue
        assessments.append(_assessment_from_official(route, roles))
    return tuple(assessments)
