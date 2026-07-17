import json
import subprocess
from copy import deepcopy
from pathlib import Path
from threading import Lock
from types import SimpleNamespace


INVALID_BOOLEAN_VALUES = ("false", "true", 0, 1, None)

PROFILE_ARRAY_FIELDS = (
    "signals",
    "benchmark_tags",
    "required_capabilities",
    "validator_categories",
    "progress_metrics",
)


def valid_profile_payload(profile_id="legal"):
    return {
        "id": profile_id,
        "version": "1.0.0",
        "signals": ["contrato", "regulação"],
        "benchmark_tags": ["professional-work"],
        "required_capabilities": ["source-grounding", "risk-analysis"],
        "validator_categories": ["sources", "assumptions", "decision"],
        "progress_metrics": ["required_checks_passed"],
        "noisy_progress": False,
        "critical_requires_independent_verifier": True,
    }


def invalid_profile_payloads():
    valid = valid_profile_payload()
    cases = [
        ("profile.unexpected", with_value(valid, ("unexpected",), True)),
        (
            "profile.disable_global_gates",
            with_value(valid, ("disable_global_gates",), ["authorization"]),
        ),
        ("profile", []),
    ]
    cases.extend(
        ("profile.%s" % field_name, without_value(valid, (field_name,)))
        for field_name in valid
        if field_name != "noisy_progress"
    )
    cases.extend(
        (path, payload)
        for path, payload in (
            ("profile.id", with_value(valid, ("id",), "")),
            ("profile.id", with_value(valid, ("id",), " \t")),
            ("profile.id", with_value(valid, ("id",), " legal")),
            ("profile.id", with_value(valid, ("id",), "legal ")),
            ("profile.id", with_value(valid, ("id",), "legal\n")),
            ("profile.id", with_value(valid, ("id",), "legal/profile")),
            ("profile.id", with_value(valid, ("id",), 7)),
            ("profile.version", with_value(valid, ("version",), "")),
            ("profile.version", with_value(valid, ("version",), "1.0")),
            ("profile.version", with_value(valid, ("version",), "01.0.0")),
            ("profile.version", with_value(valid, ("version",), " 1.0.0")),
            ("profile.version", with_value(valid, ("version",), "1.0.0\n")),
            ("profile.version", with_value(valid, ("version",), 1)),
            ("profile.version", with_value(valid, ("version",), None)),
        )
    )
    for field_name in PROFILE_ARRAY_FIELDS:
        cases.extend(
            (
                "profile.%s" % field_name,
                with_value(valid, (field_name,), invalid_value),
            )
            for invalid_value in ([], field_name, {}, None)
        )
        cases.extend(
            (
                "profile.%s[0]" % field_name,
                with_value(valid, (field_name,), [invalid_value]),
            )
            for invalid_value in (
                "",
                " \t",
                " leading",
                "trailing ",
                "trailing\n",
                1,
                True,
                None,
            )
        )
        duplicate_value = valid[field_name][0]
        cases.append(
            (
                "profile.%s[1]" % field_name,
                with_value(
                    valid,
                    (field_name,),
                    [duplicate_value, duplicate_value],
                ),
            )
        )
    for field_name in (
        "noisy_progress",
        "critical_requires_independent_verifier",
    ):
        cases.extend(
            (
                "profile.%s" % field_name,
                with_value(valid, (field_name,), invalid_value),
            )
            for invalid_value in INVALID_BOOLEAN_VALUES
        )
    return cases


def valid_route_payload():
    return {"model": "gpt-5.6-sol", "effort": "medium"}


def valid_validation_check_payload():
    return {"id": "tests", "category": "tests", "required": True}


def valid_task_payload():
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
        "validation_checks": [valid_validation_check_payload()],
    }


def request_fixture(**overrides):
    from model_router.contracts import TaskRequest

    payload = valid_task_payload()
    payload.update(overrides)
    return TaskRequest.from_dict(payload)


def profile_fixture(**overrides):
    from model_router.profiles import Profile, QualityFloor

    payload = {
        "id": "software",
        "version": "1.0.0",
        "signals": ("software",),
        "benchmark_tags": ("professional-work",),
        "quality_floor": QualityFloor(("correctness",)),
        "validator_categories": ("tests",),
        "progress_metrics": ("required_checks_passed",),
        "noisy_progress": False,
        "critical_requires_independent_verifier": True,
    }
    payload.update(overrides)
    return Profile(**payload)


