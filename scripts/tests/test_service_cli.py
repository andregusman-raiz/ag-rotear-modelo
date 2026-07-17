import importlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from platform_support import DIRECTORY_SYMLINK_AVAILABLE, POSIX


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


class ServiceCliTests(unittest.TestCase):
    def test_service_and_entrypoints_exist(self):
        service = importlib.import_module("model_router.service")

        self.assertTrue(hasattr(service, "RouterService"))
        for entrypoint in (
            "router.py",
            "run-route.sh",
            "run-route.py",
            "guarded-run.py",
            "codex-child-supervisor.py",
            "publish-ready.py",
            "validate-registry.py",
        ):
            self.assertTrue((SCRIPTS / entrypoint).is_file(), entrypoint)

    def test_dry_run_records_three_choices_without_execution(self):
        from support import request_fixture, service_fixture

        service = service_fixture()
        result = service.select(request_fixture(), workdir=Path("/project"))

        self.assertEqual(0, service.executor.call_count)
        self.assertIsNotNone(result.decision.economic)
        self.assertIsNotNone(result.decision.ideal)
        self.assertIsNotNone(result.decision.maximum_safety)
        self.assertTrue(result.decision_path.is_file())

    def test_run_executes_ideal_and_stops_on_pass(self):
        from support import passing_report, run_args, service_fixture

        service = service_fixture(child_reports=(passing_report(),))
        result = service.run(**run_args())

        self.assertEqual("pass", result.status)
        self.assertEqual(result.initial_decision.ideal, service.executor.calls[0].route)
        self.assertEqual(1, result.attempt_count)

    def test_quality_failure_escalates_with_feedback_and_no_identical_route(self):
        from support import depth_failure_report, passing_report, run_args, service_fixture

        service = service_fixture(
            child_reports=(depth_failure_report(), passing_report()),
        )
        result = service.run(**run_args())

        self.assertEqual(2, result.attempt_count)
        self.assertNotEqual(service.executor.calls[0].route, service.executor.calls[1].route)
        self.assertEqual("fail", service.executor.calls[1].feedback["status"])

    def test_critical_weak_validation_runs_independent_read_only_verifier(self):
        from model_router.contracts import SandboxMode
        from support import (
            passing_report,
            request_fixture,
            run_args,
            service_fixture,
            verifier_pass_report,
        )

        service = service_fixture(
            child_reports=(passing_report(), verifier_pass_report()),
        )
        result = service.run(
            **run_args(request=request_fixture(impact="critical", verifiability="weak"))
        )

        self.assertEqual("pass", result.status)
        self.assertEqual("verify", service.executor.calls[1].mode)
        self.assertEqual(SandboxMode.READ_ONLY, service.executor.calls[1].sandbox)

    def test_verifier_reusing_primary_thread_id_fails_closed(self):
        from support import (
            passing_report,
            request_fixture,
            run_args,
            service_fixture,
            verifier_pass_report,
        )

        service = service_fixture(
            child_reports=(passing_report(), verifier_pass_report()),
            thread_ids=("same-thread", "same-thread"),
        )
        result = service.run(
            **run_args(request=request_fixture(impact="critical", verifiability="weak"))
        )

        self.assertEqual("blocked", result.status)
        self.assertEqual("verifier-provenance-invalid", result.stop_reason)

    def test_parent_guard_refuses_to_route_inside_child(self):
        from model_router.service import RecursionBlocked, RouterService

        with mock.patch.dict(os.environ, {"AG_MODEL_ROUTER_CHILD": "1"}):
            with self.assertRaisesRegex(RecursionBlocked, "child execution"):
                RouterService.guard_recursion("")

    def test_validator_technical_failure_never_becomes_pass(self):
        from support import passing_report, run_args, service_fixture

        service = service_fixture(child_reports=(passing_report(),))
        service.validator = mock.Mock(side_effect=RuntimeError("validator unavailable"))

        result = service.run(**run_args())

        self.assertEqual("blocked", result.status)
        self.assertEqual("validator-technical-failure", result.stop_reason)

    def test_executor_technical_failure_never_becomes_pass(self):
        from model_router.executor import ExecutionProtocolError
        from support import run_args, service_fixture

        service = service_fixture()
        service.executor.execute = mock.Mock(
            side_effect=ExecutionProtocolError("invalid child protocol")
        )

        result = service.run(**run_args())

        self.assertEqual("blocked", result.status)
        self.assertEqual("executor-technical-failure", result.stop_reason)

    def test_failure_kind_precedes_an_attached_pass_report(self):
        from support import (
            execution_result_fixture,
            passing_report,
            run_args,
            service_fixture,
        )

        service = service_fixture(
            execution_results=(
                execution_result_fixture(
                    child_report=passing_report(),
                    process_exit_code=7,
                    failure_kind="process",
                ),
            )
        )

        result = service.run(**run_args())

        self.assertNotEqual("pass", result.status)

    def test_partial_operation_switches_to_terminal_read_only_recovery(self):
        from model_router.contracts import SandboxMode
        from support import (
            partial_operation_report,
            request_fixture,
            run_args,
            service_fixture,
            verifier_pass_report,
        )

        service = service_fixture(
            child_reports=(partial_operation_report(), verifier_pass_report()),
        )
        result = service.run(
            **run_args(request=request_fixture(primary_profile="operations"))
        )

        self.assertEqual("blocked", result.status)
        self.assertEqual("partial-mutation-recovery-verified", result.stop_reason)
        self.assertEqual("verify", service.executor.calls[1].mode)
        self.assertEqual(SandboxMode.READ_ONLY, service.executor.calls[1].sandbox)
        self.assertFalse(
            any(call.mode == "execute" for call in service.executor.calls[1:])
        )

    def test_failed_partial_recovery_is_terminal_and_never_resumes_execute(self):
        from support import (
            depth_failure_report,
            partial_operation_report,
            request_fixture,
            run_args,
            service_fixture,
        )

        service = service_fixture(
            child_reports=(
                partial_operation_report(),
                depth_failure_report(profile_id="operations"),
            )
        )
        result = service.run(
            **run_args(request=request_fixture(primary_profile="operations"))
        )

        self.assertEqual("blocked", result.status)
        self.assertEqual("partial-mutation-recovery-failed", result.stop_reason)
        self.assertFalse(
            any(call.mode == "execute" for call in service.executor.calls[1:])
        )

    def test_decision_record_is_complete_and_omits_private_execution_content(self):
        from support import (
            catalog_with_known_tool,
            passing_report,
            request_fixture,
            run_args_without_task,
            service_fixture,
        )

        service = service_fixture(
            child_reports=(passing_report(deliverable="PRIVATE RESULT"),),
            thread_ids=("PRIVATE-THREAD-ID",),
            catalog=catalog_with_known_tool("private-tool"),
        )
        result = service.run(
            task_text="PRIVATE SENTENCE",
            **run_args_without_task(
                request=request_fixture(
                    required_tools=["private-tool"],
                    acceptance_criteria=["PRIVATE ACCEPTANCE"],
                )
            ),
        )
        payload = json.loads(result.decision_path.read_text(encoding="utf-8"))
        raw = json.dumps(payload, sort_keys=True)

        self.assertEqual(
            {
                "schema_version",
                "fingerprint",
                "profile",
                "catalog",
                "eliminated",
                "selection",
                "decision",
                "evidence_ids",
                "history",
                "validation",
                "status",
                "stop_reason",
                "budget",
                "timestamps",
                "integrity",
            },
            set(payload),
        )
        for private in (
            "PRIVATE SENTENCE",
            "PRIVATE RESULT",
            "PRIVATE ACCEPTANCE",
            "PRIVATE-THREAD-ID",
            "private-tool",
        ):
            self.assertNotIn(private, raw)
        self.assertRegex(
            payload["history"][0]["evidence_ids"][0],
            r"^sha256:evidence:[0-9a-f]{64}$",
        )

    def test_cli_help_lists_exactly_the_four_public_subcommands(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "run-route.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        for command in ("select", "run", "audit", "promote-registry"):
            self.assertIn(command, completed.stdout)

    def test_cli_run_requires_explicit_permissions_and_nonempty_stdin(self):
        from support import valid_task_payload

        missing = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "router.py"),
                "run",
                "--request",
                "request.json",
                "--workdir",
                ".",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(2, missing.returncode)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_path = root / "request.json"
            request_path.write_text(
                json.dumps(valid_task_payload()),
                encoding="utf-8",
            )
            empty = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "router.py"),
                    "run",
                    "--request",
                    str(request_path),
                    "--workdir",
                    str(root),
                    "--sandbox",
                    "read-only",
                    "--approval-policy",
                    "never",
                    "--runtime-root",
                    str(root / "runtime"),
                ],
                input="",
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(0, empty.returncode)
        self.assertIn("stdin", empty.stderr.lower())

    def test_cli_select_uses_cache_without_executing_a_child(self):
        from support import valid_task_payload

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_path = root / "request.json"
            payload = valid_task_payload()
            payload["acceptance_criteria"] = ["PRIVATE ACCEPTANCE"]
            request_path.write_text(json.dumps(payload), encoding="utf-8")
            env = os.environ.copy()
            env["AG_MODEL_ROUTER_CODEX_BIN"] = str(root / "missing-codex")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "router.py"),
                    "select",
                    "--request",
                    str(request_path),
                    "--workdir",
                    str(root),
                    "--runtime-root",
                    str(root / "runtime"),
                    "--models-cache",
                    str(SCRIPTS / "tests" / "fixtures" / "models-cache.json"),
                    "--audit",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("Rota:", completed.stdout)
            self.assertIn('"economic"', completed.stdout)
            self.assertIn('"maximum_safety"', completed.stdout)
            self.assertNotIn("PRIVATE ACCEPTANCE", completed.stdout)
            self.assertFalse(
                (root / "runtime" / "telemetry" / "observations.jsonl").exists()
            )

    def test_validate_registry_accepts_the_complete_skill_contract(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "validate-registry.py"),
                "--skill-root",
                str(SCRIPTS.parent),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("registry valid", completed.stdout.strip())

    def test_local_observation_is_recorded_before_pareto_is_recomputed(self):
        import model_router.service as service_module
        from model_router import __version__
        from support import depth_failure_report, passing_report, run_args, service_fixture

        service = service_fixture(
            child_reports=(depth_failure_report(), passing_report()),
        )
        with mock.patch.object(
            service_module,
            "assess_routes",
            wraps=service_module.assess_routes,
        ) as assess, mock.patch.object(
            service_module,
            "select_routes",
            wraps=service_module.select_routes,
        ) as select:
            result = service.run(**run_args())

        self.assertEqual("pass", result.status)
        self.assertGreaterEqual(2, assess.call_count)
        self.assertGreaterEqual(2, select.call_count)
        self.assertEqual((), assess.call_args_list[0].args[3])
        observed = assess.call_args_list[1].args[3]
        self.assertEqual(1, len(observed))
        self.assertEqual(observed[0].route.model, observed[0].model_version)
        self.assertEqual(__version__, observed[0].engine_version)
        self.assertEqual(
            {"required_checks_passed", "test_failures", "type_errors"},
            set(observed[0].metrics),
        )

    def test_attempt_budget_and_observed_token_usage_are_separate(self):
        from model_router.executor import TokenUsage
        from support import (
            execution_result_fixture,
            passing_report,
            run_args,
            service_fixture,
        )

        service = service_fixture(
            execution_results=(
                execution_result_fixture(
                    child_report=passing_report(),
                    usage=TokenUsage(input_tokens=11, output_tokens=7),
                ),
            )
        )
        result = service.run(**run_args())
        payload = service.state.read_decision(result.decision_path.parent.name)

        self.assertEqual(1, result.attempt_count)
        self.assertEqual(11, result.input_tokens)
        self.assertEqual(7, result.output_tokens)
        self.assertEqual(1, payload["budget"]["attempts"])
        self.assertEqual(11, payload["budget"]["input_tokens"])
        self.assertEqual(7, payload["budget"]["output_tokens"])

    def test_skill_contract_is_guard_first_and_ui_metadata_is_exact(self):
        skill = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")
        expected_yaml = """interface:
  display_name: "Roteador Adaptativo de Modelos"
  short_description: "Roteia modelo e esforço com evidências"
  default_prompt: "Use $ag-rotear-modelo para escolher, executar e validar a rota ideal."
policy:
  allow_implicit_invocation: true
"""
        self.assertEqual(
            expected_yaml,
            (SCRIPTS.parent / "agents" / "openai.yaml").read_text(encoding="utf-8"),
        )
        ordered = (
            "AG_MODEL_ROUTER_CHILD=1",
            "Quando rotear",
            "Fingerprint",
            "Permissões",
            "Executar",
            "Entregar",
            "Referências",
        )
        positions = tuple(skill.index(marker) for marker in ordered)
        self.assertEqual(tuple(sorted(positions)), positions)
        architecture = SCRIPTS.parent / "references" / "architecture.md"
        self.assertTrue(architecture.is_file())
        architecture_text = architecture.read_text(encoding="utf-8")
        for marker in (
            "input-ready",
            "READY-last",
            "0700",
            "0600",
            "plaintext",
            "1 MiB",
            "512 bytes",
            "snapshot imutável",
            "stdin=PIPE",
            "pass_fds",
            "mesmo UID",
            "`TERM`",
            "`INT`",
            "`HUP`",
            "SIGKILL",
            "power loss",
            "`decision.json`",
            "`telemetry`",
        ):
            self.assertIn(marker, architecture_text)

    def test_cli_audit_reads_only_the_revalidated_decision(self):
        from support import passing_report, run_args, service_fixture

        service = service_fixture(
            child_reports=(passing_report(deliverable="PRIVATE RESULT"),)
        )
        result = service.run(**run_args(task_text="PRIVATE TASK"))
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "router.py"),
                "audit",
                "--run-id",
                result.decision_path.parent.name,
                "--runtime-root",
                str(service.state.root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual("1.0.0", payload["schema_version"])
        self.assertNotIn("PRIVATE TASK", completed.stdout)
        self.assertNotIn("PRIVATE RESULT", completed.stdout)

    def test_cli_promote_registry_validates_before_replacing_snapshot(self):
        from support import service_fixture

        service = service_fixture()
        service.state.bootstrap()
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "router.py"),
                "promote-registry",
                "--candidate",
                str(SCRIPTS.parent / "references" / "benchmark-registry.json"),
                "--runtime-root",
                str(service.state.root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("registry promoted", completed.stdout.strip())

    def test_timeout_without_report_retries_once_and_persists_decision(self):
        from support import execution_result_fixture, run_args, service_fixture

        timeout = execution_result_fixture(
            child_report=None,
            process_exit_code=None,
            failure_kind="timeout",
        )
        service = service_fixture(execution_results=(timeout, timeout))

        result = service.run(**run_args())

        self.assertEqual(2, service.executor.call_count)
        self.assertEqual(2, result.attempt_count)
        self.assertEqual("blocked", result.status)
        self.assertTrue(result.decision_path.is_file())

    def test_capacity_failure_switches_model_instead_of_repeating(self):
        from support import (
            execution_result_fixture,
            passing_report,
            run_args,
            service_fixture,
        )

        capacity = execution_result_fixture(
            child_report=None,
            failure_kind="capacity",
        )
        recovered = execution_result_fixture(
            thread_id="capacity-fallback-thread",
            child_report=passing_report(),
        )
        service = service_fixture(execution_results=(capacity, recovered))

        result = service.run(**run_args())

        self.assertEqual("pass", result.status)
        self.assertEqual(2, service.executor.call_count)
        self.assertNotEqual(
            service.executor.calls[0].route.model,
            service.executor.calls[1].route.model,
        )

    def test_global_gate_block_is_structured_auditable_and_has_no_route(self):
        from support import request_fixture, run_args, service_fixture

        service = service_fixture()
        request = request_fixture(
            external_effects=True,
            external_effects_authorized=False,
        )

        result = service.run(**run_args(request=request))
        payload = service.state.read_decision(result.decision_path.parent.name)

        self.assertEqual("blocked", result.status)
        self.assertEqual("external-effects-not-authorized", result.stop_reason)
        self.assertIsNone(result.initial_decision)
        self.assertIsNone(result.route)
        self.assertEqual(0, service.executor.call_count)
        self.assertEqual(
            {"blocked": "external-effects-not-authorized"},
            payload["selection"],
        )
        self.assertNotIn("gpt-", json.dumps(payload["selection"]))

    def test_blocked_result_never_exposes_unvalidated_deliverable(self):
        from support import (
            execution_result_fixture,
            passing_report,
            run_args,
            service_fixture,
        )

        service = service_fixture(
            execution_results=(
                execution_result_fixture(
                    child_report=passing_report(
                        deliverable="PRIVATE BLOCKED DELIVERABLE"
                    ),
                    process_exit_code=7,
                    failure_kind="process",
                ),
            )
        )

        result = service.run(**run_args())

        self.assertEqual("blocked", result.status)
        self.assertIsNone(result.final_content)

    def test_invalid_verifier_provenance_is_terminal_for_every_unsafe_id(self):
        from support import (
            passing_report,
            request_fixture,
            run_args,
            service_fixture,
            verifier_pass_report,
        )

        unsafe_pairs = (
            (None, "verifier-thread"),
            ("primary-thread", None),
            ("same-thread", "same-thread"),
            ("   ", "verifier-thread"),
            ("primary-thread", "\t"),
            (" primary-thread", "verifier-thread"),
            ("primary-thread", "verifier-thread "),
        )
        for primary, verifier in unsafe_pairs:
            with self.subTest(primary=primary, verifier=verifier):
                service = service_fixture(
                    child_reports=(passing_report(), verifier_pass_report()),
                    thread_ids=(primary, verifier),
                )
                result = service.run(
                    **run_args(
                        request=request_fixture(
                            impact="critical",
                            verifiability="weak",
                        )
                    )
                )

                self.assertEqual("blocked", result.status)
                self.assertEqual("verifier-provenance-invalid", result.stop_reason)
                self.assertEqual(
                    ["execute", "verify"],
                    [call.mode for call in service.executor.calls],
                )

    def test_zero_viable_select_is_structured_and_does_not_invent_route(self):
        from dataclasses import replace

        from support import catalog_fixture, request_fixture, service_fixture

        catalog = catalog_fixture()
        unavailable = catalog.__class__(
            catalog.source,
            catalog.observed_at,
            tuple(
                replace(model, supported_tools=(), supported_tools_known=True)
                for model in catalog.models
            ),
        )
        service = service_fixture(catalog=unavailable)

        result = service.select(
            request_fixture(required_tools=["private-tool"]),
            workdir=Path("/project"),
        )
        payload = service.state.read_decision(result.decision_path.parent.name)

        self.assertEqual("blocked", result.status)
        self.assertEqual("required-tools-unsupported", result.stop_reason)
        self.assertIsNone(result.decision)
        self.assertEqual(0, service.executor.call_count)
        self.assertEqual(
            {"blocked": "required-tools-unsupported"},
            payload["selection"],
        )

    def test_cli_prints_final_content_only_for_pass(self):
        import contextlib
        import io
        from types import SimpleNamespace

        import router as router_module
        from support import request_fixture

        blocked = SimpleNamespace(
            status="blocked",
            final_content="PRIVATE BLOCKED DELIVERABLE",
            route=None,
            initial_decision=None,
            attempt_count=1,
            decision_path=Path("/unused/run/decision.json"),
        )
        service = mock.Mock()
        service.run.return_value = blocked
        args = SimpleNamespace(
            request="request.json",
            workdir=".",
            sandbox="read-only",
            approval_policy="never",
            audit=False,
        )
        output = io.StringIO()
        with mock.patch.object(
            router_module, "_load_request", return_value=request_fixture()
        ), mock.patch.object(
            router_module, "_build_service", return_value=(service, mock.Mock())
        ), mock.patch.object(
            router_module, "_workdir", return_value=Path(".")
        ), contextlib.redirect_stdout(output):
            code = router_module._run(args, "task")

        self.assertEqual(1, code)
        self.assertNotIn("PRIVATE BLOCKED DELIVERABLE", output.getvalue())
        self.assertIn("Rota: bloqueada", output.getvalue())

    def test_catalog_runner_has_bounded_configurable_timeout_and_falls_back(self):
        import router as router_module
        from model_router.model_registry import discover_catalog

        cache = SCRIPTS / "tests" / "fixtures" / "models-cache.json"
        seed = SCRIPTS.parent / "references" / "model-registry.json"
        with mock.patch.object(
            router_module.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(("codex",), 10),
        ) as run:
            catalog = discover_catalog("codex", cache, seed, router_module._runner)

        self.assertEqual("cache", catalog.source)
        self.assertEqual(10.0, run.call_args.kwargs.get("timeout"))
        with mock.patch.dict(
            os.environ,
            {"AG_MODEL_ROUTER_CATALOG_TIMEOUT_SECONDS": "7.5"},
        ):
            self.assertEqual(7.5, router_module._catalog_timeout_seconds())

    def test_request_loader_uses_one_bounded_descriptor_at_exact_boundary(self):
        import router as router_module
        from support import valid_task_payload

        limit = 1024 * 1024
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "request.json"
            encoded = json.dumps(valid_task_payload()).encode("utf-8")
            path.write_bytes(encoded + b" " * (limit - len(encoded)))
            router_module._load_request(str(path))

            path.write_bytes(encoded + b" " * (limit + 1 - len(encoded)))
            with self.assertRaises(ValueError):
                router_module._load_request(str(path))

            path.write_bytes(encoded)
            with mock.patch.object(
                Path,
                "is_file",
                side_effect=AssertionError("check-then-open"),
            ), mock.patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("second path read"),
            ):
                router_module._load_request(str(path))

    @unittest.skipUnless(os.name == "posix", "anonymous fd transport is POSIX-only")
    def test_internal_request_fd_is_anonymous_and_public_path_stays_supported(self):
        import router as router_module
        from support import valid_task_payload

        encoded = json.dumps(valid_task_payload()).encode("utf-8")
        with tempfile.TemporaryFile(mode="w+b") as anonymous:
            anonymous.write(encoded)
            anonymous.flush()
            anonymous.seek(0)
            parsed = router_module._load_request_fd(anonymous.fileno())
        self.assertEqual(valid_task_payload(), parsed.to_dict())

        with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as named:
            named.write(encoded)
            named.flush()
            named_path = Path(named.name)
            with self.assertRaises(router_module.CliError):
                router_module._load_request_fd(named.fileno())
        named_path.unlink()

        parser = router_module.build_parser()
        public = parser.parse_args(
            [
                "run",
                "--request",
                "request.json",
                "--workdir",
                ".",
                "--sandbox",
                "read-only",
                "--approval-policy",
                "never",
            ]
        )
        self.assertEqual("request.json", public.request)
        self.assertIsNone(public.request_fd)

    def test_cli_sanitizes_request_contract_errors(self):
        from support import valid_task_payload

        secret = "PRIVATE-API-TOKEN-XYZ"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_path = root / "request.json"
            payload = valid_task_payload()
            payload[secret] = "value"
            request_path.write_text(json.dumps(payload), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "router.py"),
                    "select",
                    "--request",
                    str(request_path),
                    "--workdir",
                    str(root),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(1, completed.returncode)
        self.assertNotIn(secret, completed.stderr)
        self.assertEqual("router error: invalid request\n", completed.stderr)

    @unittest.skipUnless(
        DIRECTORY_SYMLINK_AVAILABLE,
        "directory symlinks unavailable",
    )
    def test_runtime_and_cache_paths_expand_to_absolute_without_resolving(self):
        import router as router_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            target = root / "target"
            target.mkdir()
            link = root / "runtime-link"
            link.symlink_to(target, target_is_directory=True)
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                self.assertEqual(
                    home / "runtime",
                    router_module._runtime_root("~/runtime"),
                )
                self.assertEqual(
                    home / "cache.json",
                    router_module._models_cache("~/cache.json"),
                )
            self.assertEqual(link.absolute(), router_module._runtime_root(str(link)))
            self.assertNotEqual(target.resolve(), router_module._runtime_root(str(link)))

    def test_reused_service_starts_a_fresh_budget_for_each_run(self):
        from support import passing_report, run_args, service_fixture

        service = service_fixture(
            child_reports=(passing_report(), passing_report()),
        )

        first = service.run(**run_args())
        second = service.run(**run_args())

        self.assertEqual(1, first.attempt_count)
        self.assertEqual(1, second.attempt_count)

    def test_skill_uses_concrete_tool_steps_and_absolute_launcher(self):
        skill = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")
        for marker in (
            "diretório temporário privado fora do workdir",
            "request.json",
            "task.txt",
            "READY",
            "guardian_pid",
            "0600",
            "--request",
            "--workdir",
            "--private-temp-root",
            "AG_MODEL_ROUTER_PRIVATE_TEMP_ROOT",
            "--sandbox",
            "--approval-policy",
            '${CODEX_HOME:-$HOME/.codex}/skills/ag-rotear-modelo/scripts/run-route.sh',
            '${CODEX_HOME:-$HOME/.codex}/skills/ag-rotear-modelo/scripts/guarded-run.py',
            '${CODEX_HOME:-$HOME/.codex}/skills/ag-rotear-modelo/scripts/codex-child-supervisor.py',
            '${CODEX_HOME:-$HOME/.codex}/skills/ag-rotear-modelo/scripts/publish-ready.py',
            "ready_nonce",
            "SHA-256",
            "--request-fd",
            "`exec_command`",
            "`apply_patch`",
            "session id",
            "poll",
            "timeout de preparação",
            "/bin/kill -TERM",
            "Stop-Process -Id",
            "DACL protegida",
            "Selected model is at capacity",
            "outro modelo",
        ):
            self.assertIn(marker, skill)
        self.assertIn("stdin", skill.lower())
        self.assertIn("não execute a tarefa novamente", skill.lower())
        self.assertNotIn("$TASK_TEXT", skill)
        self.assertNotIn("$TASK_REQUEST_JSON", skill)
        self.assertNotIn("write_stdin", skill)
        self.assertNotIn("PTY", skill)
        self.assertNotIn("EOT", skill)
        self.assertNotIn("Em um bloco `finally`", skill)
        self.assertNotIn("scripts/run-route.sh run", skill)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            installed = codex_home / "skills" / "ag-rotear-modelo"
            shutil.copytree(SCRIPTS.parent, installed)
            arbitrary_cwd = root / "arbitrary-cwd"
            arbitrary_cwd.mkdir()
            launcher = installed / "scripts" / "run-route.py"
            completed = subprocess.run(
                [sys.executable, str(launcher), "--help"],
                cwd=str(arbitrary_cwd),
                env={**os.environ, "CODEX_HOME": str(codex_home)},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("promote-registry", completed.stdout)

    @unittest.skipUnless(POSIX, "POSIX shell launcher only")
    def test_absolute_launcher_receives_task_file_exactly_then_eof_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            installed = codex_home / "skills" / "ag-rotear-modelo"
            shutil.copytree(SCRIPTS.parent, installed)
            arbitrary_cwd = root / "arbitrary-cwd"
            arbitrary_cwd.mkdir()
            capture_dir = root / "capture"
            capture_dir.mkdir()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/bin/sh\n"
                'cat > "$CAPTURE_STDIN"\n'
                'printf \'%s\\n\' "$@" > "$CAPTURE_ARGV"\n'
                'printf \'eof\\n\' > "$CAPTURE_EOF"\n',
                encoding="utf-8",
            )
            fake_python.chmod(0o700)
            private_dir = Path(tempfile.mkdtemp(prefix="router-input-", dir=str(root)))
            request_path = private_dir / "request.json"
            task_path = private_dir / "task.txt"
            task_bytes = b"linha 1\n$(touch SHOULD_NOT_EXIST) ' \" $HOME\nfinal-sem-newline"
            request_path.write_text("{}\n", encoding="utf-8")
            task_path.write_bytes(task_bytes)
            request_path.chmod(0o600)
            task_path.chmod(0o600)
            stdin_capture = capture_dir / "stdin.bin"
            argv_capture = capture_dir / "argv.txt"
            eof_capture = capture_dir / "eof.txt"
            controlled_arguments = (
                "run",
                "--request",
                str(request_path),
                "--workdir",
                str(arbitrary_cwd),
                "--sandbox",
                "read-only",
                "--approval-policy",
                "never",
            )
            command = (
                '"${CODEX_HOME:-$HOME/.codex}/skills/'
                'ag-rotear-modelo/scripts/run-route.sh" '
            )
            command += " ".join(
                shlex.quote(item) for item in controlled_arguments
            )
            command += " < " + shlex.quote(str(task_path))
            try:
                completed = subprocess.run(
                    ["/bin/sh", "-c", command],
                    cwd=str(arbitrary_cwd),
                    env={
                        **os.environ,
                        "CODEX_HOME": str(codex_home),
                        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
                        "CAPTURE_STDIN": str(stdin_capture),
                        "CAPTURE_ARGV": str(argv_capture),
                        "CAPTURE_EOF": str(eof_capture),
                    },
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
            finally:
                shutil.rmtree(private_dir)

            self.assertEqual(0, completed.returncode, completed.stderr.decode())
            self.assertEqual(task_bytes, stdin_capture.read_bytes())
            self.assertEqual("eof\n", eof_capture.read_text(encoding="utf-8"))
            self.assertNotIn("SHOULD_NOT_EXIST", argv_capture.read_text(encoding="utf-8"))
            self.assertFalse((arbitrary_cwd / "SHOULD_NOT_EXIST").exists())
            self.assertFalse(private_dir.exists())


if __name__ == "__main__":
    unittest.main()
