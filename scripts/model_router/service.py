import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Tuple

from . import __version__
from .contracts import (
    ApprovalPolicy,
    EliminatedRoute,
    FailureClass,
    Route,
    RouteDecision,
    SandboxMode,
    TaskRequest,
    ValidationResult,
)
from .evidence import assess_routes
from .escalation import BudgetLedger, ProgressTracker, choose_next_route
from .gates import apply_gates
from .model_registry import CatalogSnapshot, generate_candidates
from .profiles import Profile, resolve_profile
from .selector import select_routes
from .state import Observation, RuntimeState


class RecursionBlocked(RuntimeError):
    pass


def classify_execution_failure(execution) -> ValidationResult:
    failure_kind = execution.failure_kind
    if failure_kind in ("timeout", "spawn"):
        return ValidationResult(
            "fail", (), {}, FailureClass.TRANSIENT, None, False
        )
    if failure_kind == "missing-agent-message":
        return ValidationResult("fail", (), {}, FailureClass.DEPTH, None, False)
    return ValidationResult(
        "blocked",
        (),
        {},
        FailureClass.EXTERNAL_BLOCK,
        "external-dependency",
        False,
    )


@dataclass(frozen=True)
class SelectionResult:
    decision: Optional[RouteDecision]
    decision_path: Path
    status: str
    stop_reason: Optional[str]


@dataclass(frozen=True)
class RunResult:
    status: str
    stop_reason: Optional[str]
    initial_decision: Optional[RouteDecision]
    decision_path: Path
    attempt_count: int
    final_content: Optional[str]
    route: Optional[Route]
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class _SelectionContext:
    request: TaskRequest
    profile: Profile
    catalog: CatalogSnapshot
    candidates: Tuple[Route, ...]
    viable: Tuple[Route, ...]
    eliminated: Tuple[EliminatedRoute, ...]
    decision: Optional[RouteDecision]
    blocked_reason: Optional[str]


@dataclass(frozen=True)
class _AttemptAudit:
    route: Route
    validation_status: str
    failure_class: Optional[FailureClass]
    reason_code: str
    evidence_ids: Tuple[str, ...]
    escalation: int


_PERSISTED_REASONS = frozenset(
    (
        "approved",
        "attempt-limit",
        "budget-exhausted",
        "completed",
        "critical-task-without-validator",
        "executor-technical-failure",
        "external-dependency",
        "external-effects-not-authorized",
        "no-progress",
        "no-untried-route-with-plausible-gain",
        "partial-mutation-recovery-failed",
        "partial-mutation-recovery-invalid",
        "partial-mutation-recovery-verified",
        "required-tools-unsupported",
        "time-limit",
        "user-stopped",
        "validator-technical-failure",
        "verifier-budget-exhausted",
        "verifier-provenance-invalid",
    )
)
_TOKENS_PER_MILLION = 1_000_000.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _evidence_digest(domain: str, value: str) -> str:
    document = (
        ("ag-model-router:v1:%s:" % domain).encode("ascii")
        + value.encode("utf-8")
    )
    return "sha256:evidence:%s" % hashlib.sha256(document).hexdigest()


def _terminal_reason(value: Optional[str]) -> str:
    if value == "blocked":
        return "external-dependency"
    if value in _PERSISTED_REASONS:
        return value
    return "no-progress" if value is None else "external-dependency"


def _elimination_reason(value: str) -> str:
    structural = value.split(":", 1)[0]
    if structural in (
        "parallel-write-without-worktree",
        "required-tools-unsupported",
        "ultra-without-useful-decomposition",
    ):
        return structural
    return "route-eliminated"


def _selection_rationales(decision: RouteDecision) -> Mapping[str, str]:
    cold_start = all(
        item.quality_basis == "official-prior" for item in decision.frontier
    )
    if cold_start:
        return {
            "economic": "official-price-prior",
            "ideal": "structural-cold-start-prior",
            "maximum_safety": "official-capability-prior",
        }
    return {
        "economic": "lowest-cost-quality-floor",
        "ideal": "minimum-normalized-regret",
        "maximum_safety": "highest-evidence-quality-safety",
    }