class RecordingRunner:
    def __init__(
        self,
        live_payload,
        bundled_payload=None,
        live_returncode=0,
        bundled_returncode=0,
        stderr="",
    ):
        self.live_payload = live_payload
        self.bundled_payload = (
            live_payload if bundled_payload is None else bundled_payload
        )
        self.live_returncode = live_returncode
        self.bundled_returncode = bundled_returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv):
        if type(argv) is not tuple:
            raise AssertionError("runner argv must be a tuple")
        self.calls.append(argv)
        bundled = argv[-1:] == ("--bundled",)
        payload = self.bundled_payload if bundled else self.live_payload
        if isinstance(payload, BaseException):
            raise payload
        return SimpleNamespace(
            returncode=(
                self.bundled_returncode if bundled else self.live_returncode
            ),
            stdout=payload if type(payload) is str else json.dumps(payload),
            stderr=self.stderr,
        )


def valid_evidence_payload():
    return {
        "category": "tests",
        "passed": True,
        "summary": "todos os testes passaram",
        "exit_code": 0,
    }


def valid_child_report_payload():
    return {
        "status": "pass",
        "deliverable": "implementação validada",
        "evidence": [valid_evidence_payload()],
        "metrics": {"passed_checks": 1.0},
        "failure_class": None,
        "next_hint": None,
    }


def valid_validation_result_payload():
    return {
        "status": "pass",
        "evidence": [valid_evidence_payload()],
        "metrics": {"passed_checks": 1.0},
        "failure_class": None,
        "stop_reason": None,
        "requires_independent_verifier": False,
    }


def valid_route_assessment_payload():
    return {
        "route": valid_route_payload(),
        "evidence_grade": 2,
        "quality_signal": 0.9,
        "quality_basis": "validated",
        "expected_cost": 1.5,
        "expected_latency": 2.5,
        "residual_risk": 0.1,
        "sample_size": 3,
        "comparable_groups": ["software"],
        "capabilities": ["coding"],
        "prior_roles": ["executor"],
        "provisional": False,
        "evidence_ids": ["evidence-1"],
    }


def valid_eliminated_route_payload():
    return {"route": valid_route_payload(), "reason": "quality floor"}


def valid_route_decision_payload():
    assessment = valid_route_assessment_payload()
    route = deepcopy(assessment["route"])
    return {
        "economic": deepcopy(route),
        "ideal": deepcopy(route),
        "maximum_safety": deepcopy(route),
        "frontier": [assessment],
        "eliminated": [valid_eliminated_route_payload()],
        "rationale": {"ideal": "best verified quality"},
    }


def observation_fixture(project_path="/workspace/project", **overrides):
    from model_router.contracts import Effort, Route
    from model_router.state import Observation

    payload = {
        "project_path": project_path,
        "profile_id": "software",
        "profile_version": "1.0.0",
        "archetype": "bounded-change",
        "route": Route("gpt-5.6-terra", Effort.MEDIUM),
        "model_version": "gpt-5.6-terra-2026-07-09",
        "engine_version": "0.1.0",
        "duration_seconds": 12.5,
        "input_tokens": 1200,
        "output_tokens": 300,
        "observed_cost_usd": 0.012,
        "validation_status": "pass",
        "metrics": {"required_checks_passed": 1.0},
        "failure_class": None,
        "escalations": 0,
        "stop_reason": "approved",
    }
    payload.update(overrides)
    return Observation(**payload)


def benchmark_registry_contract_mutations(payload):
    mutations = {}

    impossible_date = deepcopy(payload)
    impossible_date["verified_at"] = "2026-02-31"
    mutations["calendar-date"] = impossible_date

    credential_url = deepcopy(payload)
    credential_url["sources"][0]["url"] = "https://user:pass@example.test/path"
    mutations["credential-url"] = credential_url

    whitespace_url = deepcopy(payload)
    whitespace_url["sources"][0]["url"] = "https://example.test/a b"
    mutations["whitespace-url"] = whitespace_url

    missing_host_url = deepcopy(payload)
    missing_host_url["sources"][0]["url"] = "https:///missing-host"
    mutations["missing-host-url"] = missing_host_url

    semantic_duplicate = deepcopy(payload)
    duplicate = deepcopy(semantic_duplicate["observations"][0])
    duplicate["id"] = "semantic-duplicate"
    semantic_duplicate["observations"].append(duplicate)
    mutations["semantic-duplicate-observation"] = semantic_duplicate

    luna_ultra = deepcopy(payload)
    luna_ultra["observations"][0].update(
        {"model": "gpt-5.6-luna", "effort": "ultra", "topology": "multi"}
    )
    mutations["unsupported-model-effort"] = luna_ultra

    ultra_single = deepcopy(payload)
    ultra_single["observations"][0].update(
        {"model": "gpt-5.6-sol", "effort": "ultra", "topology": "single"}
    )
    mutations["ultra-single"] = ultra_single

    non_ultra_multi = deepcopy(payload)
    non_ultra_multi["observations"][0].update(
        {"effort": "high", "topology": "multi"}
    )
    mutations["non-ultra-multi"] = non_ultra_multi

    declared_unspecified = deepcopy(payload)
    declared_unspecified["observations"][0]["topology"] = "unspecified"
    mutations["declared-unspecified"] = declared_unspecified

    return mutations


