#!/usr/bin/env python3
import argparse
import json
import math
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

from model_router.contracts import (
    ApprovalPolicy,
    SandboxMode,
    TaskRequest,
)
from model_router.escalation import BudgetLedger
from model_router.executor import CodexExecutor
from model_router.model_registry import discover_catalog
from model_router.profiles import load_profiles
from model_router.registry import promote_candidate
from model_router.service import RouterService
from model_router.state import RuntimeState
from model_router.validators import validate_child_report


SKILL_ROOT = Path(__file__).resolve().parent.parent
REFERENCES = SKILL_ROOT / "references"
MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_CATALOG_TIMEOUT_SECONDS = 10.0
MAX_CATALOG_TIMEOUT_SECONDS = 60.0


class CliError(ValueError):
    pass


class _DuplicateJsonKeyError(ValueError):
    pass


def _strict_object(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateJsonKeyError()
        payload[key] = value
    return payload


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _parse_request_document(document: bytes) -> TaskRequest:
    try:
        payload = json.loads(
            document.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except CliError:
        raise
    except (ValueError, UnicodeError):
        raise CliError("invalid request") from None
    try:
        return TaskRequest.from_dict(payload)
    except ValueError:
        raise CliError("invalid request") from None


def _read_request_descriptor(descriptor: int) -> bytes:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_REQUEST_BYTES
    ):
        raise CliError("invalid request")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    remaining = MAX_REQUEST_BYTES + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    document = b"".join(chunks)
    if len(document) > MAX_REQUEST_BYTES:
        raise CliError("invalid request")
    return document


def _load_request(path: str) -> TaskRequest:
    candidate = _absolute_path(path)
    descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(candidate), flags)
        return _parse_request_document(_read_request_descriptor(descriptor))
    except CliError:
        raise
    except OSError:
        raise CliError("invalid request") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _request_fd(value: str) -> int:
    try:
        descriptor = int(value, 10)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("request fd must be an integer") from None
    if descriptor < 3:
        raise argparse.ArgumentTypeError("request fd is outside the safe range")
    return descriptor


def _load_request_fd(descriptor: int) -> TaskRequest:
    duplicate = -1
    try:
        duplicate = os.dup(descriptor)
        metadata = os.fstat(duplicate)
        if metadata.st_nlink != 0:
            raise CliError("invalid request")
        return _parse_request_document(_read_request_descriptor(duplicate))
    except CliError:
        raise
    except OSError:
        raise CliError("invalid request") from None
    finally:
        if duplicate >= 0:
            os.close(duplicate)


def _absolute_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.absolute()


def _runtime_root(explicit: Optional[str]) -> Path:
    selected = explicit or os.environ.get("AG_MODEL_ROUTER_RUNTIME_ROOT")
    return (
        _absolute_path(selected)
        if selected
        else _absolute_path("~/.codex/model-router")
    )


def _models_cache(explicit: Optional[str]) -> Path:
    selected = explicit or os.environ.get("AG_MODEL_ROUTER_MODELS_CACHE")
    return (
        _absolute_path(selected)
        if selected
        else _absolute_path("~/.codex/models_cache.json")
    )


def _catalog_timeout_seconds() -> float:
    raw = os.environ.get("AG_MODEL_ROUTER_CATALOG_TIMEOUT_SECONDS")
    if raw is None:
        return DEFAULT_CATALOG_TIMEOUT_SECONDS
    try:
        parsed = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise CliError("invalid catalog timeout") from None
    if not math.isfinite(parsed) or not 0 < parsed <= MAX_CATALOG_TIMEOUT_SECONDS:
        raise CliError("invalid catalog timeout")
    return parsed


def _runner(argv, timeout_seconds: Optional[float] = None):
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=(
            _catalog_timeout_seconds()
            if timeout_seconds is None
            else timeout_seconds
        ),
    )


def _build_service(args):
    state = RuntimeState(_runtime_root(args.runtime_root), seed_root=REFERENCES)
    codex_bin = os.environ.get("AG_MODEL_ROUTER_CODEX_BIN", "codex")
    cache_path = _models_cache(getattr(args, "models_cache", None))
    catalog_timeout = _catalog_timeout_seconds()

    def catalog_runner(argv):
        return _runner(argv, catalog_timeout)

    def catalog_provider():
        return discover_catalog(
            codex_bin,
            cache_path,
            REFERENCES / "model-registry.json",
            catalog_runner,
        )

    service = RouterService(
        state=state,
        profiles=load_profiles(REFERENCES / "profiles"),
        catalog_provider=catalog_provider,
        executor=CodexExecutor(codex_bin=codex_bin),
        budget=BudgetLedger(),
        validator=validate_child_report,
    )
    return service, state