def _blocked_validation(
    reason: str,
    failure_class: FailureClass = FailureClass.EXTERNAL_BLOCK,
) -> ValidationResult:
    return ValidationResult("blocked", (), {}, failure_class, reason, False)


def _valid_execution_id(value) -> bool:
    return type(value) is str and bool(value) and value.strip() == value


class RouterService:
    def __init__(
        self,
        state: RuntimeState,
        profiles: Mapping[str, Profile],
        catalog_provider: Callable[[], CatalogSnapshot],
        executor,
        budget,
        validator,
    ):
        self.state = state
        self.profiles = dict(profiles)
        self.catalog_provider = catalog_provider
        self.executor = executor
        self._budget_template = budget
        self.budget = budget
        self.validator = validator

    def _reset_budget(self) -> None:
        template = self._budget_template
        self.budget = BudgetLedger(
            max_attempts=template.max_attempts,
            max_seconds=template.max_seconds,
            clock=template.clock,
        )

    @staticmethod
    def guard_recursion(task_text: str) -> None:
        if os.environ.get("AG_MODEL_ROUTER_CHILD") == "1" or (
            type(task_text) is str and "[AG_MODEL_ROUTER_CHILD=1]" in task_text
        ):
            raise RecursionBlocked("routing is unavailable during child execution")

    def _select_context(
        self,
        request: TaskRequest,
        workdir: Path,
    ) -> _SelectionContext:
        self.state.bootstrap()
        catalog = self.catalog_provider()
        profile = resolve_profile(request, self.profiles)
        candidates = generate_candidates(catalog)
        gates = apply_gates(
            request,
            candidates,
            profile,
            self.budget.can_start(),
            catalog=catalog,
        )
        eliminated = tuple(
            EliminatedRoute(route, gates.eliminated[route.key])
            for route in candidates
            if route.key in gates.eliminated
        )
        if gates.global_blockers or not gates.viable:
            blocked_reason = (
                gates.global_blockers[0]
                if gates.global_blockers
                else _elimination_reason(eliminated[0].reason)
            )
            return _SelectionContext(
                request,
                profile,
                catalog,
                candidates,
                gates.viable,
                eliminated,
                None,
                blocked_reason,
            )
        assessments = assess_routes(
            gates.viable,
            request,
            profile,
            self.state.local_observations(str(workdir)),
            self.state.benchmark_registry(),
            catalog,
        )
        decision = select_routes(assessments, profile, eliminated=eliminated)
        return _SelectionContext(
            request,
            profile,
            catalog,
            candidates,
            gates.viable,
            eliminated,
            decision,
            None,
        )

    def _safe_evidence_ids(
        self,
        context: _SelectionContext,
        audit: Sequence[_AttemptAudit],
    ) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        registry = self.state.benchmark_registry()
        known = frozenset(
            [source.id for source in registry.sources]
            + [item.id for item in registry.observations]
        )
        catalog_ids = []
        if context.decision is not None:
            for assessment in context.decision.frontier:
                for evidence_id in assessment.evidence_ids:
                    catalog_ids.append(
                        evidence_id
                        if evidence_id in known
                        else _evidence_digest("route-evidence", evidence_id)
                    )
        execution_ids = [
            evidence_id
            for entry in audit
            for evidence_id in entry.evidence_ids
        ]

        def unique(items):
            return tuple(dict.fromkeys(items))

        return unique(catalog_ids), unique(catalog_ids + execution_ids)

    def _write_decision(
        self,
        context: _SelectionContext,
        workdir: Path,
        status: str,
        stop_reason: str,
        selected_route: Optional[Route],
        audit: Sequence[_AttemptAudit],
        validation: ValidationResult,
        input_tokens: int,
        output_tokens: int,
        started_at: str,
    ) -> Path:
        registry = self.state.benchmark_registry()
        catalog_evidence, all_evidence = self._safe_evidence_ids(context, audit)
        model_metadata = json.loads(
            self.state.model_registry_path.read_text(encoding="utf-8")
        )
        known_registry_ids = frozenset(
            [source.id for source in registry.sources]
            + [item.id for item in registry.observations]
        )
        benchmark_ids = tuple(
            item for item in catalog_evidence if item in known_registry_ids
        )
        persisted_stop = _terminal_reason(stop_reason)
        validation_metrics = dict(validation.metrics)
        required_checks = int(
            validation_metrics.get("required_checks_passed", 0.0)
        )
        if context.decision is None:
            selection = {"blocked": persisted_stop}
            selected = {"blocked": persisted_stop}
        else:
            selection = {
                "economic": context.decision.economic.key,
                "ideal": context.decision.ideal.key,
                "maximum_safety": context.decision.maximum_safety.key,
                "frontier": [
                    item.route.key for item in context.decision.frontier
                ],
                "rationale_codes": dict(
                    _selection_rationales(context.decision)
                ),
            }
            selected = {"selected": selected_route.key}
        payload = {
            "schema_version": "1.0.0",
            "fingerprint": {
                "project_hash": self.state.project_hash(str(workdir)),
                "primary_profile": context.request.primary_profile,
                "secondary_profiles": list(context.request.secondary_profiles),
                "archetype": context.request.archetype,
                "novelty": context.request.novelty,
                "ambiguity": context.request.ambiguity,
                "reasoning_depth": context.request.reasoning_depth,
                "context_load": context.request.context_load,
                "tool_dependency": context.request.tool_dependency,
                "urgency": context.request.urgency,
                "cost_tolerance": context.request.cost_tolerance,
                "latency_tolerance": context.request.latency_tolerance,
                "verifiability": context.request.verifiability,
                "impact": context.request.impact,
                "reversibility": context.request.reversibility,
                "external_effects": context.request.external_effects,
                "external_effects_authorized": (
                    context.request.external_effects_authorized
                ),
                "decomposability": context.request.decomposability,
                "independent_fronts": context.request.independent_fronts,
                "parallel_writes": context.request.parallel_writes,
                "worktree_isolated": context.request.worktree_isolated,
                "required_tools": list(context.request.required_tools),
                "validation_checks": [
                    item.to_dict() for item in context.request.validation_checks
                ],
            },
            "profile": {
                "id": context.profile.id,
                "version": context.profile.version,
            },
            "catalog": {
                "source": context.catalog.source,
                "schema_version": registry.schema_version,
                "model_registry_version": model_metadata["schema_version"],
                "benchmark_registry_version": registry.schema_version,
                "verified_at": registry.verified_at,
                "model_ids": [model.slug for model in context.catalog.models],
                "benchmark_ids": list(benchmark_ids),
                "evidence_ids": list(catalog_evidence),
            },
            "eliminated": [
                {
                    "route": item.route.key,
                    "reason_code": _elimination_reason(item.reason),
                }
                for item in context.eliminated
            ],
            "selection": selection,
            "decision": selected,
            "evidence_ids": list(all_evidence),
            "history": [
                {
                    "route": item.route.key,
                    "validation_status": item.validation_status,
                    "failure_class": (
                        item.failure_class.value
                        if item.failure_class is not None
                        else None
                    ),
                    "reason_code": item.reason_code,
                    "evidence_ids": list(item.evidence_ids),
                    "escalation": item.escalation,
                }
                for item in audit
            ],
            "validation": {
                "status": validation.status,
                "failure_class": (
                    validation.failure_class.value
                    if validation.failure_class is not None
                    else None
                ),
                "stop_reason": persisted_stop,
                "required_checks_passed": required_checks,
                "metrics": validation_metrics,
                "evidence_ids": list(
                    dict.fromkeys(
                        evidence_id
                        for item in audit
                        for evidence_id in item.evidence_ids
                    )
                ),
                "requires_independent_verifier": (
                    validation.requires_independent_verifier
                ),
            },
            "status": status,
            "stop_reason": persisted_stop,
            "budget": {
                "attempts": self.budget.attempts,
                "elapsed_seconds": self.budget.elapsed_seconds(),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "timestamps": {
                "started_at": started_at,
                "finished_at": _utc_now(),
            },
        }
        return self.state.write_decision(uuid.uuid4().hex, payload)

    def _finish_run(
        self,
        context: _SelectionContext,
        workdir: Path,
        status: str,
        stop_reason: Optional[str],
        selected_route: Optional[Route],
        audit: Sequence[_AttemptAudit],
        validation: ValidationResult,
        final_content: Optional[str],
        input_tokens: int,
        output_tokens: int,
        started_at: str,
    ) -> RunResult:
        persisted_reason = "approved" if status == "pass" else _terminal_reason(
            stop_reason
        )
        decision_path = self._write_decision(
            context,
            workdir,
            status,
            persisted_reason,
            selected_route,
            audit,
            validation,
            input_tokens,
            output_tokens,
            started_at,
        )
        return RunResult(
            status,
            None if status == "pass" else persisted_reason,
            context.decision,
            decision_path,
            self.budget.attempts,
            final_content if status == "pass" else None,
            selected_route,
            input_tokens,
            output_tokens,
        )

    def _record_observation(
        self,
        context: _SelectionContext,
        workdir: Path,
        route: Route,
        execution,
        validation: ValidationResult,
        escalations: int,
    ) -> None:
        report_metrics = (
            {}
            if execution.child_report is None
            else execution.child_report.metrics
        )
        metrics = {
            name: report_metrics[name]
            for name in context.profile.progress_metrics
            if name in report_metrics
        }
        model = next(
            item for item in context.catalog.models if item.slug == route.model
        )
        observed_cost = None
        if (
            model.input_price_per_million is not None
            and model.output_price_per_million is not None
        ):
            observed_cost = (
                execution.usage.input_tokens * model.input_price_per_million
                + execution.usage.output_tokens * model.output_price_per_million
            ) / _TOKENS_PER_MILLION
        if validation.status == "pass":
            observation_reason = "approved"
        elif validation.status == "blocked":
            observation_reason = _terminal_reason(validation.stop_reason)
        else:
            observation_reason = "validation-fail"
        self.state.append_observation(
            Observation(
                project_path=str(workdir),
                profile_id=context.profile.id,
                profile_version=context.profile.version,
                archetype=context.request.archetype,
                route=route,
                model_version=route.model,
                engine_version=__version__,
                duration_seconds=execution.elapsed_seconds,
                input_tokens=execution.usage.input_tokens,
                output_tokens=execution.usage.output_tokens,
                observed_cost_usd=observed_cost,
                validation_status=validation.status,
                metrics=metrics,
                failure_class=(
                    validation.failure_class.value
                    if validation.failure_class is not None
                    else None
                ),
                escalations=escalations,
                stop_reason=observation_reason,
            )
        )

    def select(self, request: TaskRequest, workdir: Path) -> SelectionResult:
        self.guard_recursion("")
        self._reset_budget()
        started_at = _utc_now()
        context = self._select_context(request, workdir)
        if context.decision is None:
            reason = context.blocked_reason or "external-dependency"
            validation = _blocked_validation(reason)
            return SelectionResult(
                decision=None,
                decision_path=self._write_decision(
                    context,
                    workdir,
                    "blocked",
                    reason,
                    None,
                    (),
                    validation,
                    0,
                    0,
                    started_at,
                ),
                status="blocked",
                stop_reason=reason,
            )
        validation = ValidationResult(
            "blocked",
            (),
            {},
            FailureClass.EXTERNAL_BLOCK,
            "user-stopped",
            False,
        )
        return SelectionResult(
            decision=context.decision,
            decision_path=self._write_decision(
                context,
                workdir,
                "stopped",
                "user-stopped",
                context.decision.ideal,
                (),
                validation,
                0,
                0,
                started_at,
            ),
            status="stopped",
            stop_reason="user-stopped",
        )

    def run(
        self,
        task_text: str,
        request: TaskRequest,
        workdir: Path,
        sandbox: SandboxMode,
        approval: ApprovalPolicy,
    ) -> RunResult:
        self.guard_recursion(task_text)
        self._reset_budget()
        started_at = _utc_now()
        context = self._select_context(request, workdir)
        if context.decision is None:
            reason = context.blocked_reason or "external-dependency"
            return self._finish_run(
                context,
                workdir,
                "blocked",
                reason,
                None,
                (),
                _blocked_validation(reason),
                None,
                0,
                0,
                started_at,
            )
        progress = ProgressTracker(
            directions=context.profile.progress_directions,
            noisy=context.profile.noisy_progress,
        )
        current = context.decision.ideal
        route_history = []
        audit = []
        feedback = None
        route_reason = "initial-route"
        input_tokens = 0
        output_tokens = 0
        while self.budget.can_start():
            self.budget.record_attempt()
            route_history.append(current)
            try:
                execution = self.executor.execute(
                    task_text=task_text,
                    request=request,
                    route=current,
                    cwd=workdir,
                    sandbox=sandbox,
                    approval=approval,
                    timeout_seconds=self.budget.remaining_seconds(),
                    mode="execute",
                    feedback=feedback,
                )
            except Exception:
                validation = _blocked_validation(
                    "executor-technical-failure"
                )
                audit.append(
                    _AttemptAudit(
                        current,
                        "blocked",
                        FailureClass.EXTERNAL_BLOCK,
                        route_reason,
                        (),
                        len(route_history) - 1,
                    )
                )
                return self._finish_run(
                    context,
                    workdir,
                    "blocked",
                    "executor-technical-failure",
                    current,
                    audit,
                    validation,
                    None,
                    input_tokens,
                    output_tokens,
                    started_at,
                )
            input_tokens += execution.usage.input_tokens
            output_tokens += execution.usage.output_tokens
            try:
                if (
                    execution.failure_kind is not None
                    or execution.process_exit_code not in (None, 0)
                    or execution.child_report is None
                ):
                    validation = classify_execution_failure(execution)
                else:
                    validation = self.validator(
                        context.profile,
                        request,
                        execution.child_report,
                    )
            except Exception:
                validation = _blocked_validation(
                    "validator-technical-failure"
                )
                evidence_ids = (
                    ()
                    if not execution.thread_id
                    else (_evidence_digest("primary-thread", execution.thread_id),)
                )
                audit.append(
                    _AttemptAudit(
                        current,
                        "blocked",
                        FailureClass.EXTERNAL_BLOCK,
                        route_reason,
                        evidence_ids,
                        len(route_history) - 1,
                    )
                )
                return self._finish_run(
                    context,
                    workdir,
                    "blocked",
                    "validator-technical-failure",
                    current,
                    audit,
                    validation,
                    None,
                    input_tokens,
                    output_tokens,
                    started_at,
                )
            verification = None
            if validation.requires_independent_verifier:
                partial_recovery = (
                    context.profile.id == "operations"
                    and execution.child_report is not None
                    and execution.child_report.metrics.get(
                        "partial_mutation", 0.0
                    )
                    > 0.0
                )
                if not self.budget.can_start():
                    reason = (
                        "partial-mutation-recovery-failed"
                        if partial_recovery
                        else "verifier-budget-exhausted"
                    )
                    validation = _blocked_validation(
                        reason,
                        FailureClass.RISK,
                    )
                else:
                    self.budget.record_attempt()
                    try:
                        verification = self.executor.execute(
                            task_text=task_text,
                            request=request,
                            route=context.decision.maximum_safety,
                            cwd=workdir,
                            sandbox=SandboxMode.READ_ONLY,
                            approval=approval,
                            timeout_seconds=self.budget.remaining_seconds(),
                            mode="verify",
                            feedback=validation.to_feedback(),
                        )
                    except Exception:
                        validation = _blocked_validation(
                            "executor-technical-failure"
                        )
                    else:
                        input_tokens += verification.usage.input_tokens
                        output_tokens += verification.usage.output_tokens
                        if (
                            verification.failure_kind is not None
                            or verification.process_exit_code not in (None, 0)
                            or verification.child_report is None
                        ):
                            validation = (
                                _blocked_validation(
                                    "partial-mutation-recovery-failed",
                                    FailureClass.RISK,
                                )
                                if partial_recovery
                                else classify_execution_failure(verification)
                            )
                        elif (
                            not _valid_execution_id(execution.thread_id)
                            or not _valid_execution_id(verification.thread_id)
                            or execution.thread_id == verification.thread_id
                        ):
                            validation = _blocked_validation(
                                "verifier-provenance-invalid",
                                FailureClass.RISK,
                            )
                        else:
                            try:
                                validation = self.validator(
                                    context.profile,
                                    request,
                                    execution.child_report,
                                    verification.child_report,
                                    primary_execution_id=execution.thread_id,
                                    independent_execution_id=(
                                        verification.thread_id
                                    ),
                                )
                            except Exception:
                                validation = _blocked_validation(
                                    "validator-technical-failure"
                                )
            evidence_ids = []
            if execution.thread_id:
                evidence_ids.append(
                    _evidence_digest("primary-thread", execution.thread_id)
                )
            if verification is not None and verification.thread_id:
                evidence_ids.append(
                    _evidence_digest("verifier-thread", verification.thread_id)
                )
            audit.append(
                _AttemptAudit(
                    current,
                    validation.status,
                    validation.failure_class,
                    route_reason,
                    tuple(evidence_ids),
                    len(route_history) - 1,
                )
            )
            try:
                self._record_observation(
                    context,
                    workdir,
                    current,
                    execution,
                    validation,
                    len(route_history) - 1,
                )
            except Exception:
                validation = _blocked_validation(
                    "validator-technical-failure"
                )
                return self._finish_run(
                    context,
                    workdir,
                    "blocked",
                    "validator-technical-failure",
                    current,
                    audit,
                    validation,
                    None,
                    input_tokens,
                    output_tokens,
                    started_at,
                )
            if validation.status == "pass":
                return self._finish_run(
                    context,
                    workdir,
                    "pass",
                    None,
                    current,
                    audit,
                    validation,
                    execution.child_report.deliverable,
                    input_tokens,
                    output_tokens,
                    started_at,
                )
            if validation.status == "blocked":
                return self._finish_run(
                    context,
                    workdir,
                    "blocked",
                    validation.stop_reason,
                    current,
                    audit,
                    validation,
                    (
                        execution.child_report.deliverable
                        if execution.child_report is not None
                        else None
                    ),
                    input_tokens,
                    output_tokens,
                    started_at,
                )
            report_metrics = (
                {}
                if execution.child_report is None
                else execution.child_report.metrics
            )
            progress.record(
                {
                    name: report_metrics[name]
                    for name in context.profile.progress_metrics
                    if name in report_metrics
                }
            )
            if progress.stagnated:
                return self._finish_run(
                    context,
                    workdir,
                    "blocked",
                    "no-progress",
                    current,
                    audit,
                    validation,
                    (
                        execution.child_report.deliverable
                        if execution.child_report is not None
                        else None
                    ),
                    input_tokens,
                    output_tokens,
                    started_at,
                )
            assessments = assess_routes(
                context.viable,
                request,
                context.profile,
                self.state.local_observations(str(workdir)),
                self.state.benchmark_registry(),
                context.catalog,
            )
            current_decision = select_routes(
                assessments,
                context.profile,
                eliminated=context.eliminated,
            )
            escalation = choose_next_route(
                validation.failure_class,
                current,
                context.viable,
                current_decision,
                tuple(route_history),
                request,
            )
            if escalation.route is None:
                return self._finish_run(
                    context,
                    workdir,
                    "blocked",
                    escalation.reason,
                    current,
                    audit,
                    validation,
                    (
                        execution.child_report.deliverable
                        if execution.child_report is not None
                        else None
                    ),
                    input_tokens,
                    output_tokens,
                    started_at,
                )
            current = escalation.route
            feedback = validation.to_feedback()
            route_reason = escalation.reason
        validation = _blocked_validation(
            self.budget.stop_reason() or "budget-exhausted"
        )
        return self._finish_run(
            context,
            workdir,
            "blocked",
            validation.stop_reason,
            current,
            audit,
            validation,
            None,
            input_tokens,
            output_tokens,
            started_at,
        )
