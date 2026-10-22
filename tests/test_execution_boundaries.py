import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import common
import session
from clang_static_analyzer import scanner as csa


class ExecutionBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sast-execution-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "sample.c"
        self.source.write_text("int value(void) { return 0; }\n")
        self.entry = {"file": str(self.source), "directory": str(self.folder),
                      "arguments": ["clang", "-c", str(self.source)]}
        common.CANCELLED.clear()
        self.addCleanup(common.CANCELLED.clear)

    def arguments(self, *flags):
        return csa.compiler_arguments(dict(self.entry, arguments=["clang", *flags, "-c", str(self.source)]))

    def test_save_temps_removed_in_driver_and_forwarded_forms(self):
        for flag in ("-save-temps", "-save-temps=cwd", "-save-temps=obj", "--save-temps", "--save-temps=obj"):
            for flags in ((flag,), ("-Xclang", flag), ("-Xclang=" + flag,),
                          ("-Xpreprocessor", flag), ("-Wp," + flag,)):
                with self.subTest(flags=flags):
                    self.assertEqual(self.arguments(*flags, "-DFEATURE=1"), ["-DFEATURE=1"])

    def test_invalid_save_temps_mode_is_not_silently_accepted(self):
        with self.assertRaisesRegex(ValueError, "save-temps"):
            self.arguments("-save-temps=unknown")

    def test_external_clang_config_rejected_in_all_channels(self):
        for flags in (("--config=flags.cfg",), ("--config", "flags.cfg"),
                      ("--config-user-dir=config",), ("--config-system-dir", "config"),
                      ("-Xclang", "--config=flags.cfg"), ("-Xclang=--config=flags.cfg",),
                      ("-Xpreprocessor", "--config", "-Xpreprocessor", "flags.cfg"),
                      ("-Wp,--config=flags.cfg",)):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, "external Clang config"):
                self.arguments(*flags)
        self.assertEqual(self.arguments("-D", "--config=macro-value"), ["-D", "--config=macro-value"])

    def test_config_rejection_reaches_dependency_capture_and_scan_before_execution(self):
        entry = dict(self.entry, arguments=["clang", "--config=flags.cfg", "-c", str(self.source)])
        binaries = {"clang": "clang", "clang++": "clang++"}
        with patch.object(common, "execute", return_value={"exit_code": 1}) as execute:
            baseline = csa.capture_inputs([entry], self.folder / "before", binaries)
            self.assertFalse(baseline["ok"])
            self.assertIn("external Clang config", baseline["errors"][0])
            with self.assertRaisesRegex(ValueError, "external Clang config"):
                csa.scan([entry], [self.source], self.folder / "compile_commands.json", self.folder / "scan", binaries)
            execute.assert_not_called()

    def test_real_csa_save_temps_preserves_existing_files_and_analysis_context(self):
        compiler = shutil.which("clang")
        if compiler is None:
            self.skipTest("clang is unavailable")
        self.source.write_text("#ifndef FEATURE\n#error Missing analysis macro\n#endif\nint value(void) { return 0; }\n")
        for position, flag in enumerate(("-save-temps", "-save-temps=cwd", "-save-temps=obj")):
            with self.subTest(flag=flag):
                existing = self.folder / "sample.i"
                existing.write_bytes(b"OWNER_CONTENT_DO_NOT_REPLACE\n")
                before = {path.name for path in self.folder.iterdir()}
                output = self.folder / ("scan-" + str(position))
                entry = dict(self.entry, arguments=[compiler, flag, "-DFEATURE=1", "-c", str(self.source), "-o", "sample.o"])
                result = csa.scan([entry], [self.source], self.folder / "compile_commands.json", output,
                                  {"clang": compiler, "clang++": compiler})
                self.assertTrue(result["ok"], result)
                self.assertEqual(existing.read_bytes(), b"OWNER_CONTENT_DO_NOT_REPLACE\n")
                self.assertEqual({path.name for path in self.folder.iterdir()} - before, {output.name})
                self.assertEqual(list(output.rglob("*.i")), [])

    def test_sigterm_cleans_serial_scanner_and_records_cancellation(self):
        self.assert_signal_cleanup("cppcheck", "cpp_check", signal.SIGTERM, 143)

    def test_sigterm_cleans_threaded_scanner_and_records_cancellation(self):
        self.assert_signal_cleanup("clang-tidy", "clang_tidy", signal.SIGTERM, 143)

    def test_sigterm_cleans_direct_execute_child(self):
        child_pid = self.folder / "direct-child.pid"
        child_code = f"import os,time; from pathlib import Path; Path({str(child_pid)!r}).write_text(str(os.getpid())); time.sleep(60)"
        parent_code = (
            "import sys; from pathlib import Path; "
            f"sys.path.insert(0, {str(ROOT / 'scan')!r}); import common\n"
            "try:\n"
            f" common.execute([sys.executable, '-c', {child_code!r}], Path({str(self.folder)!r}), "
            f"Path({str(self.folder / 'stdout')!r}), Path({str(self.folder / 'stderr')!r}))\n"
            "except KeyboardInterrupt as error:\n raise SystemExit(common.cancellation_exit_code(error))\n"
        )
        parent = subprocess.Popen([sys.executable, "-c", parent_code], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not child_pid.exists() and time.monotonic() < deadline and parent.poll() is None:
                time.sleep(0.02)
            self.assertTrue(child_pid.exists(), "execute child never became active")
            parent.send_signal(signal.SIGTERM)
            self.assertEqual(parent.wait(timeout=10), 143)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(child_pid.read_text()), 0)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()
            if child_pid.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(int(child_pid.read_text()), signal.SIGKILL)

    def assert_signal_cleanup(self, executable, tool_name, signum, exit_code):
        binary_folder = self.folder / "bin"
        binary_folder.mkdir()
        child_pid = self.folder / "scanner.pid"
        tool = binary_folder / executable
        tool.write_text("#!" + sys.executable + "\nimport os,sys,time\nfrom pathlib import Path\n"
                        "if '--version' in sys.argv:\n print('fixture 1')\nelse:\n"
                        f" Path({str(child_pid)!r}).write_text(str(os.getpid()))\n time.sleep(60)\n")
        tool.chmod(0o755)
        common.write_json(self.folder / "compile_commands.json", [self.entry])
        output = self.folder / "results"
        environment = dict(os.environ, PATH=str(binary_folder) + os.pathsep + os.environ["PATH"], PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen([sys.executable, str(ROOT / "scan" / tool_name / "entry.py"),
                                    "--scan-path", str(self.source), "--result-path", str(output)], env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not child_pid.exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.02)
            self.assertTrue(child_pid.exists(), "scanner never became active")
            process.send_signal(signum)
            self.assertEqual(process.wait(timeout=10), exit_code)
            manifest = json.loads(next(output.glob("*/manifest.json")).read_text())
            self.assertTrue(manifest["cancelled"])
            self.assertEqual(manifest["exit_code"], exit_code)
            receipt = manifest["groups"][0]["results"][tool_name]
            self.assertEqual(receipt["status"], "cancelled")
            self.assertEqual(receipt["exit_code"], exit_code)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(child_pid.read_text()), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if child_pid.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(int(child_pid.read_text()), signal.SIGKILL)

    def test_signal_handler_is_restored_and_background_thread_run_is_supported(self):
        previous = signal.getsignal(signal.SIGTERM)
        for failure in (False, True):
            with patch.object(common, "plan", return_value=[]), \
                    patch.object(session, "complete_run", side_effect=ValueError("fixture") if failure else None, return_value=0):
                if failure:
                    with self.assertRaises(ValueError):
                        session.run([self.source], self.folder / "failure", ())
                else:
                    self.assertEqual(session.run([self.source], self.folder / "success", ()), 0)
            self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        failures = []
        def background():
            try:
                with patch.object(common, "plan", return_value=[]), patch.object(session, "complete_run", return_value=0):
                    session.run([self.source], self.folder / "thread", ())
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=background)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)


if __name__ == "__main__":
    unittest.main()