def with_value(payload, path, value):
    changed = deepcopy(payload)
    target = changed
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return changed


def without_value(payload, path):
    changed = deepcopy(payload)
    target = changed
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]
    return changed


def invalid_task_payloads():
    valid = valid_task_payload()
    cases = [("task_request.unexpected", with_value(valid, ("unexpected",), True))]
    cases.extend(
        ("task_request.%s" % field_name, without_value(valid, (field_name,)))
        for field_name in valid
    )
    for field_name in (
        "external_effects",
        "external_effects_authorized",
        "parallel_writes",
        "worktree_isolated",
    ):
        cases.extend(
            (
                "task_request.%s" % field_name,
                with_value(valid, (field_name,), invalid_value),
            )
            for invalid_value in INVALID_BOOLEAN_VALUES
        )
    cases.extend(
        (
            path,
            payload,
        )
        for path, payload in (
            ("task_request.schema_version", with_value(valid, ("schema_version",), "2.0.0")),
            ("task_request.primary_profile", with_value(valid, ("primary_profile",), "")),
            ("task_request.primary_profile", with_value(valid, ("primary_profile",), " \t")),
            ("task_request.primary_profile", with_value(valid, ("primary_profile",), 7)),
            ("task_request.secondary_profiles", with_value(valid, ("secondary_profiles",), "software")),
            ("task_request.secondary_profiles[0]", with_value(valid, ("secondary_profiles",), [""])),
            ("task_request.secondary_profiles[0]", with_value(valid, ("secondary_profiles",), ["\t"])),
            ("task_request.archetype", with_value(valid, ("archetype",), "")),
            ("task_request.archetype", with_value(valid, ("archetype",), "\n ")),
            ("task_request.independent_fronts", with_value(valid, ("independent_fronts",), 0)),
            ("task_request.independent_fronts", with_value(valid, ("independent_fronts",), 1.5)),
            ("task_request.independent_fronts", with_value(valid, ("independent_fronts",), True)),
            ("task_request.required_tools", with_value(valid, ("required_tools",), "shell")),
            ("task_request.required_tools[0]", with_value(valid, ("required_tools",), [""])),
            ("task_request.required_tools[0]", with_value(valid, ("required_tools",), [" "])),
            ("task_request.acceptance_criteria[0]", with_value(valid, ("acceptance_criteria",), [""])),
            ("task_request.acceptance_criteria[0]", with_value(valid, ("acceptance_criteria",), ["\t"])),
            ("task_request.validation_checks", with_value(valid, ("validation_checks",), [])),
            ("task_request.validation_checks", with_value(valid, ("validation_checks",), {})),
            (
                "task_request.validation_checks[0].unexpected",
                with_value(valid, ("validation_checks", 0, "unexpected"), True),
            ),
            (
                "task_request.validation_checks[0].id",
                without_value(valid, ("validation_checks", 0, "id")),
            ),
            (
                "task_request.validation_checks[0].id",
                with_value(valid, ("validation_checks", 0, "id"), ""),
            ),
            (
                "task_request.validation_checks[0].id",
                with_value(valid, ("validation_checks", 0, "id"), "   "),
            ),
            (
                "task_request.validation_checks[0].category",
                with_value(valid, ("validation_checks", 0, "category"), "\t"),
            ),
            (
                "task_request.validation_checks[0].required",
                with_value(valid, ("validation_checks", 0, "required"), "false"),
            ),
        )
    )
    return cases


