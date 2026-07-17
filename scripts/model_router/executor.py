import json
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, List, Mapping, Optional

from .contracts import (
    ApprovalPolicy,
    ChildReport,
    ContractError,
    Route,
    SandboxMode,
    TaskRequest,
)


MAX_TOKEN_COUNT = (1 << 63) - 1
DEFAULT_KILL_GRACE_SECONDS = 5.0
CHILD_MARKER = "AG_MODEL_ROUTER_CHILD"
CONTROL_FD_ENV = "AG_MODEL_ROUTER_CONTROL_FD"
CHILD_SUPERVISOR = Path(__file__).resolve().parents[1] / "codex-child-supervisor.py"
EXECUTION_MODES = ("execute", "verify")


def _posix_runtime():
    return os.name == "posix"


def _windows_runtime():
    return os.name == "nt"


class ExecutionBlocked(ValueError):
    pass


class ExecutionProtocolError(ValueError):
    pass


def _require_nonempty_string(value, path):
    if type(value) is not str or not value.strip():
        raise ExecutionProtocolError("%s must be a non-empty string" % path)
    return value


def _parse_token_count(value, path):
    if type(value) is not int or not 0 <= value <= MAX_TOKEN_COUNT:
        raise ExecutionProtocolError(
            "%s must be an integer between 0 and %d" % (path, MAX_TOKEN_COUNT)
        )
    return value


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant: %s" % value)


def _load_json(value):
    return json.loads(value, parse_constant=_reject_json_constant)


def _validate_failure_event(event, event_type):
    if event_type == "turn.failed":
        error = event.get("error")
        if type(error) is not dict:
            raise ExecutionProtocolError("turn.failed.error must be an object")
        _require_nonempty_string(
            error.get("message"),
            "turn.failed.error.message",
        )
        return
    _require_nonempty_string(event.get("message"), "error.message")


def _failure_event_kind(event, event_type):
    _validate_failure_event(event, event_type)
    error = event["error"] if event_type == "turn.failed" else event
    message = error.get("message", "").casefold()
    error_type = error.get("type")
    normalized_type = (
        error_type.casefold().replace("-", "_")
        if type(error_type) is str
        else ""
    )
    if normalized_type in (
        "capacity",
        "model_capacity",
        "model_overloaded",
        "overloaded",
    ) or any(
        marker in message
        for marker in (
            "selected model is at capacity",
            "model is at capacity",
            "model capacity is temporarily unavailable",
        )
    ):
        return "capacity"
    if normalized_type in ("transient", "temporarily_unavailable"):
        return "transient"
    return "codex-event"


def _require_positive_number(value, path):
    if type(value) not in (int, float):
        raise ExecutionBlocked("%s must be a positive finite number" % path)
    try:
        parsed = float(value)
    except OverflowError:
        raise ExecutionBlocked(
            "%s must be a positive finite number" % path
        ) from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise ExecutionBlocked("%s must be a positive finite number" % path)
    return parsed


def _read_clock(clock):
    try:
        value = clock()
    except Exception as error:
        raise ExecutionBlocked("monotonic clock is unavailable") from error
    if type(value) not in (int, float):
        raise ExecutionBlocked("monotonic clock must return a finite number")
    try:
        parsed = float(value)
    except OverflowError:
        raise ExecutionBlocked(
            "monotonic clock must return a finite number"
        ) from None
    if not math.isfinite(parsed):
        raise ExecutionBlocked("monotonic clock must return a finite number")
    return parsed


def _elapsed_seconds(clock, started):
    try:
        finished = clock()
    except Exception:
        return 0.0
    if type(finished) not in (int, float):
        return 0.0
    try:
        parsed = float(finished)
    except OverflowError:
        return 0.0
    if not math.isfinite(parsed) or parsed < started:
        return 0.0
    return parsed - started


def _absolute_path(value, path):
    try:
        return Path(value).absolute()
    except (OSError, TypeError, ValueError) as error:
        raise ExecutionBlocked("%s must be a valid path" % path) from error


def _signal_process_tree(process, force):
    if _posix_runtime():
        pid = getattr(process, "pid", None)
        if type(pid) is int and pid > 0:
            sent_signal = signal.SIGKILL if force else signal.SIGTERM
            try:
                os.killpg(pid, sent_signal)
                return
            except OSError:
                pass
    elif _windows_runtime():
        pid = getattr(process, "pid", None)
        if type(pid) is int and pid > 0:
            command = ["taskkill", "/PID", str(pid), "/T"]
            if force:
                command.append("/F")
            try:
                subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    shell=False,
                )
                return
            except OSError:
                pass
    fallback = process.kill if force else process.terminate
    try:
        fallback()
    except OSError:
        pass


