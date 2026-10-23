import contextlib
import csv
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import common
import evidence
import session
from clang_static_analyzer import scanner as csa
from clang_tidy.severity import ownership
from clang_tidy.dashboard import render_html
from cpp_check import reporting as cppcheck_reporting
from cpp_check import scanner as cppcheck


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sast-reliability-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "source.c"
        self.source.write_text("int main(void) { return 0; }\n")
        self.database = self.folder / "compile_commands.json"
        self.entry = {"file": str(self.source), "directory": str(self.folder),
                      "arguments": ["cc", "-c", str(self.source)]}
        common.write_json(self.database, [self.entry])
        common.CANCELLED.clear()
        self.addCleanup(common.CANCELLED.clear)

    def execute(self, code, timeout=5):
        return common.execute([sys.executable, "-c", code], self.folder,
                              self.folder / "stdout", self.folder / "stderr", timeout=timeout)

    def test_standard_input_is_eof(self):
        receipt = self.execute("import sys; print(len(sys.stdin.read()))")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual((self.folder / "stdout").read_text().strip(), "0")

    def test_timeout_has_receipt_and_kills_descendants(self):
        child_file = self.folder / "child.pid"
        code = ("import subprocess,sys,time; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"Path({str(child_file)!r}).write_text(str(child.pid)); time.sleep(60)")
        receipt = self.execute(code, timeout=1)
        self.assertEqual(receipt["exit_code"], 124)
        self.assertTrue(receipt["timed_out"])
        child = Path("/proc") / child_file.read_text().strip() / "stat"
        if child.exists():
            self.assertEqual(child.read_text().split()[2], "Z")
        self.assertFalse(common.ACTIVE_PROCESSES)

    def test_timeout_retries_three_times_and_preserves_attempt_logs(self):
        code = "import time; time.sleep(60)"
        receipt = self.execute(code, timeout=1)
        self.assertEqual(receipt["exit_code"], 124)
        self.assertTrue(receipt["timed_out"])
        self.assertEqual(len(receipt["attempts"]), 4)
        for attempt in receipt["attempts"]:
            self.assertTrue(Path(attempt["stdout"]).exists())
            self.assertTrue(Path(attempt["stderr"]).exists())

    def test_missing_executable_has_receipt(self):
        receipt = common.execute([str(self.folder / "missing")], self.folder,
                                 self.folder / "stdout", self.folder / "stderr")
        self.assertEqual(receipt["exit_code"], 127)

    def test_offline_install_checks_archive_and_extracts_outside_source(self):
        tool_folder = self.folder / "installation"
        (tool_folder / "source").mkdir(parents=True)
        installer = tool_folder / "install.sh"
        shutil.copy2(ROOT / "install/cpp_check/install.sh", installer)
        for name in ("cppcheck-source.tar.xz", "cppcheck-source.tar.xz.sha256"):
            shutil.copy2(ROOT / "install/cpp_check/source" / name, tool_folder / "source" / name)
        binary_folder = self.folder / "fake-build-bin"
        binary_folder.mkdir()
        make = binary_folder / "make"
        make.write_text("#!" + sys.executable + "\nimport sys\nfrom pathlib import Path\n"
                        "if 'install' in sys.argv:\n"
                        " prefix=Path(next(arg.split('=',1)[1] for arg in sys.argv if arg.startswith('PREFIX=')))\n"
                        " binary=prefix/'bin/cppcheck'\n binary.parent.mkdir(parents=True,exist_ok=True)\n"
                        " binary.write_text('#!/bin/sh\\necho Cppcheck-fixture\\n')\n binary.chmod(0o755)\n")
        make.chmod(0o755)
        environment = dict(os.environ, PATH=str(binary_folder) + os.pathsep + os.environ["PATH"],
                           CPPCHECK_INSTALL_PREFIX=str(self.folder / "prefix"), CPPCHECK_BUILD_SYSTEM="make", CXX="true")
        environment.pop("CPPCHECK_SOURCE_DIR", None)
        completed = subprocess.run(["bash", str(installer)], env=environment, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertTrue((tool_folder / "tmp/cppcheck-source/cppcheck/Makefile").exists())
        self.assertFalse((tool_folder / "source/cppcheck").exists())
        (tool_folder / "source/cppcheck-source.tar.xz.sha256").write_text("0" * 64 + "  cppcheck-source.tar.xz\n")
        completed = subprocess.run(["bash", str(installer)], env=environment, text=True, capture_output=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("checksum mismatch", completed.stderr)
        self.assertTrue((tool_folder / "tmp/cppcheck-source/cppcheck/Makefile").exists())

    def test_html_helper_works_without_full_sources_or_installed_helper(self):
        output = self.folder / "cppcheck"
        output.mkdir()
        raw = output / "report.xml"
        raw.write_text('<results version="2"><errors/></results>')
        receipt = {"mode": "compile", "stderr": str(raw), "exit_code": 0, "ok": True}
        group = {"database": str(self.database)}
        with patch.object(cppcheck_reporting.shutil, "which", return_value=None), \
                patch.object(cppcheck_reporting.Path, "home", return_value=self.folder / "empty-home"):
            cppcheck_reporting.generate(output, receipt, group, [self.entry], [self.source], {}, {})
        self.assertTrue((output / "html/index.html").exists())
        self.assertIn("scan/cpp_check/htmlreport", receipt["html_execution"]["command"][1])

    def test_cppcheck_xml_must_have_version_two(self):
        output = self.folder / "cppcheck-invalid"
        def execute(command, cwd, stdout, stderr):
            stdout.write_text("")
            stderr.write_text('<results version="1"><errors/></results>')
            return {"exit_code": 0}
        with patch.object(common, "execute", side_effect=execute):
            receipt = cppcheck.scan([self.entry], [self.source], self.database, output, {"cppcheck": "cppcheck"})
        self.assertFalse(receipt["ok"])
        self.assertIn("artifact_error", receipt)

    def test_linker_output_does_not_delete_include(self):
        entry = dict(self.entry, arguments=["cc", "-Xlinker", "-o", "-Ivendor/include", "-c", str(self.source)])
        self.assertEqual(csa.compiler_arguments(entry), ["-Ivendor/include"])

    def test_forwarded_output_is_removed_without_deleting_context(self):
        entry = dict(self.entry, arguments=["cc", "-I", "include", "-D", "FEATURE=1", "-Xclang", "-dependency-file",
                                           "-Xclang", "output.d", "--serialize-diagnostics=output.dia", "-c", str(self.source)])
        self.assertEqual(csa.compiler_arguments(entry), ["-I", "include", "-D", "FEATURE=1"])

    def test_option_value_is_not_reinterpreted_as_output_flag(self):
        entry = dict(self.entry, arguments=["cc", "-D", "-o", "-include", "header.h", "-c", str(self.source)])
        self.assertEqual(csa.compiler_arguments(entry), ["-D", "-o", "-include", "header.h"])

    def test_response_file_fails_closed(self):
        entry = dict(self.entry, arguments=["cc", "@flags.rsp", str(self.source)])
        with self.assertRaisesRegex(ValueError, "expanded response"):
            csa.compiler_arguments(entry)

    def test_objective_c_option_is_not_mistaken_for_output(self):
        entry = dict(self.entry, arguments=["cc", "-objc-arc", "-ooutput.o", "-c", str(self.source)])
        self.assertEqual(csa.compiler_arguments(entry), ["-objc-arc"])

    def test_missing_tool_keeps_other_tools_and_manifest(self):
        calls = []
        def available_scan(entries, scopes, database, output, binaries):
            output.mkdir()
            calls.append("available")
            return {"ok": True}
        available = SimpleNamespace(NAME="available", POLICY={}, SUPPORTS_SOURCE=True, REPORT_PRIORITY=0,
                                    resolve_tools=lambda: {}, scan=available_scan,
                                    report=lambda *arguments: None)
        missing = SimpleNamespace(NAME="missing", POLICY={}, SUPPORTS_SOURCE=False, REPORT_PRIORITY=0,
                                  resolve_tools=lambda: common.find_tool("sast-fixture-nonexistent"))
        output = self.folder / "results"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(session.run([self.source], output, [missing, available]), 1)
        manifest = json.loads(next(output.glob("*/manifest.json")).read_text())
        self.assertEqual(calls, ["available"])
        self.assertEqual(manifest["groups"][0]["results"]["missing"]["status"], "unavailable")
        self.assertFalse(manifest["evidence_checks_ok"])

    def test_project_specific_vendor_paths_are_not_universal_rules(self):
        self.assertEqual(ownership("include/quanergy/component.h"), "project")
        self.assertEqual(ownership("src/detector/perception/fastlioOdometry/fast-lio/module.h"), "project")
        self.assertEqual(ownership("thirdparty/dependency.h"), "vendored")

    def test_dashboard_uses_actual_version_and_directories(self):
        payload = {"files": [], "scan_dirs": ["custom"], "scan_paths": ["/project/custom"], "tool_version": "LLVM version 18.1.0",
                   "checks": "fixed-checks", "compile_db_dir": "/project/build", "jobs": 4}
        page = render_html(payload, self.folder, self.folder / "absent-csa")
        self.assertIn("LLVM version 18.1.0", page)
        self.assertIn('value="custom/"', page)
        self.assertIn("/project/custom", page)
        self.assertNotIn("LLVM 10", page)
        self.assertNotIn("tools/static_analy", page)
        self.assertNotIn('value="app/"', page)

    def test_csa_mapping_has_contract_columns(self):
        output = self.folder / "csa"
        def execute(command, cwd, stdout, stderr):
            import plistlib
            stdout.write_text("")
            stderr.write_text("")
            if "-analyzer-output=plist-html" in command:
                with Path(command[command.index("-o") + 1]).open("wb") as stream:
                    plistlib.dump({"diagnostics": []}, stream)
            return {"exit_code": 0}
        with patch.object(common, "execute", side_effect=execute):
            result = csa.scan([self.entry], [self.source], self.database, output, {"clang": "clang", "clang++": "clang++"})
        with (output / "tu-mapping.csv").open(newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[0], ["slug", "file", "diagnostics", "exit_code"])
        self.assertEqual(rows[1][1:], [str(self.source), "0", "0"])
        self.assertIn("elapsed_seconds", result)

    def test_evidence_snapshot_precedes_scan_and_detects_source_changes(self):
        folder = self.folder / "group-1"
        folder.mkdir()
        selected = folder / "compile_commands.json"
        common.write_json(selected, [self.entry])
        group = {"database": str(self.database), "selected_database": str(selected), "scan_paths": [str(self.source)],
                 "database_sha256": common.digest(self.database), "selected_files": [str(self.source)], "results": {}}
        manifest = {"versions": {}, "policy": {}}
        evidence.begin(group, folder, [self.source], self.folder / "results", manifest)
        before_hash = group["baseline"]["sources"][str(self.source)]
        self.source.write_text("int changed;\n")
        evidence.finish(group, folder, [], manifest)
        self.assertIn("Selected source files changed during scan", group["release_gate"]["errors"])
        bundle = folder / "evidence-bundle"
        self.assertTrue((bundle / "01-metadata/code-state.txt").exists())
        self.assertEqual((bundle / "02-compile-db/compile_commands.json").read_bytes(), self.database.read_bytes())
        saved = json.loads((bundle / "01-metadata/source-hashes.json").read_text())
        self.assertEqual(saved[str(self.source)], before_hash)
        exported = json.loads((bundle / "manifest.json").read_text())
        self.assertFalse(exported["release_ready"])
        self.assertFalse(exported["release_gate"]["release_ready"])
        for artifact in exported["artifacts"]:
            path = bundle / artifact["path"]
            self.assertFalse(Path(artifact["path"]).is_absolute())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), artifact["sha256"])

    def test_interrupt_leaves_cancelled_manifest_and_no_running_child(self):
        self.assert_interrupt_cleanup("cppcheck", "cpp_check")

    def test_interrupt_cleans_threaded_analyzer_processes(self):
        self.assert_interrupt_cleanup("clang-tidy", "clang_tidy")

    def assert_interrupt_cleanup(self, executable, tool_name):
        binary_folder = self.folder / "bin"
        binary_folder.mkdir()
        child_pid = self.folder / "scanner.pid"
        tool = binary_folder / executable
        tool.write_text("#!" + sys.executable + "\nimport os,sys,time\nfrom pathlib import Path\n"
                        "if '--version' in sys.argv:\n print('Cppcheck 2.22')\nelse:\n"
                        f" Path({str(child_pid)!r}).write_text(str(os.getpid()))\n time.sleep(60)\n")
        tool.chmod(0o755)
        output = self.folder / "interrupt-results"
        environment = dict(os.environ, PATH=str(binary_folder) + os.pathsep + os.environ["PATH"], PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen(["bash", str(ROOT / "scan" / tool_name / "scan.sh"), "--scan-path", str(self.source),
                                    "--result-path", str(output)], env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while not child_pid.exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.05)
            self.assertTrue(child_pid.exists())
            process.send_signal(signal.SIGINT)
            self.assertEqual(process.wait(timeout=10), 130)
            manifest = json.loads(next(output.glob("*/manifest.json")).read_text())
            self.assertTrue(manifest["cancelled"])
            with self.assertRaises(ProcessLookupError):
                os.kill(int(child_pid.read_text()), 0)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


if __name__ == "__main__":
    unittest.main()