def invalid_child_report_payloads():
    valid = valid_child_report_payload()
    cases = [("child_report.unexpected", with_value(valid, ("unexpected",), True))]
    cases.extend(
        ("child_report.%s" % field_name, without_value(valid, (field_name,)))
        for field_name in valid
    )
    cases.extend(
        (
            path,
            payload,
        )
        for path, payload in (
            ("child_report.status", with_value(valid, ("status",), "unknown")),
            ("child_report.status", with_value(valid, ("status",), 1)),
            ("child_report.deliverable", with_value(valid, ("deliverable",), "")),
            ("child_report.deliverable", with_value(valid, ("deliverable",), "   ")),
            ("child_report.deliverable", with_value(valid, ("deliverable",), 1)),
            ("child_report.evidence", with_value(valid, ("evidence",), {})),
            (
                "child_report.evidence[0].unexpected",
                with_value(valid, ("evidence", 0, "unexpected"), True),
            ),
            (
                "child_report.evidence[0].category",
                without_value(valid, ("evidence", 0, "category")),
            ),
            (
                "child_report.evidence[0].category",
                with_value(valid, ("evidence", 0, "category"), ""),
            ),
            (
                "child_report.evidence[0].category",
                with_value(valid, ("evidence", 0, "category"), "\t"),
            ),
            (
                "child_report.evidence[0].passed",
                with_value(valid, ("evidence", 0, "passed"), "false"),
            ),
            (
                "child_report.evidence[0].summary",
                with_value(valid, ("evidence", 0, "summary"), ""),
            ),
            (
                "child_report.evidence[0].summary",
                with_value(valid, ("evidence", 0, "summary"), "\n "),
            ),
            (
                "child_report.evidence[0].exit_code",
                with_value(valid, ("evidence", 0, "exit_code"), True),
            ),
            (
                "child_report.evidence[0].exit_code",
                with_value(valid, ("evidence", 0, "exit_code"), 0.5),
            ),
            ("child_report.metrics", with_value(valid, ("metrics",), [])),
            ("child_report.metrics", with_value(valid, ("metrics",), {"": 1.0})),
            ("child_report.metrics", with_value(valid, ("metrics",), {" \t": 1.0})),
            ("child_report.metrics.score", with_value(valid, ("metrics",), {"score": "1"})),
            ("child_report.failure_class", with_value(valid, ("failure_class",), "unknown")),
            ("child_report.failure_class", with_value(valid, ("failure_class",), 1)),
            ("child_report.next_hint", with_value(valid, ("next_hint",), "")),
            ("child_report.next_hint", with_value(valid, ("next_hint",), " \n")),
            ("child_report.next_hint", with_value(valid, ("next_hint",), 1)),
        )
    )
    return cases


def route_fixture():
    from model_router.contracts import Effort, Route

    efforts_by_model = (
        ("gpt-5.6-luna", tuple(item for item in Effort if item is not Effort.ULTRA)),
        ("gpt-5.6-terra", tuple(Effort)),
        ("gpt-5.6-sol", tuple(Effort)),
    )
    return tuple(
        Route(model, effort)
        for model, efforts in efforts_by_model
        for effort in efforts
    )


def ultra_viable_fixture():
    return route_fixture()


def decision_fixture(maximum_safety=None, frontier=None):
    from model_router.contracts import RouteDecision

    selected_frontier = frontier or (
        assessment(
            "gpt-5.6-luna", "low", quality=0.72, cost=0.2,
            latency=0.2, risk=0.28,
        ),
        assessment(
            "gpt-5.6-terra", "medium", quality=0.85, cost=0.5,
            latency=0.5, risk=0.15,
        ),
        assessment(
            "gpt-5.6-sol", "max", quality=0.94, cost=1.0,
            latency=1.0, risk=0.06,
        ),
    )
    selected_maximum = maximum_safety or selected_frontier[-1].route
    return RouteDecision(
        economic=selected_frontier[0].route,
        ideal=selected_frontier[1].route,
        maximum_safety=selected_maximum,
        frontier=tuple(selected_frontier),
        eliminated=(),
        rationale={"ideal": "fixture decision"},
    )


def child_report_with_categories(
    categories,
    status="pass",
    failure_class=None,
    metrics=None,
    passed=True,
):
    from model_router.contracts import ChildReport, EvidenceItem, FailureClass

    parsed_failure = failure_class
    if type(failure_class) is str:
        parsed_failure = FailureClass(failure_class)
    category_items = tuple(categories)
    selected_metrics = (
        {"required_checks_passed": float(len(category_items))}
        if metrics is None
        else metrics
    )
    return ChildReport(
        status=status,
        deliverable="technical validator fixture",
        evidence=tuple(
            EvidenceItem(category, passed, "technical %s check" % category, 0)
            for category in category_items
        ),
        metrics=selected_metrics,
        failure_class=parsed_failure,
        next_hint=None,
    )


def request_for(profile_id):
    from pathlib import Path

    from model_router.profiles import load_profiles

    profiles_root = Path(__file__).resolve().parents[2] / "references" / "profiles"
    profile = load_profiles(profiles_root)[profile_id]
    checks = [
        {"id": "check-%d" % index, "category": category, "required": True}
        for index, category in enumerate(profile.validator_categories)
    ]
    return request_fixture(primary_profile=profile_id, validation_checks=checks)