def _guardian_control_fd():
    if not _posix_runtime():
        return None
    value = os.environ.get(CONTROL_FD_ENV)
    if value is None:
        return None
    try:
        descriptor = int(value)
    except (TypeError, ValueError):
        raise ExecutionBlocked("%s must be a valid file descriptor" % CONTROL_FD_ENV)
    if descriptor < 3:
        raise ExecutionBlocked("%s must be a valid file descriptor" % CONTROL_FD_ENV)
    try:
        os.fstat(descriptor)
    except OSError as error:
        raise ExecutionBlocked("%s is unavailable" % CONTROL_FD_ENV) from error
    if not CHILD_SUPERVISOR.is_file():
        raise ExecutionBlocked("child supervisor is unavailable")
    return descriptor


def _reap_nonblocking(process):
    try:
        process.wait(timeout=0.0)
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        pass


def summarize_stderr(stderr):
    if stderr is None or stderr == "":
        return ""
    if type(stderr) is not str:
        stderr = str(stderr)
    lowered = stderr.lower()
    if any(
        marker in lowered
        for marker in ("authorization", "bearer", "credential", "token")
    ):
        category = "authentication"
    elif any(
        marker in lowered
        for marker in ("permission", "sandbox", "denied")
    ):
        category = "permission"
    elif any(marker in lowered for marker in ("timeout", "timed out")):
        category = "timeout"
    else:
        category = "process"
    line_count = max(1, len(stderr.splitlines()))
    return "%s: stderr content redacted (%d line(s), %d character(s))" % (
        category,
        line_count,
        len(stderr),
    )


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    def __post_init__(self):
        for field_name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ):
            _parse_token_count(getattr(self, field_name), "usage.%s" % field_name)


@dataclass(frozen=True)
class ExecutionResult:
    thread_id: Optional[str]
    child_report: Optional[ChildReport]
    usage: TokenUsage
    process_exit_code: Optional[int]
    stderr_summary: str
    elapsed_seconds: float
    failure_kind: Optional[str]

    @classmethod
    def timeout(cls, elapsed_seconds, stderr):
        return cls(
            thread_id=None,
            child_report=None,
            usage=TokenUsage(),
            process_exit_code=None,
            stderr_summary=summarize_stderr(stderr),
            elapsed_seconds=elapsed_seconds,
            failure_kind="timeout",
        )

    @classmethod
    def spawn_failure(cls, elapsed_seconds, error):
        return cls(
            thread_id=None,
            child_report=None,
            usage=TokenUsage(),
            process_exit_code=None,
            stderr_summary=summarize_stderr(error),
            elapsed_seconds=elapsed_seconds,
            failure_kind="spawn",
        )

    def with_process(self, exit_code, stderr, elapsed_seconds):
        if type(exit_code) is not int:
            raise ExecutionProtocolError("process exit code must be an integer")
        failure_kind = self.failure_kind
        if exit_code != 0 and failure_kind is None:
            failure_kind = "process"
        return replace(
            self,
            child_report=None if failure_kind is not None else self.child_report,
            process_exit_code=exit_code,
            stderr_summary=summarize_stderr(stderr),
            elapsed_seconds=elapsed_seconds,
            failure_kind=failure_kind,
        )


