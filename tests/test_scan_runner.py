import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import common
import deliver
import session
from registry import SCANNERS
from clang_tidy import scanner as tidy
from cpp_check import scanner as cppcheck
from clang_static_analyzer import scanner as csa


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fixed scan ")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.project = self.folder / "project"
        self.source = self.project / "src"
        self.source.mkdir(parents=True)
        self.first = self.source / "first.cpp"
        self.first.write_text("int first() { return 0; }\n")
        self.second = self.source / "second.cpp"
        self.second.write_text("int second() { return 1; }\n")
        self.noop_tool = self.folder / "no-op-tool"
        self.noop_tool.write_text("#!" + sys.executable + "\npass\n")
        self.noop_tool.chmod(0o755)
        self.config = self.folder / "analy.config"
        self.database = self.project / "build/compile_commands.json"
        self.database.parent.mkdir()
        self.entries = [
            {"directory": str(self.project), "file": "src/first.cpp",
             "arguments": ["c++", "-c", "src/first.cpp", "-o", "first.o"]},
            {"directory": str(self.project), "file": "src/second.cpp",
             "command": "c++ -c src/second.cpp -o second.o"},
        ]
        self.database.write_text(json.dumps(self.entries))

    def write_config(self, value):
        self.config.write_text(json.dumps(value))

    def test_default_config_lives_in_scan_directory(self):
        self.assertEqual(common.CONFIG, ROOT / "scan/analy.config")
        self.assertEqual(common.inputs.__defaults__[0], common.CONFIG)
        self.assertTrue((ROOT / "analy.config.example").is_file())
        self.assertFalse((ROOT / "scan/analy.config.example").exists())

    def test_scan_config_relative_paths_use_scan_directory(self):
        scan = self.folder / "scan"
        scan.mkdir()
        config = scan / "analy.config"
        config.write_text(json.dumps({"scan_paths": ["../project/src"], "result_path": "../results"}))
        self.assertEqual(common.inputs([], config), ([self.source], self.folder / "results"))

    def test_cli_needs_no_config(self):
        paths, output = common.inputs(["--scan-path", str(self.source), "--result-path", str(self.folder / "out")], self.config)
        self.assertEqual(paths, [self.source])
        self.assertEqual(output, self.folder / "out")

    def test_multiple_paths_and_repetition(self):
        paths, _ = common.inputs(["--scan-path", str(self.first), str(self.second),
                                  "--scan-path", str(self.first), "--result-path", "out"], self.config)
        self.assertEqual(paths, [self.first, self.second])

    def test_file_paths_are_relative_to_config(self):
        self.write_config({"scan_paths": ["project/src"], "result_path": "out"})
        self.assertEqual(common.inputs([], self.config), ([self.source], self.folder / "out"))

    def test_cli_paths_replace_file_list(self):
        self.write_config({"scan_paths": [str(self.second)], "result_path": "out"})
        paths, output = common.inputs(["--scan-path", str(self.first)], self.config)
        self.assertEqual(paths, [self.first])
        self.assertEqual(output, self.folder / "out")

    def test_cli_output_overrides_only_output(self):
        self.write_config({"scan_paths": [str(self.source)], "result_path": "out"})
        paths, output = common.inputs(["--result-path", "cli-out"], self.config)
        self.assertEqual(paths, [self.source])
        self.assertEqual(output, Path.cwd() / "cli-out")

    def test_cli_relative_scopes_use_cwd(self):
        with patch.object(Path, "cwd", return_value=self.project):
            paths, output = common.inputs(["--scan-path", "src", "--result-path", "out"], self.config)
        self.assertEqual((paths, output), ([self.source], self.project / "out"))

    def test_unknown_and_duplicate_config_keys_rejected(self):
        for content in ('{"scan_paths": [], "checks": "-*"}',
                        '{"result_path":"a","result_path":"b"}'):
            self.config.write_text(content)
            with self.assertRaises(ValueError):
                common.inputs(["--scan-path", str(self.source), "--result-path", "out"], self.config)

    def test_bad_config_types_rejected(self):
        for value in ([1], {"scan_paths": "src"}, {"scan_paths": [1]},
                      {"scan_paths": []}, {"result_path": False}):
            self.write_config(value)
            with self.assertRaises(ValueError):
                common.inputs([], self.config)

    def test_legacy_shell_config_never_executed(self):
        marker = self.folder / "executed"
        self.config.write_text("touch '" + str(marker) + "'\nSCAN_CODE_DIR=/tmp\n")
        with self.assertRaises(ValueError):
            common.inputs([], self.config)
        self.assertFalse(marker.exists())

    def test_missing_empty_and_unsupported_paths_rejected(self):
        for path in ("", "   ", str(self.folder / "missing"), str(self.config)):
            self.config.write_text("{}")
            with self.assertRaises(ValueError):
                common.inputs(["--scan-path", path, "--result-path", "out"], self.config)

    def test_old_overrides_are_ignored(self):
        self.write_config({"scan_paths": [str(self.source)], "result_path": "out"})
        with patch.dict(os.environ, {"ANALYZ_CONFIG": "/missing", "SCAN_CODE_DIR": "/etc",
                                     "WORK_DIR": "/etc", "CLANG_TIDY_CHECKS": "-*", "CPATH": "/tmp"}):
            self.assertEqual(common.inputs([], self.config), ([self.source], self.folder / "out"))
            self.assertNotIn("CLANG_TIDY_CHECKS", common.environment())
            self.assertNotIn("CPATH", common.environment())

    def test_scope_union_and_relative_database_records(self):
        groups = common.plan([self.first, self.second, self.source])
        self.assertEqual(len(groups), 1)
        self.assertEqual([entry["file"] for entry in groups[0][2]], [str(self.first), str(self.second)])
        self.assertEqual(groups[0][2][1]["arguments"][0], "c++")

    def test_identical_compile_contexts_deduplicate_and_file_scope_is_exact(self):
        original = self.entries[0]
        equivalent = {"directory": str(self.project / "build" / ".."), "file": "src/first.cpp",
                      "command": "c++ -c src/first.cpp -o first.o"}
        planned = []
        for records in ([original, equivalent], [equivalent, original]):
            self.database.write_text(json.dumps(records))
            entries = common.plan([self.first])[0][2]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["file"], str(self.first))
            self.assertEqual(entries[0]["directory"], str(self.project))
            self.assertEqual(entries[0]["arguments"], original["arguments"])
            planned.append(entries)
        self.assertEqual(planned[0], planned[1])

    def test_conflicting_macro_contexts_keep_first_record_in_any_database_order(self):
        first = {"directory": str(self.project), "file": "src/first.cpp",
                 "arguments": ["c++", "-DPRODUCT_A=1", "-c", "src/first.cpp", "-o", "first.o"]}
        second = {**first, "arguments": ["c++", "-DPRODUCT_A=2", "-c", "src/first.cpp", "-o", "first.o"]}
        for records in ([first, second], [second, first]):
            self.database.write_text(json.dumps(records))
            entries = common.plan([self.first])[0][2]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["arguments"], records[0]["arguments"])

    def test_conflicting_directories_keep_first_record_in_any_database_order(self):
        arguments = ["c++", "-c", str(self.first), "-o", "first.o"]
        first = {"directory": str(self.project), "file": str(self.first), "arguments": arguments}
        second = {"directory": str(self.project / "build"), "file": str(self.first), "arguments": arguments}
        for records in ([first, second], [second, first]):
            self.database.write_text(json.dumps(records))
            entries = common.plan([self.first])[0][2]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["directory"], str(Path(records[0]["directory"]).resolve()))

    def test_conflicting_context_is_deduplicated_before_scanning(self):
        first = {"directory": str(self.project), "file": "src/first.cpp",
                 "arguments": ["c++", "-DPRODUCT_A=1", "-c", "src/first.cpp", "-o", "first.o"]}
        second = {**first, "arguments": ["c++", "-DPRODUCT_A=2", "-c", "src/first.cpp", "-o", "first.o"]}
        self.database.write_text(json.dumps([first, second]))
        entries = common.plan([self.first])[0][2]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["arguments"], first["arguments"])

    def test_find_database_supports_build_variants(self):
        self.database.unlink()
        custom_build = self.project / "build_n4"
        custom_build.mkdir()
        custom_db = custom_build / "compile_commands.json"
        custom_db.write_text(json.dumps([{"directory": str(self.project), "file": str(self.first),
                                          "arguments": ["c++", "-c", str(self.first), "-o", "first.o"]}]))
        found = common.find_database(self.first)
        self.assertEqual(found, custom_db.resolve())
        root = common.source_root([self.first], found)
        self.assertEqual(root, self.project.resolve())

    def test_missing_database_does_not_fallback(self):
        self.database.unlink()
        with self.assertRaises(ValueError):
            common.plan([self.source])

    def test_unmatched_and_missing_source_fail(self):
        empty = self.project / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            common.plan([empty])
        self.first.unlink()
        with self.assertRaises(ValueError):
            common.plan([self.source])

    def test_closest_database_selected(self):
        local = self.source / "compile_commands.json"
        local.write_text(self.database.read_text())
        self.assertEqual(common.find_database(self.first), local)

    def test_multiple_projects_have_separate_database_groups(self):
        other = self.folder / "other"
        other.mkdir()
        source = other / "other.c"
        source.write_text("int other(void) { return 0; }\n")
        database = other / "compile_commands.json"
        database.write_text(json.dumps([{"directory": str(other), "file": "other.c",
                                         "arguments": ["cc", "-c", "other.c"]}]))
        groups = common.plan([self.source, other])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0][0], self.database)
        self.assertEqual(groups[1][0], database)
        self.assertEqual(groups[1][2][0]["file"], str(source))

    def test_scope_does_not_follow_symlinks_outside(self):
        outside = self.project / "outside.cpp"
        outside.write_text("int outside();")
        link = self.source / "link.cpp"
        link.symlink_to(outside)
        self.assertFalse(common.in_scope(link, [self.source]))

    def test_compiler_output_and_source_arguments_removed(self):
        entry = common.plan([self.first])[0][2][0]
        entry["arguments"] = ["ccache", "c++", "-Iinclude", "-c", "src/first.cpp", "-oresult.o", "-MMD", "-MF", "deps"]
        self.assertEqual(csa.compiler_arguments(entry), ["-Iinclude"])

    def test_top_level_thirdparty_is_excluded(self):
        vendor = self.project / "thirdparty"
        vendor.mkdir()
        source = vendor / "vendor.cpp"
        source.write_text("int vendor();\n")
        self.entries.append({"directory": str(self.project), "file": str(source),
                             "arguments": ["c++", "-c", str(source)]})
        self.database.write_text(json.dumps(self.entries))
        selected = common.plan([self.project])[0][2]
        self.assertEqual({entry["file"] for entry in selected}, {str(self.first), str(self.second)})
        self.assertTrue(common.in_scope(source, [vendor]))

    def test_header_filter_uses_llvm_compatible_groups(self):
        (self.project / "thirdparty").mkdir()
        pattern = tidy.header_pattern([self.project])
        self.assertNotIn("(?", pattern)
        self.assertIsNotNone(re.search(pattern, str(self.project / "root.hpp")))
        self.assertIsNotNone(re.search(pattern, str(self.source / "nested.hpp")))
        self.assertIsNone(re.search(pattern, str(self.project / "thirdparty/vendor.hpp")))
        self.assertIsNone(re.search(pattern, str(self.folder / "outside.hpp")))

    def test_all_public_entries_reject_extra_parameters(self):
        entries = [ROOT / "scan/scan_all.sh", ROOT / "scan/runner.py"]
        entries += list((ROOT / "scan").glob("*/scan.sh"))
        entries += list((ROOT / "scan").glob("*/entry.py"))
        entries += [ROOT / "scan" / name for name in ("serve.sh", "serve.py", "verify.sh", "verify.py",
                                                     "collect_acceptance_env.sh", "collect_acceptance_env.py")]
        for entry in entries:
            for forbidden in ("--only", "--checks", "--jobs", "--source", "--compile-db-dir", "--config", "--scan-p", "positional.cpp"):
                with self.subTest(entry=str(entry), forbidden=forbidden):
                    interpreter = "bash" if entry.suffix == ".sh" else sys.executable
                    result = subprocess.run([interpreter, str(entry), forbidden], capture_output=True)
                    self.assertEqual(result.returncode, 2)

    def test_full_run_and_subtool_manifest_with_fixed_policy(self):
        tool = self.folder / "fake-tool"
        tool.write_text("#!" + sys.executable + "\n" + '''import json, os, plistlib, sys
from pathlib import Path
assert "CLANG_TIDY_CHECKS" not in os.environ
if "--version" in sys.argv:
    print("fixture-tool 1.0")
elif "--xml" in sys.argv:
    print('<results version="2"><errors/></results>', file=sys.stderr)
elif "--analyze" in sys.argv:
    if "-analyzer-output=plist-html" in sys.argv:
        with open(sys.argv[sys.argv.index("-o") + 1], "wb") as stream:
            plistlib.dump({"diagnostics": []}, stream)
else:
    config = next(item for item in sys.argv if item.startswith("--config="))
    assert json.loads(config.split("=", 1)[1])["Checks"].startswith("-*,")
''')
        tool.chmod(0o755)
        self.project.joinpath(".clang-tidy").write_text("Checks: '-*'\n")
        output = self.folder / "results"
        with patch.object(common, "find_tool", return_value=str(tool)), patch.dict(os.environ, {"CLANG_TIDY_CHECKS": "-*"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(session.run([self.source], output, SCANNERS), 3)
            self.assertEqual(session.run([self.first], output, (cppcheck,)), 0)
        manifests = [json.loads(path.read_text()) for path in output.glob("*/manifest.json")]
        self.assertEqual(len(manifests), 2)
        full = next(item for item in manifests if len(item["tools"]) == 3)
        self.assertTrue(full["ok"])
        self.assertEqual(full["policy"]["tools"]["clang_tidy"]["jobs"], 4)
        self.assertEqual(len(full["groups"][0]["selected_files"]), 2)
        self.assertEqual(set(full["groups"][0]["results"]), {scanner.NAME for scanner in SCANNERS})
        partial = next(item for item in manifests if len(item["tools"]) == 1)
        self.assertEqual(partial["tools"], ["cpp_check"])
        full_path = Path(full["run_path"])
        for report in ("index.html", "index.md", "group-1/clang_tidy/clang-tidy-report.html",
                       "group-1/clang_tidy/clang-tidy-summary.md", "group-1/clang_tidy/severity.csv",
                       "group-1/clang_tidy/severity.md", "group-1/cpp_check/html/index.html",
                       "group-1/cpp_check/summary.json", "group-1/cpp_check/selected-tus.csv",
                       "group-1/clang_static_analyzer/index.html", "group-1/clang_static_analyzer/analyzer-summary.md"):
            self.assertTrue((full_path / report).is_file(), report)

    def test_run_directory_discovery_ignores_shared_cppcheck_cache_sibling(self):
        tool = self.folder / "cppcheck-fixture"
        tool.write_text("#!" + sys.executable + "\n" + '''import sys
if "--version" in sys.argv:
    print("Cppcheck 2.22.0")
elif "--xml" in sys.argv:
    print('<results version="2"><cppcheck version="2.22.0"/><errors/></results>', file=sys.stderr)
''')
        tool.chmod(0o755)
        output = self.folder / "one-run"
        output.mkdir()
        with patch.object(common, "find_tool", return_value=str(tool)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(session.run([self.first.resolve()], output.resolve(), (cppcheck,)), 0)

        # A complete three-tool run may also create this runner-owned cache beside its run directory.
        (output / ".cache" / "cppcheck").mkdir(parents=True)
        self.assertTrue((output / ".cache" / "cppcheck").is_dir())
        run_path, manifest = deliver._run_directory(output)
        self.assertIsNotNone(run_path)
        self.assertEqual(manifest["runner_schema"], "three-tool-sast-runner-execution-1.0")
        self.assertEqual(Path(manifest["run_path"]).resolve(), run_path.resolve())

    def test_tool_failure_is_reported_and_other_tools_continue(self):
        output = self.folder / "failed-results"
        success = {"ok": True}
        with patch.object(common, "find_tool", return_value=str(self.noop_tool)), \
                patch.object(cppcheck, "scan", side_effect=OSError("failure")), \
                patch.object(tidy, "scan", return_value=success.copy()) as tidy_scan, \
                patch.object(csa, "scan", return_value=success.copy()) as csa_scan, \
                patch.object(session, "render_reports"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(session.run([self.source], output, SCANNERS), 1)
            self.assertEqual(tidy_scan.call_count, 1)
            self.assertEqual(csa_scan.call_count, 1)
        manifest = json.loads(next(output.glob("*/manifest.json")).read_text())
        self.assertFalse(manifest["ok"])
        self.assertEqual(manifest["groups"][0]["results"]["cpp_check"]["error"], "failure")

    def test_zero_exit_without_csa_artifact_is_a_failure(self):
        database, scopes, entries = common.plan([self.first])[0]
        output = self.folder / "no-plist"
        receipt = csa.scan(entries, scopes, database, output,
                                   {"clang": str(self.noop_tool), "clang++": str(self.noop_tool)})
        self.assertFalse(receipt["ok"])
        self.assertIn("artifact_error", receipt["files"][0])

    def test_source_fallback_is_explicit_and_deduplicated(self):
        self.database.unlink()
        vendor = self.source / "thirdparty"
        vendor.mkdir()
        (vendor / "excluded.cpp").write_text("int excluded();")
        groups = common.plan([self.source, self.first], source_fallback=True)
        self.assertIsNone(groups[0][0])
        self.assertEqual([entry["file"] for entry in groups[0][2]], [str(self.first), str(self.second)])

    def test_source_mode_drops_inconclusive_and_project(self):
        self.database.unlink()
        _, _, entries = common.plan([self.source], source_fallback=True)[0]

        def source_execute(command, cwd, stdout, stderr):
            self.assertFalse(any(arg.startswith("--project=") for arg in command))
            self.assertNotIn("--inconclusive", command)
            self.assertIn(str(self.first), command)
            stderr.write_text('<results version="2"><errors/></results>')
            return {"exit_code": 0, "stderr": str(stderr)}

        with patch.object(common, "execute", side_effect=source_execute):
            result = cppcheck.scan(entries, [self.source], None, self.folder / "source-report", {"cppcheck": "cppcheck"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "source")

    def test_tidy_diagnostics_deduplicate_header_sites_without_duplicate_log_streams(self):
        from clang_tidy.diagnostics import collect_diagnostics
        logs = self.folder / "logs"
        logs.mkdir()
        warning = '/project/header.hpp:12:3: warning: division [bugprone-integer-division]\n'
        for name in ("one.log", "two.log", "one.out.log", "one.err.log"):
            (logs / name).write_text(warning)
        warnings = collect_diagnostics(logs)
        self.assertEqual(warnings["raw_total"], 2)
        self.assertEqual(warnings["unique_total"], 1)

    def test_tidy_diagnostics_match_multiline_warning(self):
        from clang_tidy.diagnostics import collect_diagnostics
        logs = self.folder / "multiline-logs"
        logs.mkdir()
        (logs / "one.log").write_text(
            "/project/header.hpp:12:3: warning: message starts here\n"
            "  and continues on another line [bugprone-integer-division]\n"
        )
        warnings = collect_diagnostics(logs)
        self.assertEqual(warnings["raw_total"], 1)
        self.assertEqual(warnings["unique_total"], 1)

    def test_main_without_database_keeps_cppcheck_but_marks_clang_unavailable(self):
        self.database.unlink()
        output = self.folder / "fallback-results"
        with patch.object(common, "find_tool", return_value=str(self.noop_tool)), \
                patch.object(cppcheck, "scan", return_value={"ok": True, "mode": "source"}), \
                patch("session.render_reports"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(session.run([self.source], output, SCANNERS), 1)
        manifest = json.loads(next(output.glob("*/manifest.json")).read_text())
        results = manifest["groups"][0]["results"]
        self.assertTrue(results["cpp_check"]["ok"])
        self.assertEqual(results["clang_tidy"]["status"], "unavailable")
        self.assertEqual(results["clang_static_analyzer"]["status"], "unavailable")

    def test_report_failure_is_not_silently_successful(self):
        output = self.folder / "report-failure"
        with patch.object(common, "find_tool", return_value=str(self.noop_tool)), \
                patch.object(cppcheck, "scan", return_value={"ok": True}), \
                patch("session.render_reports", side_effect=ValueError("HTML failed")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(session.run([self.source], output, (cppcheck,)), 1)
        manifest = json.loads(next(output.glob("*/manifest.json")).read_text())
        self.assertEqual(manifest["groups"][0]["report_error"], "HTML failed")

    def test_csa_bug_status_is_restored(self):
        from clang_static_analyzer.reporting import generate
        output = self.folder / "csa-summary"
        output.mkdir()
        result = {"ok": True, "total": 1, "analyzed": 1,
                  "files": [{"file": str(self.first), "unit_id": "unit", "ok": True, "diagnostics": 1}]}
        generate(output, result, {}, [], [], {}, {})
        self.assertEqual(result["status"], "issues")
        self.assertTrue(result["ok"], "a parsed CSA finding is a successful completed scan, not a tool failure")
        self.assertTrue((output / "index.html").is_file())

    def test_csa_execution_failure_stays_failed_when_other_units_have_findings(self):
        from clang_static_analyzer.reporting import generate
        output = self.folder / "csa-partial-summary"
        output.mkdir()
        result = {"ok": False, "total": 2, "analyzed": 1,
                  "files": [
                      {"file": str(self.first), "unit_id": "unit", "ok": True, "diagnostics": 1},
                      {"file": str(self.second), "unit_id": "failed", "ok": False, "diagnostics": 0},
                  ]}
        generate(output, result, {}, [], [], {}, {})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["reports"], 1)
        self.assertFalse(result["ok"])

    def test_tidy_severity_and_remediation_are_populated(self):
        from types import SimpleNamespace
        from clang_tidy.severity import generate
        output = self.folder / "tidy-severity"
        (output / "logs").mkdir(parents=True)
        (output / "fixes").mkdir()
        warning = str(self.first) + ':12:3: warning: use after move [bugprone-use-after-move]\n'
        (output / "logs/one.log").write_text(warning)
        (output / "logs/two.log").write_text(warning)
        with contextlib.redirect_stdout(io.StringIO()):
            generate(SimpleNamespace(result_dir=str(output), project_root=str(self.project),
                                     output_md=str(output / "severity.md"), output_csv=str(output / "severity.csv")))
        import csv
        with (output / "severity.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["severity"], "P0")
        self.assertEqual(rows[0]["ownership"], "project")
        self.assertTrue(rows[0]["remediation"])

    def test_tidy_dashboard_whole_root_counts(self):
        from clang_tidy.dashboard import build_directory_stats
        stats = build_directory_stats([{"file": "src/a.cpp", "status": "ok", "warning_count": 2,
                                        "check_diagnostics": 2, "error_count": 0}])
        self.assertEqual(stats["."]["files"], 1)
        self.assertEqual(stats["."]["warnings"], 2)

    def test_report_helpers_do_not_expose_scanner_parameter_cli(self):
        helpers = ["reporting.py", "support.py", "common.py", "session.py", "registry.py",
                   "clang_tidy/diagnostics.py", "clang_tidy/severity.py", "clang_tidy/dashboard.py"]
        helpers.extend(str(path.relative_to(ROOT / "scan")) for pattern in ("*/scanner.py", "*/reporting.py")
                       for path in (ROOT / "scan").glob(pattern))
        for helper in helpers:
            completed = subprocess.run([sys.executable, str(ROOT / "scan" / helper), "--jobs", "100"], capture_output=True)
            self.assertNotEqual(completed.returncode, 0, helper)

    def test_zero_exit_without_cppcheck_xml_is_a_failure(self):
        database, _, entries = common.plan([self.first])[0]
        receipt = cppcheck.scan(entries, [self.source], database, self.folder / "no-xml",
                                {"cppcheck": str(self.noop_tool)})
        self.assertFalse(receipt["ok"])
        self.assertIn("artifact_error", receipt)

    def test_malformed_plist_is_reported_without_crashing(self):
        database, scopes, entries = common.plan([self.first])[0]

        def invalid_artifact(command, cwd, stdout, stderr):
            artifact = Path(command[command.index("-o") + 1])
            artifact.write_text('<?xml version="1.0"?><plist>broken')
            return {"exit_code": 0}

        with patch.object(common, "execute", side_effect=invalid_artifact):
            receipt = csa.scan(entries, scopes, database,
                                       self.folder / "invalid-plist", {"clang": "clang", "clang++": "clang++"})
        self.assertFalse(receipt["ok"])
        self.assertIn("artifact_error", receipt["files"][0])

    def test_cppcheck_truncated_xml_handled_gracefully(self):
        from cpp_check.reporting import export_evidence
        output = self.folder / "truncated-cppcheck-output"
        output.mkdir()
        (output / "selected-tus.csv").write_text("file,status\n/dummy/file.cpp,selected\n")
        target = self.folder / "truncated-target"
        target.mkdir()
        meta = target / "01-metadata"
        meta.mkdir()
        (meta / "cppcheck.version.stdout.txt").write_text("Cppcheck 2.22.0\n")
        stderr_file = output / "stderr.log"
        stderr_file.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<results version="2">\n<errors><error id="broken"')
        result = {"exit_code": 124, "timed_out": True, "stderr": str(stderr_file), "command": ["cppcheck"]}
        validation = export_evidence(output, target, result)
        self.assertIn("errors", validation)
        self.assertTrue(any("parse error" in err for err in validation["errors"]))


if __name__ == "__main__":
    unittest.main()