def verifier_pass_report(status="pass", passed=True):
    from model_router.contracts import FailureClass

    return child_report_with_categories(
        ("independent-verifier",),
        status=status,
        passed=passed,
        failure_class=(FailureClass.RISK if status == "fail" else None),
        metrics={},
    )


def partial_operation_report():
    from model_router.contracts import FailureClass

    return child_report_with_categories(
        ("before-state",),
        status="fail",
        failure_class=FailureClass.RISK,
        metrics={"partial_mutation": 1.0},
    )


class FakeClock:
    def __init__(self, initial=0.0):
        self._value = initial
        self._lock = Lock()

    def __call__(self):
        with self._lock:
            return self._value

    def advance(self, seconds):
        with self._lock:
            self._value += seconds

    def set(self, value):
        with self._lock:
            self._value = value


class FakeProcess:
    def __init__(
        self,
        stdout="",
        stderr="",
        returncode=0,
        hanging=False,
        pipes_never_close=False,
        pid=None,
        clock=None,
        elapsed_seconds=0.0,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.hanging = hanging
        self.pipes_never_close = pipes_never_close
        self.pid = pid
        self.clock = clock
        self.elapsed_seconds = elapsed_seconds
        self.communicated_input = None
        self.communicate_calls = []
        self.terminated = False
        self.killed = False
        self.lifecycle = []
        self.wait_calls = []
        self._clock_advanced = False

    def _advance_clock(self, seconds):
        if self.clock is not None and seconds:
            self.clock.advance(seconds)

    def communicate(self, input=None, timeout=None):
        self.communicate_calls.append({"input": input, "timeout": timeout})
        if input is not None:
            self.communicated_input = input
        if self.hanging and (not self.killed or self.pipes_never_close):
            self._advance_clock(float(timeout or 0.0))
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)
        if not self._clock_advanced:
            self._advance_clock(self.elapsed_seconds)
            self._clock_advanced = True
        return self.stdout, self.stderr

    def terminate(self):
        self.terminated = True
        self.lifecycle.append("terminate")

    def kill(self):
        self.killed = True
        self.lifecycle.append("kill")

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if not self.killed:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)
        self.returncode = -9
        return self.returncode


class FakeProcessFactory:
    def __init__(self, process):
        self.process = process
        self.call_count = 0
        self.last_argv = None
        self.last_env = None
        self.last_kwargs = None

    @classmethod
    def success(
        cls,
        stdout=None,
        stderr="",
        returncode=0,
        clock=None,
        elapsed_seconds=0.0,
    ):
        if stdout is None:
            fixture = Path(__file__).resolve().parent / "fixtures" / "codex-events-success.jsonl"
            stdout = fixture.read_text()
        return cls(
            FakeProcess(
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                clock=clock,
                elapsed_seconds=elapsed_seconds,
            )
        )

    @classmethod
    def hanging(
        cls,
        stderr="",
        clock=None,
        pipes_never_close=False,
        pid=None,
    ):
        return cls(
            FakeProcess(
                stderr=stderr,
                returncode=None,
                hanging=True,
                pipes_never_close=pipes_never_close,
                pid=pid,
                clock=clock,
            )
        )

    def __call__(self, argv, **kwargs):
        self.call_count += 1
        self.last_argv = argv
        self.last_env = kwargs.get("env")
        self.last_kwargs = dict(kwargs)
        return self.process


def execution_args(**overrides):
    from model_router.contracts import ApprovalPolicy, Effort, Route, SandboxMode

    payload = {
        "task_text": "Implement the bounded technical change and run its checks.",
        "request": request_fixture(),
        "route": Route("gpt-5.6-luna", Effort.MEDIUM),
        "cwd": Path(__file__).resolve().parents[1],
        "sandbox": SandboxMode.READ_ONLY,
        "approval": ApprovalPolicy.NEVER,
        "timeout_seconds": 30.0,
        "mode": "execute",
        "feedback": None,
    }
    payload.update(overrides)
    return payload


def catalog_fixture(prices=True, partial_prices=False, models=None):
    from model_router.contracts import Effort
    from model_router.model_registry import CatalogSnapshot, ModelSpec

    if models is None:
        price_rows = ((1.0, 6.0), (2.5, 15.0), (5.0, 30.0))
        if not prices:
            price_rows = ((None, None),) * 3
        elif partial_prices:
            price_rows = ((1.0, None), (None, 15.0), (5.0, 30.0))
        models = (
            ModelSpec(
                "gpt-5.6-luna", "GPT-5.6-Luna", Effort.MEDIUM,
                (Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH, Effort.MAX),
                True, "list", 3, "v1", price_rows[0][0], price_rows[0][1],
            ),
            ModelSpec(
                "gpt-5.6-terra", "GPT-5.6-Terra", Effort.MEDIUM,
                tuple(Effort), True, "list", 2, "v2",
                price_rows[1][0], price_rows[1][1],
            ),
            ModelSpec(
                "gpt-5.6-sol", "GPT-5.6-Sol", Effort.LOW,
                tuple(Effort), True, "list", 1, "v2",
                price_rows[2][0], price_rows[2][1],
            ),
        )
    return CatalogSnapshot(
        source="seed",
        observed_at="2026-07-16T00:00:00Z",
        models=tuple(models),
    )


