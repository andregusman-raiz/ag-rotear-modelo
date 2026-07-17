import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
GUARDIAN = SCRIPTS / "guarded-run.py"
sys.path.insert(0, str(SCRIPTS))


class WindowsCompatibilityTests(unittest.TestCase):
    def _guardian_module(self):
        spec = importlib.util.spec_from_file_location("guarded_run", GUARDIAN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_runtime_modules_import_through_portable_lock_facade(self):
        import model_router.registry as registry_module
        import model_router.state as state_module
        from model_router.portable_flock import fcntl

        self.assertIs(state_module.fcntl, fcntl)
        self.assertIs(registry_module.fcntl, fcntl)
        self.assertTrue(callable(fcntl.flock))
        self.assertTrue(fcntl.LOCK_EX)
        self.assertTrue(fcntl.LOCK_UN)

    def test_windows_request_transport_uses_private_path_and_cleans_it(self):
        module = self._guardian_module()
        payload = b'{"request":"snapshot"}\n'
        with mock.patch.object(module, "_posix_runtime", return_value=False):
            transport = module._request_transport(payload)
        try:
            self.assertEqual(["--request"], transport.argv[:1])
            self.assertEqual((), transport.pass_fds)
            request_path = Path(transport.argv[1])
            self.assertEqual(payload, request_path.read_bytes())
            self.assertFalse(request_path.is_symlink())
        finally:
            transport.close()
        self.assertFalse(request_path.exists())

    def test_windows_publish_ready_uses_path_commit_without_dirfd(self):
        module = self._guardian_module()
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp)
            request = b"{}\n"
            task = b"task\n"
            (input_dir / "request.json").write_bytes(request)
            (input_dir / "task.txt").write_bytes(task)
            (input_dir / "request.json").chmod(0o600)
            (input_dir / "task.txt").chmod(0o600)
            with mock.patch.object(module, "_posix_runtime", return_value=False):
                module.publish_ready(input_dir, "a" * 64)

            ready = input_dir / "READY"
            self.assertTrue(ready.is_file())
            self.assertFalse((input_dir / ".READY.new").exists())
            manifest = json.loads(ready.read_text(encoding="ascii"))
            self.assertEqual("a" * 64, manifest["nonce"])
            self.assertEqual(len(request), manifest["request"]["size"])
            self.assertEqual(len(task), manifest["task"]["size"])

    def test_python_launcher_is_invoked_through_current_interpreter(self):
        module = self._guardian_module()
        args = type(
            "Args",
            (),
            {
                "launcher": SCRIPTS / "run-route.py",
                "workdir": Path("C:/work"),
                "sandbox": "read-only",
                "approval_policy": "never",
            },
        )()
        argv = module._child_argv(args, ["--request", "C:/tmp/request.json"])

        self.assertEqual(sys.executable, argv[0])
        self.assertEqual(str(SCRIPTS / "run-route.py"), argv[1])
        self.assertIn("--request", argv)
        self.assertNotIn("--request-fd", argv)

    def test_executor_windows_timeout_uses_taskkill_tree(self):
        from model_router.contracts import ApprovalPolicy, SandboxMode
        from model_router.executor import CodexExecutor
        from support import FakeProcessFactory, execution_args

        factory = FakeProcessFactory.hanging(pid=4321)
        calls = []

        def record_taskkill(argv, **_kwargs):
            calls.append(tuple(argv))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch(
            "model_router.executor._windows_runtime",
            return_value=True,
        ), mock.patch(
            "model_router.executor._posix_runtime",
            return_value=False,
        ), mock.patch(
            "model_router.executor.subprocess.run",
            side_effect=record_taskkill,
        ):
            result = CodexExecutor(
                process_factory=factory,
                kill_grace_seconds=0.01,
            ).execute(
                **execution_args(
                    sandbox=SandboxMode.READ_ONLY,
                    approval=ApprovalPolicy.NEVER,
                    timeout_seconds=0.01,
                )
            )

        self.assertEqual("timeout", result.failure_kind)
        self.assertIn(("taskkill", "/PID", "4321", "/T"), calls)
        self.assertIn(("taskkill", "/PID", "4321", "/T", "/F"), calls)


if __name__ == "__main__":
    unittest.main()
