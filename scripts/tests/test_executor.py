import json
import os
import signal
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from model_router.contracts import ApprovalPolicy, Effort, Route, SandboxMode  # noqa: E402
from model_router.executor import (  # noqa: E402
    CHILD_SUPERVISOR,
    CONTROL_FD_ENV,
    MAX_TOKEN_COUNT,
    CodexExecutor,
    ExecutionBlocked,
    ExecutionProtocolError,
    build_argv,
    parse_jsonl,
)
from support import (  # noqa: E402
    FakeClock,
    FakeProcessFactory,
    execution_args,
    valid_child_report_payload,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ExecutorTests(unittest.TestCase):
    def test_builds_exact_safe_argv_without_shell(self):
        project = Path(tempfile.gettempdir()) / "project"
        schema = Path(tempfile.gettempdir()) / "child-result-schema.json"
        argv = build_argv(
            codex_bin="codex",
            cwd=project,
            route=Route("gpt-5.6-luna", Effort.MEDIUM),
            sandbox=SandboxMode.READ_ONLY,
            approval=ApprovalPolicy.NEVER,
            schema_path=schema,
        )

        self.assertEqual(
            [
                "codex",
                "-a",
                "never",
                "exec",
                "--ephemeral",
                "--strict-config",
                "--json",
                "--color",
                "never",
                "-C",
                str(project),
                "-m",
                "gpt-5.6-luna",
                "-c",
                'model_reasoning_effort="medium"',
                "-c",
                'service_tier="default"',
                "-s",
                "read-only",
                "--output-schema",
                str(schema),
                "-",
            ],
            argv,
        )
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_parses_last_agent_message_and_usage_in_memory(self):
        result = parse_jsonl(
            (FIXTURES / "codex-events-success.jsonl").read_text().splitlines()
        )

        self.assertEqual("thread-smoke", result.thread_id)
        self.assertEqual(120, result.usage.input_tokens)
        self.assertEqual(20, result.usage.cached_input_tokens)
        self.assertEqual(40, result.usage.output_tokens)
        self.assertEqual(8, result.usage.reasoning_output_tokens)
        self.assertEqual("pass", result.child_report.status)

    def test_child_environment_and_prompt_have_both_recursion_markers(self):
        executor = CodexExecutor(process_factory=FakeProcessFactory.success())

        result = executor.execute(**execution_args())

        factory = executor.process_factory
        self.assertEqual("1", factory.last_env["AG_MODEL_ROUTER_CHILD"])
        self.assertIn(
            "[AG_MODEL_ROUTER_CHILD=1]",
            factory.process.communicated_input,
        )
        self.assertIn(
            "Não invoque ag-rotear-modelo",
            factory.process.communicated_input,
        )
        self.assertNotIn(execution_args()["task_text"], " ".join(factory.last_argv))
        self.assertIs(factory.last_kwargs["shell"], False)
        self.assertIs(type(factory.last_argv), list)
        self.assertEqual(0, result.process_exit_code)
        self.assertFalse(hasattr(result, "stdout"))
        self.assertFalse(hasattr(result, "task_text"))

    @unittest.skipUnless(os.name == "posix", "guardian control fd is POSIX-only")
    def test_guardian_control_wraps_codex_and_passes_only_control_fd(self):
        read_descriptor, write_descriptor = os.pipe()
        self.addCleanup(os.close, read_descriptor)
        self.addCleanup(os.close, write_descriptor)
        factory = FakeProcessFactory.success()
        with mock.patch.dict(
            os.environ,
            {CONTROL_FD_ENV: str(write_descriptor)},
            clear=False,
        ):
            CodexExecutor(process_factory=factory).execute(**execution_args())

        self.assertEqual(sys.executable, factory.last_argv[0])
        self.assertEqual(str(CHILD_SUPERVISOR), factory.last_argv[1])
        self.assertEqual("--", factory.last_argv[2])
        self.assertEqual("codex", factory.last_argv[3])
        self.assertEqual((write_descriptor,), factory.last_kwargs["pass_fds"])
        self.assertTrue(factory.last_kwargs["close_fds"])

    def test_missing_or_invalid_permissions_fail_closed_before_subprocess(self):
        invalid_permissions = (
            ("sandbox", None, "sandbox_mode"),
            ("sandbox", "read-only", "sandbox_mode"),
            ("approval", None, "approval_policy"),
            ("approval", "never", "approval_policy"),
        )
        for field_name, value, expected_message in invalid_permissions:
            with self.subTest(field_name=field_name, value=value):
                factory = FakeProcessFactory.success()
                executor = CodexExecutor(process_factory=factory)
                with self.assertRaisesRegex(ExecutionBlocked, expected_message):
                    executor.execute(**execution_args(**{field_name: value}))
                self.assertEqual(0, factory.call_count)

    def test_verify_mode_requires_read_only_and_forbids_mutation_in_prompt(self):
        blocked_factory = FakeProcessFactory.success()
        blocked = CodexExecutor(process_factory=blocked_factory)
        with self.assertRaisesRegex(ExecutionBlocked, "verify.*read-only"):
            blocked.execute(
                **execution_args(
                    mode="verify",
                    sandbox=SandboxMode.WORKSPACE_WRITE,
                )
            )
        self.assertEqual(0, blocked_factory.call_count)

        factory = FakeProcessFactory.success()
        executor = CodexExecutor(process_factory=factory)
        executor.execute(**execution_args(mode="verify"))
        prompt = factory.process.communicated_input
        self.assertIn("Modo: verify", prompt)
        self.assertIn("Não altere arquivos", prompt)

    def test_unknown_mode_fails_before_subprocess(self):
        factory = FakeProcessFactory.success()
        executor = CodexExecutor(process_factory=factory)

        with self.assertRaisesRegex(ExecutionBlocked, "mode"):
            executor.execute(**execution_args(mode="plan"))

        self.assertEqual(0, factory.call_count)

    def test_falsy_non_mapping_feedback_fails_before_subprocess(self):
        for feedback in ([], "", 0, False):
            with self.subTest(feedback=feedback):
                factory = FakeProcessFactory.success()
                executor = CodexExecutor(process_factory=factory)

                with self.assertRaisesRegex(ExecutionBlocked, "feedback"):
                    executor.execute(**execution_args(feedback=feedback))

                self.assertEqual(0, factory.call_count)

    def test_timeout_terminates_then_kills_after_grace_period(self):
        factory = FakeProcessFactory.hanging(
            stderr="Authorization: Bearer child-secret"
        )
        executor = CodexExecutor(
            process_factory=factory,
            kill_grace_seconds=2,
        )

        result = executor.execute(**execution_args(timeout_seconds=1))

        self.assertEqual("timeout", result.failure_kind)
        self.assertIsNone(result.process_exit_code)
        self.assertEqual(["terminate", "kill"], factory.process.lifecycle)
        self.assertEqual([1.0, 2.0, 2.0], [
            call["timeout"] for call in factory.process.communicate_calls
        ])
        self.assertNotIn("child-secret", result.stderr_summary)
        self.assertIn("redacted", result.stderr_summary)
        self.assertFalse(hasattr(result, "stdout"))

    def test_post_kill_open_pipes_return_timeout_after_bounded_reap(self):
        factory = FakeProcessFactory.hanging(pipes_never_close=True)
        executor = CodexExecutor(
            process_factory=factory,
            kill_grace_seconds=0.01,
        )

        result = executor.execute(**execution_args(timeout_seconds=0.01))

        self.assertEqual("timeout", result.failure_kind)
        self.assertEqual(
            [0.01, 0.01, 0.01],
            [call["timeout"] for call in factory.process.communicate_calls],
        )
        self.assertEqual([0.0], factory.process.wait_calls)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups only")
    def test_posix_timeout_starts_session_and_signals_process_group(self):
        factory = FakeProcessFactory.hanging(pid=4242)

        def record_group_signal(_pid, sent_signal):
            if sent_signal == signal.SIGTERM:
                factory.process.terminated = True
            elif sent_signal == signal.SIGKILL:
                factory.process.killed = True

        with mock.patch(
            "model_router.executor.os.killpg",
            side_effect=record_group_signal,
        ) as kill_group:
            result = CodexExecutor(
                process_factory=factory,
                kill_grace_seconds=0.01,
            ).execute(**execution_args(timeout_seconds=0.01))

        self.assertEqual("timeout", result.failure_kind)
        self.assertIs(factory.last_kwargs["start_new_session"], True)
        self.assertEqual(
            [
                mock.call(4242, signal.SIGTERM),
                mock.call(4242, signal.SIGKILL),
            ],
            kill_group.call_args_list,
        )
        self.assertEqual([], factory.process.lifecycle)

    def test_elapsed_time_uses_injected_monotonic_clock_and_never_goes_negative(self):
        clock = FakeClock(initial=10.0)
        factory = FakeProcessFactory.success(
            clock=clock,
            elapsed_seconds=3.5,
        )
        result = CodexExecutor(process_factory=factory, clock=clock).execute(
            **execution_args()
        )
        self.assertEqual(3.5, result.elapsed_seconds)

        backwards_clock = FakeClock(initial=10.0)
        backwards_factory = FakeProcessFactory.success(
            clock=backwards_clock,
            elapsed_seconds=-5.0,
        )
        backwards = CodexExecutor(
            process_factory=backwards_factory,
            clock=backwards_clock,
        ).execute(**execution_args())
        self.assertEqual(0.0, backwards.elapsed_seconds)

    def test_invalid_timeout_fails_before_subprocess(self):
        for timeout in (0, -1, True, "1", float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                factory = FakeProcessFactory.success()
                executor = CodexExecutor(process_factory=factory)
                with self.assertRaisesRegex(ExecutionBlocked, "timeout_seconds"):
                    executor.execute(**execution_args(timeout_seconds=timeout))
                self.assertEqual(0, factory.call_count)

    def test_overflowing_time_values_are_blocked_before_subprocess(self):
        huge = 10**400

        timeout_factory = FakeProcessFactory.success()
        with self.assertRaisesRegex(ExecutionBlocked, "timeout_seconds"):
            CodexExecutor(process_factory=timeout_factory).execute(
                **execution_args(timeout_seconds=huge)
            )
        self.assertEqual(0, timeout_factory.call_count)

        clock_factory = FakeProcessFactory.success()
        with self.assertRaisesRegex(ExecutionBlocked, "monotonic clock"):
            CodexExecutor(
                process_factory=clock_factory,
                clock=lambda: huge,
            ).execute(**execution_args())
        self.assertEqual(0, clock_factory.call_count)

        grace_factory = FakeProcessFactory.success()
        with self.assertRaisesRegex(ExecutionBlocked, "kill_grace_seconds"):
            CodexExecutor(
                process_factory=grace_factory,
                kill_grace_seconds=huge,
            )
        self.assertEqual(0, grace_factory.call_count)

    def test_process_failure_is_classified_and_stderr_is_fully_redacted(self):
        raw_stderr = (
            "Authorization: Bearer child-secret\n"
            "failed while reading /Users/private/customer-task.txt"
        )
        factory = FakeProcessFactory.success(stderr=raw_stderr, returncode=7)

        result = CodexExecutor(process_factory=factory).execute(**execution_args())

        self.assertEqual("process", result.failure_kind)
        self.assertEqual(7, result.process_exit_code)
        self.assertNotEqual(raw_stderr, result.stderr_summary)
        self.assertNotIn("child-secret", result.stderr_summary)
        self.assertNotIn("customer-task", result.stderr_summary)
        self.assertIn("redacted", result.stderr_summary)

    def test_default_schema_is_final_argv_input_and_exists(self):
        factory = FakeProcessFactory.success()
        CodexExecutor(process_factory=factory).execute(**execution_args())

        schema_index = factory.last_argv.index("--output-schema") + 1
        schema_path = Path(factory.last_argv[schema_index])
        self.assertEqual("child-result-schema.json", schema_path.name)
        self.assertTrue(schema_path.is_file())
        self.assertEqual("-", factory.last_argv[-1])

    def test_parses_structured_failure_events(self):
        result = parse_jsonl(
            (FIXTURES / "codex-events-failure.jsonl").read_text().splitlines()
        )

        self.assertEqual("thread-failure", result.thread_id)
        self.assertEqual("codex-event", result.failure_kind)
        self.assertIsNone(result.child_report)

    def test_capacity_failure_event_is_sanitized_for_route_fallback(self):
        lines = (
            json.dumps(
                {"type": "thread.started", "thread_id": "thread-capacity"}
            ),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "message": (
                            "Selected model is at capacity. "
                            "Please try a different model."
                        ),
                        "type": "server_error",
                    },
                }
            ),
        )

        result = parse_jsonl(lines)

        self.assertEqual("capacity", result.failure_kind)
        self.assertEqual("", result.stderr_summary)
        self.assertIsNone(result.child_report)

    def test_failure_event_invalidates_a_later_pass_report(self):
        lines = (
            json.dumps({"type": "thread.started", "thread_id": "thread-one"}),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"message": "child failed"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(valid_child_report_payload()),
                    },
                }
            ),
        )

        result = parse_jsonl(lines)

        self.assertEqual("codex-event", result.failure_kind)
        self.assertIsNone(result.child_report)

    def test_nonzero_exit_invalidates_a_parsed_pass_report(self):
        factory = FakeProcessFactory.success(returncode=7)

        result = CodexExecutor(process_factory=factory).execute(**execution_args())

        self.assertEqual("process", result.failure_kind)
        self.assertEqual(7, result.process_exit_code)
        self.assertIsNone(result.child_report)

    def test_rejects_unstructured_failure_events(self):
        invalid_events = (
            {"type": "turn.failed"},
            {"type": "turn.failed", "error": "failed"},
            {"type": "turn.failed", "error": {}},
            {"type": "error"},
            {"type": "error", "message": "   "},
        )
        for event in invalid_events:
            with self.subTest(event=event):
                lines = (
                    json.dumps(
                        {"type": "thread.started", "thread_id": "thread-one"}
                    ),
                    json.dumps(event),
                )
                with self.assertRaisesRegex(ExecutionProtocolError, "failed|error"):
                    parse_jsonl(lines)

    def test_rejects_invalid_jsonl_and_non_object_events(self):
        for invalid_line in ("{", "[]", '"text"', "NaN"):
            with self.subTest(invalid_line=invalid_line):
                with self.assertRaisesRegex(ExecutionProtocolError, "JSONL"):
                    parse_jsonl((invalid_line,))

    def test_invalid_jsonl_does_not_retain_private_content_in_exception_chain(self):
        sentinel = "PRIVATE_TASK_CONTENT_7"
        invalid_line = '{"type":"thread.started","private":"%s"' % sentinel

        with self.assertRaises(ExecutionProtocolError) as raised:
            parse_jsonl((invalid_line,))

        error = raised.exception
        rendered = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        self.assertNotIn(sentinel, str(error))
        self.assertNotIn(sentinel, rendered)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_requires_nonempty_thread_id(self):
        cases = (
            ({"type": "thread.started", "thread_id": ""}, "thread_id"),
            ({"type": "thread.started", "thread_id": "  "}, "thread_id"),
            ({"type": "future.event"}, "thread_id"),
        )
        for event, expected_message in cases:
            with self.subTest(event=event):
                with self.assertRaisesRegex(
                    ExecutionProtocolError,
                    expected_message,
                ):
                    parse_jsonl((json.dumps(event),))

    def test_rejects_unsafe_usage_types_and_ranges(self):
        invalid_values = (True, "1", -1, MAX_TOKEN_COUNT + 1, 1.0)
        fields = (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
        for field_name in fields:
            for invalid_value in invalid_values:
                with self.subTest(field_name=field_name, value=invalid_value):
                    lines = (
                        json.dumps(
                            {
                                "type": "thread.started",
                                "thread_id": "thread-one",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {field_name: invalid_value},
                            }
                        ),
                    )
                    with self.assertRaisesRegex(
                        ExecutionProtocolError,
                        field_name,
                    ):
                        parse_jsonl(lines)

    def test_final_agent_message_must_be_strict_child_report(self):
        invalid_report = {
            "status": "pass",
            "deliverable": "done",
            "evidence": [],
            "metrics": {},
            "failure_class": None,
            "next_hint": None,
            "unexpected": True,
        }
        lines = (
            json.dumps({"type": "thread.started", "thread_id": "thread-one"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(invalid_report),
                    },
                }
            ),
        )

        with self.assertRaisesRegex(
            ExecutionProtocolError,
            "final agent_message",
        ):
            parse_jsonl(lines)

    def test_invalid_child_report_does_not_retain_private_exception_context(self):
        sentinel = "PRIVATE_CHILD_REPORT_CONTENT_7"
        lines = (
            json.dumps({"type": "thread.started", "thread_id": "thread-one"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": '{"private":"%s"' % sentinel,
                    },
                }
            ),
        )

        with self.assertRaises(ExecutionProtocolError) as raised:
            parse_jsonl(lines)

        error = raised.exception
        rendered = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        self.assertNotIn(sentinel, str(error))
        self.assertNotIn(sentinel, rendered)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_unknown_events_and_fields_are_ignored(self):
        valid_report = {
            "status": "pass",
            "deliverable": "done",
            "evidence": [],
            "metrics": {},
            "failure_class": None,
            "next_hint": None,
        }
        lines = (
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "thread-one",
                    "future": {"nested": True},
                }
            ),
            json.dumps({"type": "future.event", "payload": [1, 2, 3]}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "future_item", "payload": True},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(valid_report),
                        "future": True,
                    },
                    "future": True,
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"future_tokens": 999},
                    "future": True,
                }
            ),
        )

        result = parse_jsonl(lines)

        self.assertEqual("pass", result.child_report.status)
        self.assertEqual(0, result.usage.input_tokens)

    def test_missing_agent_message_is_classified_without_persisting_jsonl(self):
        raw_line = json.dumps(
            {
                "type": "thread.started",
                "thread_id": "thread-no-message",
                "sensitive": "customer-task-secret",
            }
        )

        result = parse_jsonl((raw_line,))

        self.assertEqual("missing-agent-message", result.failure_kind)
        self.assertNotIn("customer-task-secret", repr(result))


if __name__ == "__main__":
    unittest.main()