def assessment(
    model,
    effort,
    quality=None,
    cost=None,
    latency=None,
    risk=None,
    evidence_grade=4,
    quality_basis="validated",
    capabilities=("correctness",),
    prior_roles=(),
    provisional=False,
):
    from model_router.contracts import Effort, Route, RouteAssessment

    return RouteAssessment(
        route=Route(model, Effort(effort)),
        evidence_grade=evidence_grade,
        quality_signal=quality,
        quality_basis=quality_basis,
        expected_cost=cost,
        expected_latency=latency,
        residual_risk=risk,
        sample_size=1,
        comparable_groups=("fixture-cohort",),
        capabilities=tuple(capabilities),
        prior_roles=tuple(prior_roles),
        provisional=provisional,
        evidence_ids=("fixture-evidence",),
    )


def stored_observation_fixture(route=None, **overrides):
    from model_router.contracts import Effort, Route
    from model_router.state import StoredObservation

    payload = {
        "project_hash": "a" * 64,
        "profile_id": "software",
        "profile_version": "1.0.0",
        "archetype": "bounded-change",
        "route": route or Route("gpt-5.6-luna", Effort.MEDIUM),
        "model_version": "gpt-5.6-luna-2026-07-16",
        "engine_version": "0.1.0",
        "duration_seconds": 10.0,
        "input_tokens": 100,
        "output_tokens": 50,
        "observed_cost_usd": 0.25,
        "validation_status": "pass",
        "metrics": {"required_checks_passed": 1.0},
        "failure_class": None,
        "escalations": 0,
        "stop_reason": "approved",
    }
    payload.update(overrides)
    return StoredObservation(**payload)


def benchmark_source_fixture(
    source_id="primary-source",
    status="active",
    kind="primary-independent",
):
    from model_router.registry import BenchmarkSource

    return BenchmarkSource(
        id=source_id,
        owner="Independent evaluator",
        url="https://example.test/%s" % source_id,
        status=status,
        kind=kind,
        limitations=("fixture-only",),
    )


def benchmark_observation_fixture(
    observation_id="observation-one",
    source_id="primary-source",
    route=None,
    benchmark="FixtureBench v1",
    dataset="fixture-dataset-v1",
    harness="fixture-harness-v1",
    topology=None,
    topology_declared=True,
    metric_name="task-completed-correctly",
    metric_value=50.0,
    cost=1.0,
    evaluated_at="2026-07-16",
    profile_tags=("professional-work", "bounded-change"),
):
    from model_router.contracts import Effort, Route
    from model_router.registry import BenchmarkObservation, ScalarMeasurement

    selected_route = route or Route("gpt-5.6-luna", Effort.MEDIUM)
    metric_units = {
        "task-completed-correctly": "percent",
        "arc-agi-1-public-eval": "percent",
        "arc-agi-2-public-eval": "percent",
        "arc-agi-3-public-demo": "percent",
        "artificial-analysis-intelligence-index": "index-points",
    }
    selected_topology = selected_route.topology.value if topology is None else topology
    return BenchmarkObservation(
        id=observation_id,
        source_id=source_id,
        benchmark=benchmark,
        dataset=dataset,
        harness=harness,
        route=selected_route,
        topology=selected_topology,
        topology_declared=topology_declared,
        profile_tags=tuple(profile_tags),
        metric=ScalarMeasurement(
            metric_name, metric_value, metric_units[metric_name]
        ),
        cost=(
            None
            if cost is None
            else ScalarMeasurement("cost-per-task", cost, "USD/task")
        ),
        evaluated_at=evaluated_at,
    )


def benchmark_registry_fixture(sources=None, observations=()):
    from model_router.registry import BenchmarkRegistry

    selected_sources = (
        (benchmark_source_fixture(),) if sources is None else tuple(sources)
    )
    return BenchmarkRegistry(
        schema_version="1.0.0",
        verified_at="2026-07-16",
        sources=selected_sources,
        observations=tuple(observations),
    )


def empty_registry_fixture():
    return benchmark_registry_fixture()