def parse_jsonl(lines: Iterable[str]) -> ExecutionResult:
    thread_id = None
    last_agent_message = None
    usage = TokenUsage()
    failure_kind = None
    for line_number, line in enumerate(lines, start=1):
        if type(line) is not str:
            raise ExecutionProtocolError("JSONL line %d must be text" % line_number)
        if not line.strip():
            continue
        invalid_json = False
        try:
            event = _load_json(line)
        except (TypeError, ValueError):
            invalid_json = True
            event = None
        if invalid_json:
            raise ExecutionProtocolError(
                "invalid JSONL at line %d" % line_number
            ) from None
        if type(event) is not dict:
            raise ExecutionProtocolError(
                "JSONL event at line %d must be an object" % line_number
            )
        event_type = event.get("type")
        if event_type == "thread.started":
            parsed_thread_id = _require_nonempty_string(
                event.get("thread_id"),
                "thread.started.thread_id",
            )
            if thread_id is not None and parsed_thread_id != thread_id:
                raise ExecutionProtocolError("thread_id changed within JSONL stream")
            thread_id = parsed_thread_id
        elif event_type == "item.completed":
            item = event.get("item")
            if type(item) is not dict:
                raise ExecutionProtocolError("item.completed.item must be an object")
            if item.get("type") == "agent_message":
                last_agent_message = _require_nonempty_string(
                    item.get("text"),
                    "item.completed.item.text",
                )
        elif event_type == "turn.completed":
            raw_usage = event.get("usage", {})
            if type(raw_usage) is not dict:
                raise ExecutionProtocolError(
                    "turn.completed.usage must be an object"
                )
            usage = TokenUsage(
                input_tokens=_parse_token_count(
                    raw_usage.get("input_tokens", 0),
                    "turn.completed.usage.input_tokens",
                ),
                cached_input_tokens=_parse_token_count(
                    raw_usage.get("cached_input_tokens", 0),
                    "turn.completed.usage.cached_input_tokens",
                ),
                output_tokens=_parse_token_count(
                    raw_usage.get("output_tokens", 0),
                    "turn.completed.usage.output_tokens",
                ),
                reasoning_output_tokens=_parse_token_count(
                    raw_usage.get("reasoning_output_tokens", 0),
                    "turn.completed.usage.reasoning_output_tokens",
                ),
            )
        elif event_type in ("turn.failed", "error"):
            event_failure = _failure_event_kind(event, event_type)
            if failure_kind != "capacity" or event_failure == "capacity":
                failure_kind = event_failure

    if thread_id is None:
        raise ExecutionProtocolError("thread_id is required")

    report = None
    if last_agent_message is not None and failure_kind is None:
        invalid_report = False
        try:
            report = ChildReport.from_dict(_load_json(last_agent_message))
        except (TypeError, ValueError, ContractError):
            invalid_report = True
        if invalid_report:
            raise ExecutionProtocolError(
                "final agent_message violates child schema"
            ) from None
    elif failure_kind is None:
        failure_kind = "missing-agent-message"

    return ExecutionResult(
        thread_id=thread_id,
        child_report=report,
        usage=usage,
        process_exit_code=None,
        stderr_summary="",
        elapsed_seconds=0.0,
        failure_kind=failure_kind,
    )


def build_argv(
    codex_bin: str,
    cwd: Path,
    route: Route,
    sandbox: SandboxMode,
    approval: ApprovalPolicy,
    schema_path: Path,
) -> List[str]:
    if type(sandbox) is not SandboxMode:
        raise ExecutionBlocked("sandbox_mode is required")
    if type(approval) is not ApprovalPolicy:
        raise ExecutionBlocked("approval_policy is required")
    if type(route) is not Route:
        raise ExecutionBlocked("route is required")
    if type(codex_bin) is not str or not codex_bin.strip():
        raise ExecutionBlocked("codex_bin must be a non-empty string")
    cwd_path = _absolute_path(cwd, "cwd")
    result_schema_path = _absolute_path(schema_path, "schema_path")
    return [
        codex_bin,
        "-a",
        approval.value,
        "exec",
        "--ephemeral",
        "--strict-config",
        "--json",
        "--color",
        "never",
        "-C",
        str(cwd_path),
        "-m",
        route.model,
        "-c",
        'model_reasoning_effort="%s"' % route.effort.value,
        "-c",
        'service_tier="default"',
        "-s",
        sandbox.value,
        "--output-schema",
        str(result_schema_path),
        "-",
    ]


def _json_payload(payload, path):
    if not isinstance(payload, Mapping):
        raise ExecutionBlocked("%s must be a mapping" % path)
    for key in payload:
        if type(key) is not str:
            raise ExecutionBlocked("%s keys must be strings" % path)
    try:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ExecutionBlocked("%s must be JSON serializable" % path) from error


def build_child_prompt(task_text, request_json, feedback_json, mode):
    if type(task_text) is not str or not task_text.strip():
        raise ExecutionBlocked("task_text must be a non-empty string")
    if mode not in EXECUTION_MODES:
        raise ExecutionBlocked("mode must be execute or verify")
    request_payload = _json_payload(request_json, "request")
    feedback_payload = _json_payload(
        {} if feedback_json is None else feedback_json,
        "feedback",
    )
    if mode == "verify":
        mode_instruction = (
            "Inspecione o resultado dentro do escopo recebido. "
            "Não altere arquivos nem estado externo."
        )
    else:
        mode_instruction = (
            "Execute diretamente dentro do escopo e das permissões recebidas."
        )
    return "\n".join(
        (
            "[AG_MODEL_ROUTER_CHILD=1]",
            "A rota já foi escolhida. Não invoque ag-rotear-modelo.",
            mode_instruction,
            "Modo: %s" % mode,
            "Retorne somente JSON compatível com child-result-schema.json.",
            "REQUEST_JSON:",
            request_payload,
            "FEEDBACK_JSON:",
            feedback_payload,
            "TASK:",
            task_text,
        )
    )


