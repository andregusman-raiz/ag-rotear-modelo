import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from platform_support import WINDOWS


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

    @unittest.skipUnless(WINDOWS, "Windows runtime integration only")
    def test_windows_runtime_bootstrap_round_trip_real(self):
        from model_router.state import RuntimeState
        from support import complete_decision_payload

        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeState(
                Path(tmp) / "runtime",
                SCRIPTS.parent / "references",
            )
            runtime.bootstrap()
            runtime.write_decision("windows-real", complete_decision_payload())

            decision = runtime.read_decision("windows-real")

        self.assertEqual("1.0.0", decision["schema_version"])

    @unittest.skipUnless(WINDOWS, "Windows lock integration only")
    def test_windows_portable_lock_contends_across_processes(self):
        from model_router.portable_flock import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "portable.lock"
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                script = """
import errno
import os
import sys
sys.path.insert(0, %r)
from model_router.portable_flock import fcntl
descriptor = os.open(%r, os.O_RDWR)
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError as error:
    raise SystemExit(0 if error.errno == errno.EAGAIN else 2)
raise SystemExit(3)
""" % (str(SCRIPTS), str(lock_path))
                completed = subprocess.run(
                    [sys.executable, "-c", script],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_windows_replace_uses_write_through_move(self):
        import model_router.portable_fs as portable_fs

        calls = []

        def record_move(source, destination, flags):
            calls.append((source, destination, flags))
            return 1

        with mock.patch.object(
            portable_fs,
            "_windows_runtime",
            return_value=True,
        ), mock.patch.object(
            portable_fs,
            "_load_move_file_ex",
            return_value=record_move,
        ):
            portable_fs.replace_file(
                Path("C:/runtime/decision.new"),
                Path("C:/runtime/decision.json"),
                replace=True,
            )
            portable_fs.replace_file(
                Path("C:/runtime/key.new"),
                Path("C:/runtime/decision.key"),
                replace=False,
            )

        self.assertEqual(
            [
                (
                    "C:/runtime/decision.new",
                    "C:/runtime/decision.json",
                    portable_fs.MOVEFILE_REPLACE_EXISTING
                    | portable_fs.MOVEFILE_WRITE_THROUGH,
                ),
                (
                    "C:/runtime/key.new",
                    "C:/runtime/decision.key",
                    portable_fs.MOVEFILE_WRITE_THROUGH,
                ),
            ],
            calls,
        )

    def test_private_directory_applies_windows_dacl(self):
        import model_router.portable_acl as portable_acl

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "private"
            directory.mkdir()
            with mock.patch.object(
                portable_acl,
                "_windows_runtime",
                return_value=True,
            ), mock.patch.object(
                portable_acl,
                "_set_windows_private_dacl",
            ) as restrict:
                portable_acl.ensure_private_directory(directory)

        restrict.assert_called_once_with(directory)

    def test_private_descriptor_applies_windows_dacl(self):
        import model_router.portable_acl as portable_acl

        descriptor, path = tempfile.mkstemp()
        try:
            with mock.patch.object(
                portable_acl,
                "_windows_runtime",
                return_value=True,
            ), mock.patch.object(
                portable_acl,
                "_set_windows_private_handle",
            ) as restrict:
                portable_acl.ensure_private_descriptor(descriptor, 0o600)
        finally:
            os.close(descriptor)
            Path(path).unlink()

        restrict.assert_called_once_with(descriptor)

    def test_windows_request_transport_uses_private_path_and_cleans_it(self):
        module = self._guardian_module()
        payload = b'{"request":"snapshot"}\n'
        with tempfile.TemporaryDirectory() as tmp:
            private_temp_root = Path(tmp)
            with mock.patch.object(module, "_posix_runtime", return_value=False):
                transport = module._request_transport(payload, private_temp_root)
            try:
                self.assertEqual(["--request"], transport.argv[:1])
                self.assertEqual((), transport.pass_fds)
                request_path = Path(transport.argv[1])
                self.assertEqual(private_temp_root, request_path.parent)
                self.assertEqual(payload, request_path.read_bytes())
                self.assertFalse(request_path.is_symlink())
            finally:
                transport.close()
            self.assertFalse(request_path.exists())

    def test_guardian_uses_explicit_private_temp_root_outside_workspace(self):
        module = self._guardian_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "workspace"
            workdir.mkdir()
            private_temp_root = root / "private-temp"
            private_temp_root.mkdir()

            input_dir, identity = module._create_input_directory_path(
                workdir,
                private_temp_root,
            )
            try:
                self.assertEqual(private_temp_root, input_dir.parent)
                self.assertEqual(identity, module._directory_identity(input_dir.stat()))
            finally:
                input_dir.rmdir()

    def test_guardian_default_temp_uses_dedicated_child(self):
        module = self._guardian_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "workspace"
            system_temp = root / "system-temp"
            workdir.mkdir()
            system_temp.mkdir(mode=0o755)
            original_mode = system_temp.stat().st_mode
            with mock.patch.object(
                module.tempfile,
                "gettempdir",
                return_value=str(system_temp),
            ):
                private_temp = module._resolve_private_temp_root(workdir, None)

            self.assertEqual(system_temp, private_temp.parent)
            self.assertNotEqual(system_temp, private_temp)
            self.assertEqual(original_mode, system_temp.stat().st_mode)
            self.assertTrue(private_temp.is_dir())

    def test_guardian_parser_reads_private_temp_root_from_environment(self):
        module = self._guardian_module()
        configured = Path("C:/ag-model-router-private")
        with mock.patch.dict(
            module.os.environ,
            {"AG_MODEL_ROUTER_PRIVATE_TEMP_ROOT": str(configured)},
            clear=True,
        ):
            args = module.build_parser().parse_args(
                [
                    "--workdir",
                    "C:/workspace",
                    "--sandbox",
                    "read-only",
                    "--approval-policy",
                    "never",
                ]
            )

        self.assertEqual(configured, args.private_temp_root)

    def test_windows_cleanup_preserves_replacement_directory(self):
        module = self._guardian_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            identity = module._directory_identity(input_dir.stat())
            moved = root / "moved-original"
            input_dir.rename(moved)
            input_dir.mkdir()
            sentinel = input_dir / "sentinel.txt"
            sentinel.write_text("preserve", encoding="utf-8")

            cleaned = module._scrub_bound_directory_path(input_dir, identity)

            self.assertFalse(cleaned)
            self.assertEqual("preserve", sentinel.read_text(encoding="utf-8"))

    def test_windows_runtime_state_avoids_posix_descriptor_apis(self):
        import model_router.portable_acl as portable_acl
        import model_router.registry as registry_module
        import model_router.state as state_module
        from model_router.state import RuntimeState
        from support import complete_decision_payload

        real_open = os.open
        real_link = os.link
        real_listdir = os.listdir
        real_unlink = os.unlink

        def reject_dirfd_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is not None:
                raise AssertionError("Windows path used dir_fd")
            return real_open(path, flags, mode)

        def reject_dirfd_link(
            source,
            destination,
            *,
            src_dir_fd=None,
            dst_dir_fd=None,
            follow_symlinks=True,
        ):
            if src_dir_fd is not None or dst_dir_fd is not None:
                raise AssertionError("Windows path used link dir_fd")
            return real_link(
                source,
                destination,
                follow_symlinks=follow_symlinks,
            )

        def reject_descriptor_listdir(path="."):
            if isinstance(path, int):
                raise AssertionError("Windows path listed a directory descriptor")
            return real_listdir(path)

        def reject_dirfd_unlink(path, *, dir_fd=None):
            if dir_fd is not None:
                raise AssertionError("Windows path used unlink dir_fd")
            return real_unlink(path)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            state_module,
            "_posix_runtime",
            return_value=False,
        ), mock.patch.object(
            registry_module,
            "_posix_runtime",
            return_value=False,
        ), mock.patch.object(
            portable_acl,
            "_windows_runtime",
            return_value=True,
        ), mock.patch.object(
            portable_acl,
            "_set_windows_private_handle",
        ), mock.patch.object(
            portable_acl,
            "_set_windows_private_dacl",
        ), mock.patch.object(
            state_module.os,
            "fchmod",
            side_effect=AssertionError("Windows path used fchmod"),
        ), mock.patch.object(
            state_module.os,
            "open",
            side_effect=reject_dirfd_open,
        ), mock.patch.object(
            state_module.os,
            "link",
            side_effect=reject_dirfd_link,
        ), mock.patch.object(
            state_module.os,
            "listdir",
            side_effect=reject_descriptor_listdir,
        ), mock.patch.object(
            state_module.os,
            "unlink",
            side_effect=reject_dirfd_unlink,
        ):
            runtime = RuntimeState(Path(tmp) / "runtime", SCRIPTS.parent / "references")
            runtime.bootstrap()
            runtime.write_decision("windows-path", complete_decision_payload())
            decision = runtime.read_decision("windows-path")

        self.assertEqual("1.0.0", decision["schema_version"])

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

    def test_windows_publish_ready_uses_durable_replace(self):
        module = self._guardian_module()
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp)
            (input_dir / "request.json").write_bytes(b"{}\n")
            (input_dir / "task.txt").write_bytes(b"task\n")
            real_replace = os.replace
            calls = []

            def record_replace(source, destination):
                calls.append((Path(source), Path(destination)))
                real_replace(source, destination)

            with mock.patch.object(
                module,
                "_posix_runtime",
                return_value=False,
            ), mock.patch.object(
                module,
                "replace_file",
                side_effect=record_replace,
            ):
                module.publish_ready(input_dir, "b" * 64)

        self.assertEqual(
            [(input_dir / ".READY.new", input_dir / "READY")],
            calls,
        )

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