def local_fixture(route="gpt-5.6-luna:medium:single", passes=1, failures=0):
    from model_router.contracts import Effort, Route

    model, effort, _topology = route.split(":")
    selected = Route(model, Effort(effort))
    observations = [
        stored_observation_fixture(
            route=selected,
            model_version="%s-2026-07-16" % model,
            validation_status="pass",
            duration_seconds=10.0 + index,
            observed_cost_usd=0.2 + (index * 0.01),
        )
        for index in range(passes)
    ]
    observations.extend(
        stored_observation_fixture(
            route=selected,
            model_version="%s-2026-07-16" % model,
            validation_status="fail",
            duration_seconds=20.0 + index,
            observed_cost_usd=0.4 + (index * 0.01),
            failure_class="capacity",
            stop_reason="validation-fail",
        )
        for index in range(failures)
    )
    return tuple(observations)


def external_fixture(favoring="gpt-5.6-sol:max:single"):
    from model_router.contracts import Effort, Route

    favored_model, favored_effort, _topology = favoring.split(":")
    favored = Route(favored_model, Effort(favored_effort))
    baseline = Route("gpt-5.6-luna", Effort.MEDIUM)
    observations = [
        benchmark_observation_fixture(
            observation_id="favored-route",
            route=favored,
            metric_value=90.0,
        )
    ]
    if baseline != favored:
        observations.append(
            benchmark_observation_fixture(
                observation_id="baseline-route",
                route=baseline,
                metric_value=40.0,
            )
        )
    return benchmark_registry_fixture(observations=tuple(observations))


def two_incompatible_benchmarks_fixture():
    from model_router.contracts import Effort, Route

    route = Route("gpt-5.6-luna", Effort.MEDIUM)
    return benchmark_registry_fixture(
        observations=(
            benchmark_observation_fixture(
                observation_id="benchmark-one",
                route=route,
                benchmark="FixtureBench v1",
                metric_value=60.0,
            ),
            benchmark_observation_fixture(
                observation_id="benchmark-two",
                route=route,
                benchmark="OtherBench v2",
                dataset="other-dataset-v2",
                harness="other-harness-v2",
                metric_name="artificial-analysis-intelligence-index",
                metric_value=45.0,
            ),
        )
    )


class RecordingExecutor:
    def __init__(self, child_reports=(), execution_results=(), thread_ids=()):
        self.child_reports = list(child_reports)
        self.execution_results = list(execution_results)
        self.thread_ids = list(thread_ids)
        self.calls = []

    @property
    def call_count(self):
        return len(self.calls)

    def execute(self, **kwargs):
        from model_router.executor import ExecutionResult, TokenUsage

        self.calls.append(SimpleNamespace(**kwargs))
        if self.execution_results:
            return self.execution_results.pop(0)
        if not self.child_reports:
            raise AssertionError("no deterministic child report remains")
        report = self.child_reports.pop(0)
        thread_id = (
            self.thread_ids.pop(0)
            if self.thread_ids
            else "fixture-thread-%d" % len(self.calls)
        )
        return ExecutionResult(
            thread_id=thread_id,
            child_report=report,
            usage=TokenUsage(),
            process_exit_code=0,
            stderr_summary="",
            elapsed_seconds=1.0,
            failure_kind=None,
        )


def _temporary_directory():
    import atexit
    import shutil
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="model-router-test-"))
    atexit.register(shutil.rmtree, str(path), True)
    return path


def _profile_report(
    profile_id="software",
    status="pass",
    failure_class=None,
    deliverable="validated fixture",
    passed=True,
    metrics=None,
):
    from model_router.contracts import ChildReport, EvidenceItem
    from model_router.profiles import load_profiles

    profiles_root = Path(__file__).resolve().parents[2] / "references" / "profiles"
    profile = load_profiles(profiles_root)[profile_id]
    selected_metrics = (
        {name: 0.0 for name in profile.progress_metrics}
        if metrics is None
        else dict(metrics)
    )
    if "required_checks_passed" not in selected_metrics:
        selected_metrics["required_checks_passed"] = (
            float(len(profile.validator_categories)) if passed else 0.0
        )
    return ChildReport(
        status=status,
        deliverable=deliverable,
        evidence=tuple(
            EvidenceItem(category, passed, "technical %s check" % category, 0)
            for category in profile.validator_categories
        ),
        metrics=selected_metrics,
        failure_class=failure_class,
        next_hint=None,
    )


def passing_report(deliverable="validated fixture", profile_id="software"):
    return _profile_report(profile_id=profile_id, deliverable=deliverable)