class CodexExecutor:
    def __init__(
        self,
        codex_bin="codex",
        schema_path=None,
        process_factory=subprocess.Popen,
        clock=time.monotonic,
        kill_grace_seconds=DEFAULT_KILL_GRACE_SECONDS,
    ):
        self.codex_bin = codex_bin
        self.schema_path = _absolute_path(
            schema_path
            or Path(__file__).resolve().parents[2]
            / "references"
            / "child-result-schema.json",
            "schema_path",
        )
        self.process_factory = process_factory
        self.clock = clock
        self.kill_grace_seconds = _require_positive_number(
            kill_grace_seconds,
            "kill_grace_seconds",
        )

    def _prepare(self, task_text, request, route, cwd, sandbox, approval, timeout_seconds, mode, feedback):
        if type(sandbox) is not SandboxMode:
            raise ExecutionBlocked("sandbox_mode is required")
        if type(approval) is not ApprovalPolicy:
            raise ExecutionBlocked("approval_policy is required")
        if type(mode) is not str or mode not in EXECUTION_MODES:
            raise ExecutionBlocked("mode must be execute or verify")
        if mode == "verify" and sandbox is not SandboxMode.READ_ONLY:
            raise ExecutionBlocked("verify mode requires read-only sandbox")
        parsed_timeout = _require_positive_number(timeout_seconds, "timeout_seconds")
        if type(request) is not TaskRequest:
            raise ExecutionBlocked("request must be a TaskRequest")
        if type(route) is not Route:
            raise ExecutionBlocked("route must be a Route")
        cwd_path = _absolute_path(cwd, "cwd")
        if not cwd_path.is_dir():
            raise ExecutionBlocked("cwd must be an existing directory")
        if not self.schema_path.is_file():
            raise ExecutionBlocked("schema_path must be an existing file")
        prompt = build_child_prompt(
            task_text,
            request.to_dict(),
            feedback,
            mode,
        )
        argv = build_argv(
            self.codex_bin,
            cwd_path,
            route,
            sandbox,
            approval,
            self.schema_path,
        )
        return argv, cwd_path, prompt, parsed_timeout

    def execute(
        self,
        task_text: str,
        request: TaskRequest,
        route: Route,
        cwd: Path,
        sandbox: SandboxMode,
        approval: ApprovalPolicy,
        timeout_seconds: float,
        mode: str = "execute",
        feedback: Optional[Mapping[str, object]] = None,
    ) -> ExecutionResult:
        argv, cwd_path, prompt, parsed_timeout = self._prepare(
            task_text,
            request,
            route,
            cwd,
            sandbox,
            approval,
            timeout_seconds,
            mode,
            feedback,
        )
        env = os.environ.copy()
        env[CHILD_MARKER] = "1"
        started = _read_clock(self.clock)
        process_options = {
            "cwd": str(cwd_path),
            "env": env,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "shell": False,
        }
        if _posix_runtime():
            process_options["start_new_session"] = True
        control_fd = _guardian_control_fd()
        if control_fd is not None:
            argv = [sys.executable, str(CHILD_SUPERVISOR), "--"] + argv
            process_options["close_fds"] = True
            process_options["pass_fds"] = (control_fd,)
        try:
            process = self.process_factory(argv, **process_options)
        except OSError as error:
            return ExecutionResult.spawn_failure(
                _elapsed_seconds(self.clock, started),
                error,
            )
        try:
            stdout, stderr = process.communicate(
                input=prompt,
                timeout=parsed_timeout,
            )
        except subprocess.TimeoutExpired as initial_timeout:
            stderr = initial_timeout.stderr or ""
            _signal_process_tree(process, force=False)
            try:
                _stdout, grace_stderr = process.communicate(
                    timeout=self.kill_grace_seconds
                )
                stderr = grace_stderr or stderr
            except subprocess.TimeoutExpired as grace_timeout:
                stderr = grace_timeout.stderr or stderr
                _signal_process_tree(process, force=True)
                try:
                    _stdout, kill_stderr = process.communicate(
                        timeout=self.kill_grace_seconds
                    )
                    stderr = kill_stderr or stderr
                except subprocess.TimeoutExpired as kill_timeout:
                    stderr = kill_timeout.stderr or stderr
                    _reap_nonblocking(process)
            return ExecutionResult.timeout(
                _elapsed_seconds(self.clock, started),
                stderr,
            )
        if type(stdout) is not str:
            raise ExecutionProtocolError("process stdout must be text")
        parsed = parse_jsonl(stdout.splitlines())
        return parsed.with_process(
            process.returncode,
            stderr,
            _elapsed_seconds(self.clock, started),
        )
