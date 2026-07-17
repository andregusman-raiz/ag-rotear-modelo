import ast
import hashlib
import importlib.util
import json
import os
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
GUARDIAN = SCRIPTS / "guarded-run.py"
PUBLISHER = SCRIPTS / "publish-ready.py"
CHILD_SUPERVISOR = SCRIPTS / "codex-child-supervisor.py"
MAX_INPUT_BYTES = 1024 * 1024


def _canonical_json(payload):
    return (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def _manifest_mutation(payload, path, value):
    mutated = json.loads(json.dumps(payload))
    target = mutated
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return _canonical_json(mutated)


def _manifest_without(payload, path):
    mutated = json.loads(json.dumps(payload))
    target = mutated
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]
    return _canonical_json(mutated)


class GuardedRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workdir = self.root / "workdir"
        self.workdir.mkdir()
        self.temp_root = self.root / "guardian-tmp"
        self.temp_root.mkdir()
        self.capture = self.root / "capture"
        self.capture.mkdir()

    def _launcher(self, exit_code=0):
        launcher = self.root / ("launcher-%d.sh" % exit_code)
        stdin_path = self.capture / "stdin.bin"
        argv_path = self.capture / "argv.txt"
        env_path = self.capture / "env.txt"
        eof_path = self.capture / "eof.txt"
        launcher.write_text(
            "#!/bin/sh\n"
            "cat > %s\n" % shlex.quote(str(stdin_path))
            + "printf '%%s\\n' \"$@\" > %s\n" % shlex.quote(str(argv_path))
            + "env > %s\n" % shlex.quote(str(env_path))
            + "printf 'eof\\n' > %s\n" % shlex.quote(str(eof_path))
            + "printf 'child-out\\n'\n"
            + "printf 'child-err\\n' >&2\n"
            + "exit %d\n" % exit_code,
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        return launcher

    def _blocking_launcher(self):
        launcher = self.root / "blocking-launcher.py"
        term_marker = self.capture / "child-term.txt"
        launcher.write_text(
            "#!%s\n" % sys.executable
            + "import signal\n"
            + "import sys\n"
            + "\n"
            + "def handle_term(_signum, _frame):\n"
            + "    with open(%r, 'w', encoding='utf-8') as marker:\n"
            % str(term_marker)
            + "        marker.write('term\\n')\n"
            + "    raise SystemExit(0)\n"
            + "\n"
            + "signal.signal(signal.SIGTERM, handle_term)\n"
            + "print('child-ready', flush=True)\n"
            + "while True:\n"
            + "    signal.pause()\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        return launcher

    def _detached_executor_launcher(self):
        launcher = self.root / "detached-executor-launcher.py"
        grandchild = self.root / "detached-codex.py"
        pid_marker = self.capture / "detached-codex.pid"
        term_marker = self.capture / "detached-codex-term.txt"
        grandchild.write_text(
            "#!%s\n" % sys.executable
            + "import os\n"
            + "import pathlib\n"
            + "import signal\n"
            + "\n"
            + "pathlib.Path(%r).write_text(str(os.getpid()), encoding='utf-8')\n"
            % str(pid_marker)
            + "def handle_term(_signum, _frame):\n"
            + "    pathlib.Path(%r).write_text('term\\n', encoding='utf-8')\n"
            % str(term_marker)
            + "    raise SystemExit(0)\n"
            + "signal.signal(signal.SIGTERM, handle_term)\n"
            + "print('detached-ready', flush=True)\n"
            + "while True:\n"
            + "    signal.pause()\n",
            encoding="utf-8",
        )
        grandchild.chmod(0o700)
        launcher.write_text(
            "#!%s\n" % sys.executable
            + "import os\n"
            + "import subprocess\n"
            + "import sys\n"
            + "control_fd = int(os.environ['AG_MODEL_ROUTER_CONTROL_FD'])\n"
            + "process = subprocess.Popen(\n"
            + "    [%r, %r, '--', %r, %r],\n"
            % (
                sys.executable,
                str(CHILD_SUPERVISOR),
                sys.executable,
                str(grandchild),
            )
            + "    pass_fds=(control_fd,),\n"
            + "    start_new_session=True,\n"
            + ")\n"
            + "raise SystemExit(process.wait())\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        return launcher, pid_marker, term_marker

    def _sigterm_resistant_detached_executor_launcher(self):
        launcher = self.root / "resistant-detached-executor-launcher.py"
        grandchild = self.root / "resistant-detached-codex.py"
        pid_marker = self.capture / "resistant-detached-codex.pid"
        grandchild.write_text(
            "#!%s\n" % sys.executable
            + "import os\n"
            + "import pathlib\n"
            + "import signal\n"
            + "\n"
            + "pathlib.Path(%r).write_text(str(os.getpid()), encoding='utf-8')\n"
            % str(pid_marker)
            + "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            + "print('resistant-detached-ready', flush=True)\n"
            + "while True:\n"
            + "    signal.pause()\n",
            encoding="utf-8",
        )
        grandchild.chmod(0o700)
        launcher.write_text(
            "#!%s\n" % sys.executable
            + "import os\n"
            + "import subprocess\n"
            + "import sys\n"
            + "control_fd = int(os.environ['AG_MODEL_ROUTER_CONTROL_FD'])\n"
            + "process = subprocess.Popen(\n"
            + "    [%r, %r, '--', %r, %r],\n"
            % (
                sys.executable,
                str(CHILD_SUPERVISOR),
                sys.executable,
                str(grandchild),
            )
            + "    pass_fds=(control_fd,),\n"
            + "    start_new_session=True,\n"
            + ")\n"
            + "raise SystemExit(process.wait())\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        return launcher, pid_marker

    def _snapshot_gate_launcher(self, case):
        launcher = self.root / ("snapshot-%s.py" % case)
        capture = self.capture / ("snapshot-%s.bin" % case)
        stdin_mode = self.capture / ("snapshot-%s-mode.txt" % case)
        release = self.capture / ("snapshot-%s-release" % case)
        launcher.write_text(
            "#!%s\n" % sys.executable
            + "import os\n"
            + "import pathlib\n"
            + "import sys\n"
            + "import time\n"
            + "pathlib.Path(%r).write_text(str(os.fstat(0).st_mode), encoding='utf-8')\n"
            % str(stdin_mode)
            + "print('child-start', flush=True)\n"
            + "release = pathlib.Path(%r)\n" % str(release)
            + "while not release.exists():\n"
            + "    time.sleep(0.01)\n"
            + "pathlib.Path(%r).write_bytes(sys.stdin.buffer.read())\n" % str(capture),
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        return launcher, capture, stdin_mode, release

    def _request_fd_launcher(self, case):
        launcher = self.root / ("request-fd-%s.py" % case)
        request_capture = self.capture / ("request-%s.bin" % case)
        task_capture = self.capture / ("request-%s-task.bin" % case)
        argv_capture = self.capture / ("request-%s-argv.txt" % case)
        release = self.capture / ("request-%s-release" % case)
        launcher.write_text(
            "#!%s\n" % sys.executable
            + "import os\n"
            + "import pathlib\n"
            + "import sys\n"
            + "import time\n"
            + "args = sys.argv[1:]\n"
            + "pathlib.Path(%r).write_text('\\n'.join(args), encoding='utf-8')\n"
            % str(argv_capture)
            + "print('child-start', flush=True)\n"
            + "release = pathlib.Path(%r)\n" % str(release)
            + "while not release.exists():\n"
            + "    time.sleep(0.01)\n"
            + "if '--request-fd' in args:\n"
            + "    request_fd = int(args[args.index('--request-fd') + 1])\n"
            + "    os.lseek(request_fd, 0, os.SEEK_SET)\n"
            + "    with os.fdopen(os.dup(request_fd), 'rb') as source:\n"
            + "        request = source.read()\n"
            + "elif '--request' in args:\n"
            + "    request = pathlib.Path(args[args.index('--request') + 1]).read_bytes()\n"
            + "else:\n"
            + "    raise SystemExit(91)\n"
            + "pathlib.Path(%r).write_bytes(request)\n" % str(request_capture)
            + "pathlib.Path(%r).write_bytes(sys.stdin.buffer.read())\n" % str(task_capture),
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        return launcher, request_capture, task_capture, argv_capture, release

    @staticmethod
    def _manifest_bytes(request, task, nonce):
        payload = {
            "nonce": nonce,
            "protocol": "ag-model-router-ready/v1",
            "request": {
                "sha256": hashlib.sha256(request).hexdigest(),
                "size": len(request),
            },
            "task": {
                "sha256": hashlib.sha256(task).hexdigest(),
                "size": len(task),
            },
        }
        return _canonical_json(payload)

    @classmethod
    def _write_manifest_inputs(cls, input_dir, nonce, request, task):
        request_path = input_dir / "request.json"
        task_path = input_dir / "task.txt"
        ready_path = input_dir / "READY"
        request_path.write_bytes(request)
        task_path.write_bytes(task)
        request_path.chmod(0o600)
        task_path.chmod(0o600)
        ready_path.write_bytes(cls._manifest_bytes(request, task, nonce))
        ready_path.chmod(0o600)

    def _start(self, *, exit_code=0, prepare_timeout=2.0, launcher=None):
        process = subprocess.Popen(
            [
                sys.executable,
                str(GUARDIAN),
                "--launcher",
                str(launcher or self._launcher(exit_code)),
                "--workdir",
                str(self.workdir),
                "--sandbox",
                "read-only",
                "--approval-policy",
                "never",
                "--prepare-timeout",
                str(prepare_timeout),
            ],
            cwd=str(self.workdir),
            env={**os.environ, "TMPDIR": str(self.temp_root)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.addCleanup(self._stop_if_running, process)
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=3), "guardian did not announce input")
            line = process.stdout.readline()
        finally:
            selector.close()
        if not line:
            self.fail(process.stderr.read())
        event = json.loads(line)
        self.assertEqual("input-ready", event["event"])
        self.assertRegex(event["ready_nonce"], r"^[0-9a-f]{64}$")
        input_dir = Path(event["input_dir"])
        self.assertEqual(0o700, stat.S_IMODE(input_dir.stat().st_mode))
        self.assertFalse(input_dir.is_relative_to(self.workdir))
        return process, event, input_dir

    def _read_stdout_line(self, process):
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=3), "child did not announce readiness")
            return process.stdout.readline()
        finally:
            selector.close()

    @staticmethod
    def _stop_if_running(process):
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    @classmethod
    def _write_inputs(cls, input_dir, nonce, task=b"task\n", request=b"{}\n"):
        cls._write_manifest_inputs(input_dir, nonce, request, task)

    def test_success_streams_exact_stdin_eof_and_always_cleans(self):
        task = b"unique-task-secret\n$(touch SHOULD_NOT_EXIST)\nlast-line"
        process, event, input_dir = self._start()
        self.assertEqual(str(input_dir / "request.json"), event["request_path"])
        self.assertEqual(str(input_dir / "task.txt"), event["task_path"])
        self.assertEqual(str(input_dir / "READY"), event["ready_path"])
        self._write_inputs(input_dir, event["ready_nonce"], task=task)

        stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(0, process.returncode, stderr)
        self.assertEqual(task, (self.capture / "stdin.bin").read_bytes())
        self.assertEqual("eof\n", (self.capture / "eof.txt").read_text())
        argv = (self.capture / "argv.txt").read_text()
        environment = (self.capture / "env.txt").read_text()
        self.assertNotIn("unique-task-secret", argv)
        self.assertNotIn("unique-task-secret", environment)
        self.assertNotIn("unique-task-secret", stdout)
        self.assertNotIn("unique-task-secret", stderr)
        self.assertIn("child-out", stdout)
        self.assertIn("child-err", stderr)
        self.assertFalse((self.workdir / "SHOULD_NOT_EXIST").exists())
        self.assertFalse(input_dir.exists())

    def test_publisher_commits_inputs_and_guardian_accepts_manifest(self):
        task = b"publisher task\n"
        request = b"{}\n"
        process, event, input_dir = self._start()
        request_path = input_dir / "request.json"
        task_path = input_dir / "task.txt"
        request_path.write_bytes(request)
        task_path.write_bytes(task)
        request_path.chmod(0o600)
        task_path.chmod(0o600)

        published = subprocess.run(
            [
                sys.executable,
                str(PUBLISHER),
                "--input-dir",
                str(input_dir),
                "--nonce",
                event["ready_nonce"],
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(0, published.returncode, published.stderr)
        self.assertEqual(0, process.returncode, stderr)
        self.assertEqual(task, (self.capture / "stdin.bin").read_bytes())
        self.assertNotIn(task.decode("utf-8").strip(), stdout)
        self.assertFalse(input_dir.exists())

    def test_publisher_rolls_back_ready_if_link_reports_failure_after_creation(self):
        spec = importlib.util.spec_from_file_location("guarded_run", GUARDIAN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        input_dir = self.root / "publisher-link-fault"
        input_dir.mkdir(mode=0o700)
        request = b"{}\n"
        task = b"task\n"
        (input_dir / "request.json").write_bytes(request)
        (input_dir / "task.txt").write_bytes(task)
        (input_dir / "request.json").chmod(0o600)
        (input_dir / "task.txt").chmod(0o600)
        real_link = module.os.link

        def link_then_raise(*args, **kwargs):
            real_link(*args, **kwargs)
            raise OSError("fault after link")

        with mock.patch.object(module.os, "link", side_effect=link_then_raise):
            with self.assertRaises(module.GuardianError):
                module.publish_ready(input_dir, "d" * 64)

        self.assertEqual(
            {"request.json", "task.txt"},
            {entry.name for entry in input_dir.iterdir()},
        )

    def test_publisher_has_no_reportable_failure_after_visibility_commit(self):
        spec = importlib.util.spec_from_file_location("guarded_run", GUARDIAN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for case in ("late-fsync", "late-close"):
            with self.subTest(case=case):
                input_dir = self.root / ("publisher-" + case)
                input_dir.mkdir(mode=0o700)
                (input_dir / "request.json").write_bytes(b"{}\n")
                (input_dir / "task.txt").write_bytes(b"task\n")
                (input_dir / "request.json").chmod(0o600)
                (input_dir / "task.txt").chmod(0o600)
                if case == "late-fsync":
                    real_fsync = module.os.fsync
                    calls = 0

                    def fail_third_fsync(descriptor):
                        nonlocal calls
                        calls += 1
                        if calls == 3:
                            raise OSError("post-commit fsync fault")
                        return real_fsync(descriptor)

                    patcher = mock.patch.object(
                        module.os,
                        "fsync",
                        side_effect=fail_third_fsync,
                    )
                else:
                    real_close = module.os.close

                    def close_then_raise(descriptor):
                        real_close(descriptor)
                        if (input_dir / "READY").exists() and not (
                            input_dir / ".READY.new"
                        ).exists():
                            raise OSError("post-commit close fault")

                    patcher = mock.patch.object(
                        module.os,
                        "close",
                        side_effect=close_then_raise,
                    )
                with patcher:
                    module.publish_ready(input_dir, "e" * 64)

                self.assertTrue((input_dir / "READY").is_file())
                self.assertFalse((input_dir / ".READY.new").exists())

    def test_early_directory_rejection_removes_empty_mkdtemp(self):
        spec = importlib.util.spec_from_file_location("guarded_run", GUARDIAN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        nested_tmp = self.workdir / "tmp"
        nested_tmp.mkdir()
        with mock.patch.dict(os.environ, {"TMPDIR": str(nested_tmp)}):
            tempfile.tempdir = None
            try:
                with self.assertRaises(module.GuardianError):
                    module._create_input_directory(self.workdir)
            finally:
                tempfile.tempdir = None

        self.assertEqual([], list(nested_tmp.glob("ag-model-router.*")))

    @unittest.skipUnless(os.name == "posix", "POSIX pipe metadata only")
    def test_task_snapshot_is_immutable_after_ready_and_uses_pipe_stdin(self):
        original = b"immutable task snapshot\n" * 4096
        for case in ("append-over-limit", "same-size-rewrite"):
            with self.subTest(case=case):
                launcher, capture, stdin_mode, release = self._snapshot_gate_launcher(
                    case,
                )
                process, event, input_dir = self._start(launcher=launcher)
                request_path = input_dir / "request.json"
                task_path = input_dir / "task.txt"
                request_path.write_bytes(b"{}\n")
                task_path.write_bytes(original)
                request_path.chmod(0o600)
                task_path.chmod(0o600)
                writer = os.open(task_path, os.O_RDWR)
                try:
                    (input_dir / "READY").write_bytes(
                        self._manifest_bytes(b"{}\n", original, event["ready_nonce"])
                    )
                    (input_dir / "READY").chmod(0o600)
                    self.assertEqual("child-start\n", self._read_stdout_line(process))
                    if case == "append-over-limit":
                        os.lseek(writer, 0, os.SEEK_END)
                        remaining = b"x" * (MAX_INPUT_BYTES + 1)
                    else:
                        os.lseek(writer, 0, os.SEEK_SET)
                        remaining = b"z" * len(original)
                    while remaining:
                        written = os.write(writer, remaining)
                        self.assertGreater(written, 0)
                        remaining = remaining[written:]
                    release.write_text("release\n", encoding="utf-8")
                    process.communicate(timeout=5)
                finally:
                    os.close(writer)

                self.assertEqual(0, process.returncode)
                self.assertTrue(
                    stat.S_ISFIFO(int(stdin_mode.read_text(encoding="utf-8")))
                )
                self.assertEqual(original, capture.read_bytes())
                self.assertFalse(input_dir.exists())

    def test_task_mutation_during_snapshot_is_rejected_without_overread(self):
        spec = importlib.util.spec_from_file_location("guarded_run", GUARDIAN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(
            hasattr(module, "_snapshot_task_descriptor"),
            "task snapshot function is missing",
        )
        payload = b"a" * (128 * 1024)
        task_path = self.root / "mutable-task.txt"
        task_path.write_bytes(payload)
        task_path.chmod(0o600)
        descriptor = os.open(task_path, module._open_regular_flags())
        real_read = module.os.read
        reads = []
        mutated = False

        def mutate_after_first_chunk(opened, amount):
            nonlocal mutated
            offset = os.lseek(opened, 0, os.SEEK_CUR)
            chunk = real_read(opened, amount)
            if opened == descriptor:
                reads.append((offset, len(chunk), amount))
                if chunk and not mutated:
                    with task_path.open("r+b", buffering=0) as writer:
                        writer.seek(offset)
                        writer.write(b"z" * len(chunk))
                    mutated = True
            return chunk

        try:
            with mock.patch.object(
                module.os,
                "read",
                side_effect=mutate_after_first_chunk,
            ), self.assertRaises(module.GuardianError):
                module._snapshot_task_descriptor(descriptor)
        finally:
            os.close(descriptor)

        self.assertTrue(mutated)
        self.assertTrue(reads)
        self.assertTrue(
            all(
                offset + size <= MAX_INPUT_BYTES
                and offset + requested <= MAX_INPUT_BYTES
                for offset, size, requested in reads
            )
        )

    def test_ready_manifest_rejects_full_rewrite_after_ready_was_observed(self):
        spec = importlib.util.spec_from_file_location("guarded_run", GUARDIAN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(
            hasattr(module, "_parse_ready_manifest"),
            "strict READY manifest parser is missing",
        )
        request = b'{"request":"original"}\n'
        task = b"original task\n"
        nonce = "a" * 64
        for label in ("request", "task"):
            with self.subTest(label=label):
                input_dir = self.root / ("manifest-input-" + label)
                input_dir.mkdir(mode=0o700)
                self._write_manifest_inputs(input_dir, nonce, request, task)
                descriptor = os.open(input_dir, module._open_directory_flags())
                try:
                    self.assertTrue(module._ready_exists(descriptor))
                    target = input_dir / (label + (".json" if label == "request" else ".txt"))
                    target.write_bytes(b"x" * target.stat().st_size)
                    target.chmod(0o600)
                    with self.assertRaises(module.GuardianError):
                        module._validate_inputs(descriptor, nonce)
                finally:
                    os.close(descriptor)

    def test_ready_manifest_parser_is_strict_canonical_and_bounded(self):
        spec = importlib.util.spec_from_file_location("guarded_run", GUARDIAN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        nonce = "b" * 64
        request = b"{}\n"
        task = b"task\n"
        valid = self._manifest_bytes(request, task, nonce)
        self.assertEqual(
            len(request),
            module._parse_ready_manifest(valid, nonce)["request"]["size"],
        )
        decoded = json.loads(valid)
        mutations = {
            "wrong-nonce": self._manifest_bytes(request, task, "c" * 64),
            "noncanonical": json.dumps(decoded, indent=2).encode("ascii") + b"\n",
            "uppercase-digest": self._manifest_bytes(request, task, nonce).replace(
                hashlib.sha256(task).hexdigest().encode("ascii"),
                hashlib.sha256(task).hexdigest().upper().encode("ascii"),
            ),
            "bool-size": _manifest_mutation(decoded, ("task", "size"), True),
            "float-size": _manifest_mutation(decoded, ("task", "size"), 1.0),
            "string-size": _manifest_mutation(decoded, ("task", "size"), "5"),
            "extra-key": _manifest_mutation(decoded, ("extra",), "x"),
            "missing-key": _manifest_without(decoded, ("task", "sha256")),
            "duplicate-key": valid.replace(
                b'{"nonce":',
                b'{"nonce":"' + nonce.encode("ascii") + b'","nonce":',
                1,
            ),
            "too-large": b"{" + b"x" * module.MAX_READY_BYTES + b"}\n",
        }
        for case, document in mutations.items():
            with self.subTest(case=case), self.assertRaises(module.GuardianError):
                module._parse_ready_manifest(document, nonce)

    def test_request_snapshot_uses_anonymous_fd_after_child_start(self):
        request = b'{"request":"original"}\n'
        task = b"original task\n"
        launcher, request_capture, task_capture, argv_capture, release = (
            self._request_fd_launcher("rewrite")
        )
        process, event, input_dir = self._start(launcher=launcher)
        self._write_manifest_inputs(input_dir, event["ready_nonce"], request, task)
        self.assertEqual("child-start\n", self._read_stdout_line(process))
        input_dir.mkdir(mode=0o700)
        replacement = input_dir / "request.json"
        replacement.write_bytes(b"x" * len(request))
        replacement.chmod(0o600)
        release.write_text("release\n", encoding="utf-8")
        process.communicate(timeout=5)

        argv = argv_capture.read_text(encoding="utf-8")
        self.assertEqual(0, process.returncode)
        self.assertIn("--request-fd", argv)
        self.assertNotIn("--request\n", argv + "\n")
        self.assertEqual(request, request_capture.read_bytes())
        self.assertEqual(task, task_capture.read_bytes())
        self.assertEqual(b"x" * len(request), replacement.read_bytes())

    def test_directory_replacement_preserves_sentinel_and_scrubs_original(self):
        request = b'{"request":"bound-original"}\n'
        task = b"bound original task\n"
        launcher, request_capture, task_capture, argv_capture, _release = (
            self._request_fd_launcher("replacement")
        )
        process, event, input_dir = self._start(launcher=launcher)
        request_path = input_dir / "request.json"
        task_path = input_dir / "task.txt"
        request_path.write_bytes(request)
        task_path.write_bytes(task)
        request_path.chmod(0o600)
        task_path.chmod(0o600)
        moved = self.root / "moved-original"
        input_dir.rename(moved)
        input_dir.mkdir(mode=0o700)
        sentinel = input_dir / "replacement-sentinel"
        sentinel.write_bytes(b"preserve replacement")
        (moved / "READY").write_bytes(
            self._manifest_bytes(request, task, event["ready_nonce"])
        )
        (moved / "READY").chmod(0o600)

        process.communicate(timeout=5)

        self.assertEqual([], list(moved.iterdir()))
        self.assertEqual(125, process.returncode)
        self.assertEqual(b"preserve replacement", sentinel.read_bytes())
        self.assertEqual([], list(moved.iterdir()))
        self.assertFalse(request_capture.exists())
        self.assertFalse(task_capture.exists())
        self.assertFalse(argv_capture.exists())

    def test_child_failure_returns_its_status_and_cleans(self):
        process, event, input_dir = self._start(exit_code=7)
        self._write_inputs(input_dir, event["ready_nonce"])

        process.communicate(timeout=5)

        self.assertEqual(7, process.returncode)
        self.assertFalse(input_dir.exists())

    def test_signal_during_preparation_cleans_without_starting_child(self):
        handled = tuple(
            getattr(signal, name)
            for name in ("SIGTERM", "SIGINT", "SIGHUP")
            if hasattr(signal, name)
        )
        for signum in handled:
            with self.subTest(signum=signum):
                process, _event, input_dir = self._start(prepare_timeout=5)

                process.send_signal(signum)
                process.communicate(timeout=5)

                self.assertEqual(128 + signum, process.returncode)
                self.assertFalse(input_dir.exists())
                self.assertFalse((self.capture / "stdin.bin").exists())

    def test_event_pid_can_cancel_preparation_and_cleanup(self):
        process, event, input_dir = self._start(prepare_timeout=5)

        guardian_pid = event["guardian_pid"]
        self.assertIs(type(guardian_pid), int)
        self.assertGreater(guardian_pid, 0)
        self.assertEqual(process.pid, guardian_pid)
        os.kill(guardian_pid, signal.SIGTERM)
        process.communicate(timeout=5)

        self.assertEqual(128 + signal.SIGTERM, process.returncode)
        self.assertFalse(input_dir.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX process groups only")
    def test_sigterm_reaches_running_child_before_kill_fallback_and_cleans(self):
        process, event, input_dir = self._start(
            launcher=self._blocking_launcher(),
        )
        self._write_inputs(input_dir, event["ready_nonce"])
        self.assertEqual("child-ready\n", self._read_stdout_line(process))

        os.kill(event["guardian_pid"], signal.SIGTERM)
        process.communicate(timeout=5)

        self.assertEqual(128 + signal.SIGTERM, process.returncode)
        self.assertEqual(
            "term\n",
            (self.capture / "child-term.txt").read_text(encoding="utf-8"),
        )
        self.assertFalse(input_dir.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX process groups only")
    def test_sigterm_reaches_executor_session_and_no_detached_child_survives(self):
        launcher, pid_marker, term_marker = self._detached_executor_launcher()
        process, event, input_dir = self._start(launcher=launcher)
        self._write_inputs(input_dir, event["ready_nonce"])
        self.assertEqual("detached-ready\n", self._read_stdout_line(process))
        detached_pid = int(pid_marker.read_text(encoding="utf-8"))
        try:
            os.kill(event["guardian_pid"], signal.SIGTERM)
            process.communicate(timeout=8)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not term_marker.exists():
                time.sleep(0.02)

            self.assertEqual(128 + signal.SIGTERM, process.returncode)
            self.assertEqual("term\n", term_marker.read_text(encoding="utf-8"))
            self.assertFalse(input_dir.exists())
        finally:
            try:
                os.kill(detached_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @unittest.skipUnless(os.name == "posix", "POSIX process groups only")
    def test_sigkill_reaches_executor_group_after_supervisor_exits_first(self):
        launcher, pid_marker = self._sigterm_resistant_detached_executor_launcher()
        process, event, input_dir = self._start(launcher=launcher)
        self._write_inputs(input_dir, event["ready_nonce"])
        self.assertEqual(
            "resistant-detached-ready\n",
            self._read_stdout_line(process),
        )
        detached_pid = int(pid_marker.read_text(encoding="utf-8"))
        try:
            os.kill(event["guardian_pid"], signal.SIGTERM)
            process.communicate(timeout=8)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(detached_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            with self.assertRaises(ProcessLookupError):
                os.kill(detached_pid, 0)

            self.assertEqual(128 + signal.SIGTERM, process.returncode)
            self.assertFalse(input_dir.exists())
        finally:
            try:
                os.kill(detached_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_preparation_timeout_cleans_without_starting_child(self):
        process, _event, input_dir = self._start(prepare_timeout=0.1)

        process.communicate(timeout=5)

        self.assertEqual(124, process.returncode)
        self.assertFalse(input_dir.exists())
        self.assertFalse((self.capture / "stdin.bin").exists())

    def test_invalid_inputs_fail_closed_preserve_external_target_and_clean(self):
        cases = [
            "request-mode",
            "ready-mode",
            "task-symlink",
            "request-limit",
            "task-limit",
            "extra-entry",
            "missing-task",
        ]
        if hasattr(os, "mkfifo"):
            cases.append("request-fifo")
        for case in cases:
            with self.subTest(case=case):
                process, event, input_dir = self._start()
                external = self.root / (case + "-external")
                external.write_bytes(b"preserve")
                if case != "missing-task":
                    self._write_inputs(input_dir, event["ready_nonce"])
                else:
                    (input_dir / "request.json").write_bytes(b"{}\n")
                    (input_dir / "request.json").chmod(0o600)
                    (input_dir / "READY").write_bytes(b"ready\n")
                    (input_dir / "READY").chmod(0o600)
                if case == "request-mode":
                    (input_dir / "request.json").chmod(0o644)
                elif case == "ready-mode":
                    (input_dir / "READY").chmod(0o644)
                elif case == "task-symlink":
                    (input_dir / "task.txt").unlink()
                    (input_dir / "task.txt").symlink_to(external)
                elif case == "request-limit":
                    (input_dir / "request.json").write_bytes(
                        b"x" * (MAX_INPUT_BYTES + 1)
                    )
                elif case == "task-limit":
                    (input_dir / "task.txt").write_bytes(
                        b"x" * (MAX_INPUT_BYTES + 1)
                    )
                elif case == "extra-entry":
                    (input_dir / "extra").write_bytes(b"unexpected")
                    (input_dir / "extra").chmod(0o600)
                elif case == "request-fifo":
                    (input_dir / "request.json").unlink()
                    os.mkfifo(input_dir / "request.json", mode=0o600)

                process.communicate(timeout=5)

                self.assertNotEqual(0, process.returncode)
                self.assertFalse(input_dir.exists())
                self.assertEqual(b"preserve", external.read_bytes())
                self.assertFalse((self.capture / "stdin.bin").exists())

    def test_owner_validation_rejects_unexpected_uid(self):
        spec = importlib.util.spec_from_file_location("guarded_run", GUARDIAN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        metadata = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_uid=os.getuid(),
            st_size=1,
        )

        with mock.patch.object(module.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(module.GuardianError):
                module._validate_private_regular_metadata(
                    metadata,
                    "task.txt",
                    MAX_INPUT_BYTES,
                )

    def test_special_input_open_is_nonblocking_before_regular_file_validation(self):
        spec = importlib.util.spec_from_file_location("guarded_run", GUARDIAN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertTrue(module._open_regular_flags() & os.O_NONBLOCK)

    def test_script_is_python39_executable_and_never_uses_shell_transport(self):
        for script in (GUARDIAN, CHILD_SUPERVISOR):
            with self.subTest(script=script.name):
                source = script.read_text(encoding="utf-8")
                ast.parse(source, filename=str(script), feature_version=(3, 9))
                self.assertNotIn("shell=True", source)
                self.assertTrue(script.stat().st_mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