def depth_failure_report(profile_id="software"):
    from model_router.contracts import FailureClass
    from model_router.profiles import load_profiles

    profiles_root = Path(__file__).resolve().parents[2] / "references" / "profiles"
    profile = load_profiles(profiles_root)[profile_id]
    profile_metrics = {name: 0.0 for name in profile.progress_metrics}
    profile_metrics["required_checks_passed"] = 0.0
    if len(profile.progress_metrics) > 1:
        profile_metrics[profile.progress_metrics[1]] = 1.0
    return _profile_report(
        profile_id=profile_id,
        status="fail",
        failure_class=FailureClass.DEPTH,
        deliverable="validation failed",
        passed=False,
        metrics=profile_metrics,
    )


def execution_result_fixture(**overrides):
    from model_router.executor import ExecutionResult, TokenUsage

    payload = {
        "thread_id": "fixture-thread",
        "child_report": None,
        "usage": TokenUsage(),
        "process_exit_code": 0,
        "stderr_summary": "",
        "elapsed_seconds": 1.0,
        "failure_kind": None,
    }
    payload.update(overrides)
    return ExecutionResult(**payload)


def run_args(**overrides):
    from model_router.contracts import ApprovalPolicy, SandboxMode

    payload = {
        "task_text": "small reversible task",
        "request": request_fixture(),
        "workdir": _temporary_directory(),
        "sandbox": SandboxMode.READ_ONLY,
        "approval": ApprovalPolicy.NEVER,
    }
    payload.update(overrides)
    return payload


def run_args_without_task(**overrides):
    payload = run_args(**overrides)
    del payload["task_text"]
    return payload


def catalog_with_known_tool(tool_name="private-tool"):
    from dataclasses import replace

    catalog = catalog_fixture()
    models = tuple(
        replace(
            model,
            supported_tools=((tool_name,) if model.slug == "gpt-5.6-luna" else ()),
            supported_tools_known=True,
        )
        for model in catalog.models
    )
    return catalog.__class__(catalog.source, catalog.observed_at, models)


def service_fixture(
    child_reports=(),
    execution_results=(),
    thread_ids=(),
    catalog=None,
    budget=None,
):
    from model_router.escalation import BudgetLedger
    from model_router.profiles import load_profiles
    from model_router.service import RouterService
    from model_router.state import RuntimeState
    from model_router.validators import validate_child_report

    skill_root = Path(__file__).resolve().parents[2]
    references = skill_root / "references"
    temporary = _temporary_directory()
    service = RouterService(
        state=RuntimeState(temporary / "runtime", seed_root=references),
        profiles=load_profiles(references / "profiles"),
        catalog_provider=(lambda: catalog or catalog_fixture()),
        executor=RecordingExecutor(
            child_reports=child_reports,
            execution_results=execution_results,
            thread_ids=thread_ids,
        ),
        budget=budget or BudgetLedger(),
        validator=validate_child_report,
    )
    return service


def complete_decision_payload():
    route = "gpt-5.6-terra:medium:single"
    return {
        "schema_version": "1.0.0",
        "fingerprint": {
            "project_hash": "a" * 64,
            "primary_profile": "software",
            "secondary_profiles": [],
            "archetype": "bounded-change",
            "novelty": "low",
            "ambiguity": "low",
            "reasoning_depth": "low",
            "context_load": "small",
            "tool_dependency": "none",
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
            "required_tools": [],
            "validation_checks": [],
        },
        "profile": {"id": "software", "version": "1.0.0"},
        "catalog": {
            "source": "seed",
            "schema_version": "1.0.0",
            "model_registry_version": "1.0.0",
            "benchmark_registry_version": "1.0.0",
            "verified_at": "2026-07-16",
            "model_ids": ["gpt-5.6-terra"],
            "benchmark_ids": [],
            "evidence_ids": [],
        },
        "eliminated": [],
        "selection": {
            "economic": route,
            "ideal": route,
            "maximum_safety": route,
            "frontier": [route],
            "rationale_codes": {
                "economic": "official-price-prior",
                "ideal": "structural-cold-start-prior",
                "maximum_safety": "official-capability-prior",
            },
        },
        "decision": {"selected": route},
        "evidence_ids": [],
        "history": [],
        "validation": {
            "status": "blocked",
            "failure_class": "external_block",
            "stop_reason": "user-stopped",
            "required_checks_passed": 0,
            "metrics": {},
            "evidence_ids": [],
            "requires_independent_verifier": False,
        },
        "status": "stopped",
        "stop_reason": "user-stopped",
        "budget": {
            "attempts": 0,
            "elapsed_seconds": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
        },
        "timestamps": {
            "started_at": "2026-07-17T00:00:00Z",
            "finished_at": "2026-07-17T00:00:00Z",
        },
    }
