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
from clang_tidy import scanner as tidy
from clang_tidy import reporting as tidy_reporting
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

    def test_timeout_then_success_keeps_canonical_final_bytes_and_attempt_history(self):
        code = (
            "from pathlib import Path; import sys,time; "
            "counter=Path.cwd()/'attempt-count'; "
            "number=int(counter.read_text())+1 if counter.exists() else 1; "
            "counter.write_text(str(number)); "
            "print(f'stdout attempt {number}', flush=True); "
            "print(f'stderr attempt {number}', file=sys.stderr, flush=True); "
            "time.sleep(60) if number == 1 else None"
        )
        with patch.object(common, "TIMEOUT_ATTEMPTS", 2):
            receipt = self.execute(code, timeout=1)

        self.assertEqual(receipt["exit_code"], 0)
        self.assertFalse(receipt["timed_out"])
        self.assertTrue(receipt["timeout_recovered"])
        self.assertEqual(receipt["stdout"], str(self.folder / "stdout"))
        self.assertEqual(receipt["stderr"], str(self.folder / "stderr"))
        self.assertEqual(len(receipt["attempts"]), 2)
        self.assertTrue(receipt["attempts"][0]["timed_out"])
        self.assertFalse(receipt["attempts"][1]["timed_out"])
        self.assertEqual((self.folder / "stdout").read_text(), "stdout attempt 2\n")
        self.assertEqual((self.folder / "stderr").read_text(), "stderr attempt 2\n")
        self.assertEqual(Path(receipt["attempts"][0]["stdout"]).read_text(), "stdout attempt 1\n")
        first_stderr = Path(receipt["attempts"][0]["stderr"]).read_text()
        self.assertTrue(first_stderr.startswith("stderr attempt 1\n"))
        self.assertIn("Process timeout; process group terminated.", first_stderr)
        self.assertEqual(Path(receipt["attempts"][1]["stdout"]).read_text(), "stdout attempt 2\n")
        self.assertEqual(Path(receipt["attempts"][1]["stderr"]).read_text(), "stderr attempt 2\n")

    def test_git_evidence_reads_canonical_bytes_after_a_retry(self):
        fake_git = self.folder / "git"
        fake_git.write_text("#!" + sys.executable + "\n" + '''
import hashlib, sys, time
from pathlib import Path
arguments = sys.argv[3:]
key = hashlib.sha256("\\0".join(arguments).encode()).hexdigest()[:12]
counter = Path.cwd() / ("git-call-" + key)
number = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(number))
if number == 1:
    print("partial git output", flush=True)
    time.sleep(60)
elif arguments == ["rev-parse", "--show-toplevel"]:
    print(Path.cwd())
elif arguments == ["rev-parse", "--abbrev-ref", "HEAD"]:
    print("main")
elif arguments == ["rev-parse", "HEAD"]:
    print("a" * 40)
''')
        fake_git.chmod(0o755)
        output = self.folder / "git-evidence"
        output.mkdir()
        original_execute = common.execute

        def short_execute(command, cwd, stdout, stderr, timeout=None, env=None):
            return original_execute(command, cwd, stdout, stderr, timeout=1, env=env)

        with patch.dict(os.environ, {"PATH": str(self.folder) + os.pathsep + os.environ["PATH"]}), \
                patch.object(common, "TIMEOUT_ATTEMPTS", 2), patch.object(common, "execute", side_effect=short_execute):
            state = evidence.git_state(self.folder, output, "before")

        self.assertTrue(state["ok"])
        self.assertEqual(state["repo"], str(self.folder))
        self.assertEqual(state["branch"], "main")
        self.assertEqual(state["commit"], "a" * 40)
        expected_output = {
            ("rev-parse", "--show-toplevel"): str(self.folder),
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
            ("rev-parse", "HEAD"): "a" * 40,
            ("status", "--porcelain"): "",
            ("diff", "HEAD", "--binary"): "",
        }
        for receipt in state["receipts"].values():
            self.assertEqual(Path(receipt["stdout"]).read_text().strip(),
                             expected_output[tuple(receipt["command"][3:])])
            self.assertEqual(len(receipt["attempts"]), 2)
            self.assertTrue(receipt["attempts"][0]["timed_out"])
            self.assertIn("partial git output", Path(receipt["attempts"][0]["stdout"]).read_text())

    def test_cppcheck_timeout_retry_counts_final_native_xml(self):
        tool = self.folder / "cppcheck-fixture"
        tool.write_text("#!" + sys.executable + "\n" + '''
import sys, time
from pathlib import Path
cache = Path(next(arg for arg in sys.argv if arg.startswith("--cppcheck-build-dir=")).split("=", 1)[1])
counter = cache.parent / "cppcheck-attempt-count"
number = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(number))
if number == 1:
    print("first attempt", flush=True)
    sys.stderr.write("<results version=\\\"2\\\">")
    sys.stderr.flush()
    time.sleep(60)
else:
    print("final attempt", flush=True)
    sys.stderr.write('<results version="2"><cppcheck version="2.22.0"/><errors>'
                     '<error id="one"/><error id="two"/></errors></results>')
''')
        tool.chmod(0o755)
        output = self.folder / "cppcheck-retry"
        output.mkdir()
        with patch.object(common, "TIMEOUT_ATTEMPTS", 2), patch.object(common, "PROCESS_TIMEOUT", 1):
            receipt = cppcheck.scan_with_cache([self.entry], [self.folder], self.database, output,
                                               {"cppcheck": str(tool)})

        report = output / "cppcheck-compile-report.xml"
        self.assertTrue(receipt["ok"], receipt.get("artifact_error"))
        self.assertEqual(receipt["diagnostics"], 2)
        self.assertEqual(receipt["stderr"], str(report))
        self.assertEqual(len(receipt["attempts"]), 2)
        self.assertTrue(Path(receipt["attempts"][0]["stderr"]).name.endswith(".err.log"))
        self.assertNotEqual(Path(receipt["attempts"][0]["stderr"]).suffix, ".xml")
        self.assertIn('<error id="one"/>', report.read_text())
        self.assertIn("<results", Path(receipt["attempts"][0]["stderr"]).read_text())
        captured = json.loads((output / "receipt.json").read_text())
        self.assertEqual(captured["diagnostics"], 2)
        self.assertEqual(captured["stderr"], str(report))
        self.assertEqual(captured["attempts"], receipt["attempts"])

    def test_tidy_retry_final_log_remains_bound_to_native_yaml_for_handoff(self):
        tool = self.folder / "clang-tidy-fixture"
        tool.write_text("#!" + sys.executable + "\n" + '''
import sys, time
from pathlib import Path
artifact = Path(next(arg for arg in sys.argv if arg.startswith("--export-fixes=")).split("=", 1)[1])
source = Path(sys.argv[-1])
counter = Path(__file__).with_name(".tidy-invoke-count")
number = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(number))
if number == 1:
    print(str(source) + ":1:5: warning: incomplete first attempt [bugprone-first-attempt]", file=sys.stderr, flush=True)
    time.sleep(60)
else:
    print(str(source) + ":1:5: warning: final diagnostic [bugprone-example-check]", file=sys.stderr, flush=True)
    artifact.write_text(
        "MainSourceFile: " + str(source) + "\\n"
        "Diagnostics:\\n"
        "  - DiagnosticName: bugprone-example-check\\n"
        "    Level: Warning\\n"
        "    DiagnosticMessage:\\n"
        "      Message: final diagnostic\\n"
        "      FilePath: " + str(source) + "\\n"
        "      FileOffset: 4\\n"
        "    Notes: []\\n"
        "    Replacements: []\\n")
''')
        tool.chmod(0o755)
        output = self.folder / "clang-tidy-retry"
        version = self.folder / "clang-tidy-version.txt"
        version.write_text("Ubuntu LLVM version 18.1.3\n")
        group = {"database": str(self.database), "selected_database": str(self.database),
                 "tool_versions": {"clang-tidy": {"stdout": str(version)}}}
        with patch.object(common, "TIMEOUT_ATTEMPTS", 2), patch.object(common, "PROCESS_TIMEOUT", 1):
            result = tidy.scan([self.entry], [self.folder], self.database, output, {"clang-tidy": str(tool)})
        item = result["files"][0]
        final_warning = str(self.source) + ":1:5: warning: final diagnostic [bugprone-example-check]\n"
        self.assertTrue(item["ok"])
        self.assertTrue(item["timeout_recovered"])
        self.assertTrue(Path(item["attempts"][0]["stderr"]).name.endswith(".err.log"))
        self.assertEqual((output / "logs" / (item["unit_id"] + ".err.log")).read_text(), final_warning)
        self.assertIn("incomplete first attempt", Path(item["attempts"][0]["stderr"]).read_text())

        tidy.report(result, group, output, [self.entry], [self.folder], {"clang-tidy": str(tool)})
        summary = json.loads((output / "clang-tidy-summary.json").read_text())
        self.assertEqual(summary["files"][0]["stderr"], str(output / "logs" / (item["unit_id"] + ".err.log")))
        self.assertEqual(summary["files"][0]["attempts"], item["attempts"])
        self.assertEqual(summary["warnings"]["raw_total"], 1)
        self.assertEqual(summary["warnings"]["unique_total"], 1)
        self.assertEqual(summary["warnings"]["by_check"],
                         [{"check": "bugprone-example-check", "raw": 1, "unique": 1}])
        with (output / "severity.csv").open(newline="", encoding="utf-8") as stream:
            severity_rows = list(csv.DictReader(stream))
        self.assertEqual([row["check"] for row in severity_rows], ["bugprone-example-check"])
        report_html = (output / "clang-tidy-report.html").read_text(encoding="utf-8")
        self.assertNotIn("bugprone-first-attempt", report_html)
        self.assertNotIn("incomplete first attempt", report_html)
        evidence_root = self.folder / "runner-evidence"
        evidence_root.mkdir()
        validation = tidy.export_evidence(output, evidence_root, result)
        self.assertEqual(validation["errors"], [])
        copied = evidence_root / "04-clang-tidy/result"
        fixed_handoff_log = copied / "logs" / (item["unit_id"] + ".err.log")
        native_yaml = copied / "fixes" / (item["unit_id"] + ".yaml")
        combined_log = copied / "logs" / (item["unit_id"] + ".log")
        self.assertEqual(fixed_handoff_log.read_text(), final_warning)
        self.assertEqual(tidy_reporting.log_diagnostics(combined_log, self.folder),
                         tidy_reporting.yaml_diagnostics(native_yaml, self.folder, self.source))

        no_retry_output = self.folder / "clang-tidy-no-retry"
        no_retry_result = tidy.scan([self.entry], [self.folder], self.database, no_retry_output,
                                    {"clang-tidy": str(tool)})
        no_retry_item = no_retry_result["files"][0]
        self.assertFalse(no_retry_item.get("timeout_recovered", False))
        self.assertEqual(len(no_retry_item["attempts"]), 1)
        tidy.report(no_retry_result, group, no_retry_output, [self.entry], [self.folder], {"clang-tidy": str(tool)})
        no_retry_summary = json.loads((no_retry_output / "clang-tidy-summary.json").read_text())
        self.assertEqual(no_retry_summary["warnings"]["raw_total"], 1)
        self.assertEqual(no_retry_summary["warnings"]["unique_total"], 1)
        self.assertEqual(no_retry_summary["warnings"]["by_check"],
                         [{"check": "bugprone-example-check", "raw": 1, "unique": 1}])

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
        self.assertEqual(ownership("include/quanergy/component.h"), "unclassified")
        self.assertEqual(ownership("src/detector/perception/fastlioOdometry/fast-lio/module.h"), "unclassified")
        self.assertEqual(ownership("thirdparty/dependency.h"), "unclassified")
        self.assertEqual(ownership("/usr/include/stdio.h"), "unclassified")
        self.assertEqual(ownership("/opt/product/lib/business.cpp"), "unclassified")
        self.assertEqual(ownership("/usr/local/business/include/module.hpp"), "unclassified")

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
