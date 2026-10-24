import csv
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import common
from clang_static_analyzer import reporting, scanner


class CsaSinglePassTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="csa-single-pass-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.sources = [self.folder / "first.cpp", self.folder / "second.cpp"]
        self.entries = []
        for source in self.sources:
            source.write_text("int value() { return 0; }\n", encoding="utf-8")
            self.entries.append({"file": str(source), "directory": str(self.folder),
                                 "arguments": ["clang++", "-c", str(source)]})

    def scan(self, behavior, entries=None):
        calls = []

        def execute(command, cwd, stdout, stderr):
            calls.append(list(command))
            artifact = Path(command[command.index("-o") + 1])
            exit_code = behavior(command, artifact)
            return {"command": list(command), "cwd": str(cwd), "exit_code": exit_code,
                    "stdout": str(stdout), "stderr": str(stderr)}

        output = self.folder / "csa"
        with patch.object(common, "execute", side_effect=execute):
            result = scanner.scan(entries or self.entries, [self.folder], None, output,
                                  {"clang": "clang", "clang++": "clang++"})
        return output, result, calls

    @staticmethod
    def write_plist(path, diagnostics):
        with path.open("wb") as stream:
            plistlib.dump({"diagnostics": diagnostics}, stream)

    def test_zero_findings_use_one_isolated_plist_html_execution_per_unit(self):
        def successful_zero(command, artifact):
            self.assertIn("-analyzer-output=plist-html", command)
            self.write_plist(artifact, [])
            return 0

        output, result, calls = self.scan(successful_zero)

        self.assertEqual(scanner.POLICY["output"], ["plist-html"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), len(self.entries))
        self.assertTrue(all("-analyzer-output=plist-html" in call for call in calls))
        self.assertEqual(len({Path(call[call.index("-o") + 1]).parent for call in calls}), len(self.entries))
        self.assertEqual(len({Path(item["artifact"]).parent.name for item in result["files"]}), len(self.entries))
        for item in result["files"]:
            self.assertTrue(item["ok"])
            self.assertEqual(item["diagnostics"], 0)
            self.assertEqual(item["html_reports"], [])
            self.assertNotIn("html_execution", item)
            self.assertTrue(Path(item["artifact"]).is_file())
            self.assertFalse((output / "reports" / (item["unit_id"] + ".plist")).exists())

        reporting.generate(output, result, {}, [], [], {}, {})
        exported = self.folder / "delivery"
        receipt = reporting.export_evidence(output, exported, result)
        native_plists = sorted((exported / "05-clang-static-analyzer/result/csa-native").glob("*.plist"))
        self.assertEqual(receipt["errors"], [])
        self.assertEqual({path.name for path in native_plists}, {item["unit_id"] + ".plist" for item in result["files"]})
        self.assertEqual(len(list((exported / "05-clang-static-analyzer/result/compile-db/reports").glob("*/*.plist"))),
                         len(self.entries))

    def test_findings_keep_their_html_report_link(self):
        def successful_finding(command, artifact):
            self.write_plist(artifact, [{"description": "fixture diagnostic"}])
            (artifact.parent / ("report-" + artifact.parent.name + ".html")).write_text(
                "<!doctype html><p>finding</p>", encoding="utf-8")
            return 0

        output, result, calls = self.scan(successful_finding)
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), len(self.entries))
        for item in result["files"]:
            self.assertTrue(item["ok"])
            self.assertEqual(len(item["html_reports"]), 1)
            self.assertIn("reports/" + item["unit_id"] + "/report-" + item["unit_id"] + ".html",
                          item["html_reports"][0])

        reporting.generate(output, result, {}, [], [], {}, {})
        index = (output / "index.html").read_text(encoding="utf-8")
        for item in result["files"]:
            self.assertIn("reports/" + item["unit_id"] + "/report-" + item["unit_id"] + ".html", index)
            self.assertIn("reports/" + item["unit_id"] + "/" + item["unit_id"] + ".plist", index)

    def test_exported_analyzer_log_excludes_retry_attempt_streams(self):
        output = self.folder / "csa-retry-logs"
        logs = output / "logs"
        reports = output / "reports"
        logs.mkdir(parents=True)
        reports.mkdir()
        source = self.sources[0]
        unit_id = "retry-unit"
        artifact = reports / (unit_id + ".plist")
        self.write_plist(artifact, [])
        (logs / (unit_id + ".out.log")).write_text("final stdout\n", encoding="utf-8")
        (logs / (unit_id + ".err.log")).write_text("final stderr\n", encoding="utf-8")
        (logs / (unit_id + ".attempt-1.out.log")).write_text("partial warning from first attempt\n", encoding="utf-8")
        (logs / (unit_id + ".attempt-1.err.log")).write_text("partial error from first attempt\n", encoding="utf-8")
        (output / "analyzer-summary.md").write_text("# Summary\n", encoding="utf-8")
        common.write_json(output / "analyzer-status.json",
                          {"total": 1, "analyzed": 1, "failed": [], "reports": 0, "elapsed_seconds": 1})
        with (output / "tu-mapping.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("slug", "file", "diagnostics", "exit_code"))
            writer.writerow((unit_id, str(source), 0, 0))
        result = {"total": 1, "files": [{"file": str(source), "unit_id": unit_id,
                                          "exit_code": 0, "diagnostics": 0, "artifact": str(artifact)}]}

        target = self.folder / "retry-delivery"
        reporting.export_evidence(output, target, result)

        aggregate = target / "05-clang-static-analyzer/result/compile-db/analyzer.log"
        self.assertEqual(aggregate.read_text(encoding="utf-8"), "final stderr\n\nfinal stdout\n")

    def test_positive_plist_without_html_fails_closed(self):
        def missing_html(command, artifact):
            self.write_plist(artifact, [{"description": "fixture diagnostic"}])
            return 0

        _, result, calls = self.scan(missing_html, self.entries[:1])
        item = result["files"][0]
        self.assertFalse(result["ok"])
        self.assertFalse(item["ok"])
        self.assertIn("no HTML report", item["artifact_error"])
        self.assertEqual(len(calls), 1)

    def test_empty_html_output_fails_closed(self):
        def empty_html(command, artifact):
            self.write_plist(artifact, [{"description": "fixture diagnostic"}])
            (artifact.parent / "report-empty.html").write_text("", encoding="utf-8")
            return 0

        _, result, calls = self.scan(empty_html, self.entries[:1])
        item = result["files"][0]
        self.assertFalse(result["ok"])
        self.assertFalse(item["ok"])
        self.assertIn("HTML report output is invalid", item["artifact_error"])
        self.assertEqual(len(calls), 1)

    def test_malformed_plist_fails_without_second_execution(self):
        def malformed(command, artifact):
            artifact.write_bytes(b"<?xml version='1.0'?><plist><broken>")
            return 0

        _, result, calls = self.scan(malformed, self.entries[:1])
        self.assertFalse(result["ok"])
        self.assertFalse(result["files"][0]["ok"])
        self.assertIn("artifact_error", result["files"][0])
        self.assertEqual(len(calls), 1)

    def test_non_plist_bytes_fail_as_a_recorded_unit_failure(self):
        def non_plist(command, artifact):
            artifact.write_bytes(b"not a plist")
            return 0

        _, result, calls = self.scan(non_plist, self.entries[:1])
        item = result["files"][0]
        self.assertFalse(result["ok"])
        self.assertFalse(item["ok"])
        self.assertIn("artifact_error", item)
        self.assertEqual(len(calls), 1)

    def test_nonzero_analyzer_exit_fails_without_parsing_partial_output(self):
        def failed(command, artifact):
            return 2

        _, result, calls = self.scan(failed, self.entries[:1])
        item = result["files"][0]
        self.assertFalse(result["ok"])
        self.assertFalse(item["ok"])
        self.assertEqual(item["artifact_error"], "CSA analyzer exited with status 2")
        self.assertEqual(item["html_reports"], [])
        self.assertNotIn("diagnostics", item)
        self.assertEqual(len(calls), 1)

    def test_plist_must_have_a_diagnostics_array_of_dictionaries(self):
        def malformed_structure(command, artifact):
            self.write_plist(artifact, "no diagnostics array")
            return 0

        _, result, calls = self.scan(malformed_structure, self.entries[:1])
        item = result["files"][0]
        self.assertFalse(result["ok"])
        self.assertFalse(item["ok"])
        self.assertIn("diagnostics array", item["artifact_error"])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
