#!/usr/bin/env python3
"""Register and supervise the real Codex child process for the guardian."""

import os
import signal
import subprocess
import sys


CONTROL_FD_ENV = "AG_MODEL_ROUTER_CONTROL_FD"
SPAWN_FAILURE_EXIT = 125


def _write_control(descriptor: int, marker: str, pid: int) -> None:
    os.write(descriptor, ("%s%d\n" % (marker, pid)).encode("ascii"))


def _exit_code(returncode: int) -> int:
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode


def _parse_args(argv):
    if len(argv) < 3 or argv[1] != "--":
        print("usage: codex-child-supervisor.py -- COMMAND...", file=sys.stderr)
        return None
    return argv[2:]


def main(argv=None) -> int:
    argv = sys.argv if argv is None else argv
    command = _parse_args(argv)
    if command is None:
        return 2
    try:
        control_fd = int(os.environ[CONTROL_FD_ENV])
    except (KeyError, TypeError, ValueError):
        return SPAWN_FAILURE_EXIT

    child_env = os.environ.copy()
    child_env.pop(CONTROL_FD_ENV, None)
    child = None
    try:
        supervisor_pid = os.getpid()
        _write_control(control_fd, "+", supervisor_pid)
        child = subprocess.Popen(
            command,
            env=child_env,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            shell=False,
            close_fds=True,
        )
        return _exit_code(child.wait())
    except OSError:
        return SPAWN_FAILURE_EXIT
    finally:
        try:
            _write_control(control_fd, "-", os.getpid())
        except OSError:
            pass
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signal.SIGTERM)
            except OSError:
                pass
        try:
            os.close(control_fd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