def _workdir(value: str) -> Path:
    path = Path(value)
    if not path.is_dir():
        raise CliError("workdir must be an existing directory")
    return path


def _print_audit(state: RuntimeState, decision_path: Path) -> None:
    payload = state.read_decision(decision_path.parent.name)
    print(
        json.dumps(
            {
                "selection": payload["selection"],
                "evidence_ids": payload["evidence_ids"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )


def _select(args) -> int:
    request = _load_request(args.request)
    service, state = _build_service(args)
    result = service.select(request, _workdir(args.workdir))
    if result.decision is None:
        print("Rota: bloqueada | tentativas: 0 | validação: blocked")
        if args.audit:
            _print_audit(state, result.decision_path)
        return 1
    route = result.decision.ideal
    print(
        "Rota: %s/%s | tentativas: 0 | validação: não-executado"
        % (route.model, route.effort.value)
    )
    if args.audit:
        _print_audit(state, result.decision_path)
    return 0


def _run(args, task_text: str) -> int:
    request_fd = getattr(args, "request_fd", None)
    request = (
        _load_request_fd(request_fd)
        if request_fd is not None
        else _load_request(args.request)
    )
    service, state = _build_service(args)
    result = service.run(
        task_text=task_text,
        request=request,
        workdir=_workdir(args.workdir),
        sandbox=SandboxMode(args.sandbox),
        approval=ApprovalPolicy(args.approval_policy),
    )
    if result.status == "pass" and result.final_content is not None:
        print(result.final_content)
    route = result.route
    if route is None:
        print(
            "Rota: bloqueada | tentativas: %d | validação: %s"
            % (result.attempt_count, result.status)
        )
    else:
        print(
            "Rota: %s/%s | tentativas: %d | validação: %s"
            % (route.model, route.effort.value, result.attempt_count, result.status)
        )
    if args.audit:
        _print_audit(state, result.decision_path)
    return 0 if result.status == "pass" else 1


def _audit(args) -> int:
    state = RuntimeState(_runtime_root(args.runtime_root), seed_root=REFERENCES)
    payload = state.read_decision(args.run_id)
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


def _promote(args) -> int:
    state = RuntimeState(_runtime_root(args.runtime_root), seed_root=REFERENCES)
    state.bootstrap()
    promote_candidate(Path(args.candidate), state.benchmark_registry_path)
    print("registry promoted")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="router.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    select = subparsers.add_parser("select")
    select.add_argument("--request", required=True)
    select.add_argument("--workdir", required=True)
    select.add_argument("--runtime-root")
    select.add_argument("--models-cache")
    select.add_argument("--audit", action="store_true")

    run = subparsers.add_parser("run")
    request_source = run.add_mutually_exclusive_group(required=True)
    request_source.add_argument("--request")
    request_source.add_argument(
        "--request-fd",
        type=_request_fd,
        help=argparse.SUPPRESS,
    )
    run.add_argument("--workdir", required=True)
    run.add_argument(
        "--sandbox",
        required=True,
        choices=("read-only", "workspace-write", "danger-full-access"),
    )
    run.add_argument(
        "--approval-policy",
        required=True,
        choices=("untrusted", "on-request", "never"),
    )
    run.add_argument("--runtime-root")
    run.add_argument("--models-cache")
    run.add_argument("--audit", action="store_true")

    audit = subparsers.add_parser("audit")
    audit.add_argument("--run-id", required=True)
    audit.add_argument("--runtime-root")

    promote = subparsers.add_parser("promote-registry")
    promote.add_argument("--candidate", required=True)
    promote.add_argument("--runtime-root")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    task_text = None
    if args.command == "run":
        task_text = sys.stdin.read()
        if not task_text.strip():
            print(
                "router error: run requires non-empty task text on stdin",
                file=sys.stderr,
            )
            return 2
    try:
        if args.command == "select":
            return _select(args)
        if args.command == "run":
            return _run(args, task_text)
        if args.command == "audit":
            return _audit(args)
        if args.command == "promote-registry":
            return _promote(args)
        raise CliError("unsupported command")
    except CliError as error:
        print("router error: %s" % error, file=sys.stderr)
        return 1
    except ValueError:
        print("router error: invalid input", file=sys.stderr)
        return 1
    except Exception:
        print("router error: operation failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
