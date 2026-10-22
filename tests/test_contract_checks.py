import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import common
import evidence
import session
from clang_static_analyzer.scanner import compiler_arguments
from clang_tidy.diagnostics import collect_diagnostics
from clang_tidy.reporting import validate_evidence, yaml_diagnostics


class ContractChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sast-contract-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "source.c"
        self.source.write_text("int example;\nint second;\n", encoding="utf-8")
        self.entry = {"arguments": ["cc", "-c", str(self.source)], "file": str(self.source), "directory": str(self.folder)}
        common.CANCELLED.clear()

    def arguments(self, *arguments):
        return compiler_arguments(dict(self.entry, arguments=["cc", *arguments, "-c", str(self.source)]))

    def test_preprocessor_forwarded_outputs_removed(self):
        self.assertEqual(self.arguments("-Xpreprocessor", "-MF", "-Xpreprocessor", "deps.d", "-Iinclude"), ["-Iinclude"])

    def test_preprocessor_joined_outputs_removed(self):
        self.assertEqual(self.arguments("-Xpreprocessor", "-MFdeps.d", "-Xpreprocessor", "-MMD", "-Iinclude"), ["-Iinclude"])

    def test_preprocessor_comma_outputs_removed_without_losing_macros(self):
        self.assertEqual(self.arguments("-Wp,-MMD,-MF,deps.d,-D,FEATURE=1,-I,include"), ["-Wp,-D,FEATURE=1,-I,include"])

    def test_preprocessor_md_filename_is_not_left_as_an_input(self):
        self.assertEqual(self.arguments("-Wp,-MD,deps.d,-D,FEATURE=1"), ["-Wp,-D,FEATURE=1"])
        self.assertEqual(self.arguments("-Xpreprocessor", "-MMD", "-Xpreprocessor", "deps.d"), [])

    def test_forwarded_serialized_diagnostics_output_removed(self):
        self.assertEqual(self.arguments("-Xclang", "-serialize-diagnostic-file", "-Xclang", "warnings.dia",
                                        "-Xclang", "-diagnostic-log-file=warnings.log", "-Iinclude"), ["-Iinclude"])

    def test_forwarded_values_not_reinterpreted(self):
        self.assertEqual(self.arguments("-Xpreprocessor", "-D", "-Xpreprocessor", "-MF"),
                         ["-Xpreprocessor", "-D", "-Xpreprocessor", "-MF"])

    def test_forwarded_context_retained_around_outputs(self):
        self.assertEqual(self.arguments("-Xclang", "-triple", "-Xclang", "x86_64-linux-gnu", "-Xclang", "-dependency-file",
                                        "-Xclang", "deps.d", "-Xclang", "-D", "-Xclang", "FEATURE=1"),
                         ["-Xclang", "-triple", "-Xclang", "x86_64-linux-gnu", "-Xclang", "-D", "-Xclang", "FEATURE=1"])

    def test_malformed_forwarding_fails_closed(self):
        for flags in (("-Xpreprocessor", "-MF"), ("-Wp,-MF",), ("-Xclang", "@args.rsp")):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                self.arguments(*flags)

    def test_output_options_without_values_fail(self):
        for flag in ("-o", "-MF", "-Xpreprocessor", "-Xlinker"):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                compiler_arguments(dict(self.entry, arguments=["cc", str(self.source), flag]))

    def test_layers_follow_producer_not_only_extension(self):
        expected = {"03-cppcheck/cppcheck-artifacts/summary.json": "DERIVED",
                    "04-clang-tidy/result/clang-tidy-summary.json": "RECEIPT",
                    "03-cppcheck/result/cppcheck-compile-report.xml": "RAW",
                    "04-clang-tidy/result/fixes/unit.yaml": "RAW",
                    "05-clang-static-analyzer/result/csa-native/unit.plist": "RAW",
                    "03-cppcheck/cppcheck-build/unit.xml": "CACHE",
                    "01-metadata/analy.config.original": "RECEIPT",
                    "01-metadata/unrelated.yaml": "RECEIPT",
                    "04-clang-tidy/result/severity.csv": "DERIVED",
                    "release-gate.json": "DERIVED"}
        for name, layer in expected.items():
            with self.subTest(name=name):
                self.assertEqual(evidence.artifact_layer(name), layer)

    def tidy_fixture(self):
        destination = self.folder / "tidy"
        (destination / "logs").mkdir(parents=True)
        (destination / "fixes").mkdir()
        identity = "unit"
        log = str(self.source) + ":1:5: warning: example [bugprone-example]\n"
        stdout = destination / "logs/unit.out.log"
        stderr = destination / "logs/unit.err.log"
        stdout.write_text("")
        stderr.write_text(log)
        (destination / "logs/unit.log").write_text("\n" + log)
        document = {"MainSourceFile": str(self.source), "Diagnostics": [{"DiagnosticName": "bugprone-example",
                    "DiagnosticMessage": {"FilePath": str(self.source), "FileOffset": 4, "Message": "example"}}]}
        fixes = destination / "fixes/unit.yaml"
        common.write_json(fixes, document)
        receipt = {"file": str(self.source), "unit_id": identity, "cwd": str(self.folder), "stdout": str(stdout),
                   "stderr": str(stderr), "ok": True, "exit_code": 0}
        result = {"total": 1, "files": [receipt]}
        payload = {"files": [dict(receipt, status="ok", unique_diagnostics=1, fixes="fixes/unit.yaml", log="logs/unit.log")],
                   "selected_file_count": 1, "warnings": collect_diagnostics(destination / "logs")}
        return destination, payload, result, document

    def test_tidy_native_identity_matches_log_and_summary(self):
        destination, payload, result, _ = self.tidy_fixture()
        self.assertEqual(validate_evidence(destination, payload, result)["errors"], [])

    def test_tidy_extra_native_diagnostic_rejected(self):
        destination, payload, result, document = self.tidy_fixture()
        document["Diagnostics"].append(copy.deepcopy(document["Diagnostics"][0]))
        common.write_json(destination / "fixes/unit.yaml", document)
        errors = validate_evidence(destination, payload, result)["errors"]
        self.assertTrue(any("identity or count mismatch" in error for error in errors), errors)

    def test_tidy_wrong_rule_or_location_rejected_even_with_same_count(self):
        for key, value in (("DiagnosticName", "bugprone-other"), ("FileOffset", 5)):
            temporary = tempfile.TemporaryDirectory(dir=self.folder)
            self.addCleanup(temporary.cleanup)
            previous = self.folder
            self.folder = Path(temporary.name)
            destination, payload, result, document = self.tidy_fixture()
            if key == "DiagnosticName":
                document["Diagnostics"][0][key] = value
            else:
                document["Diagnostics"][0]["DiagnosticMessage"][key] = value
            common.write_json(destination / "fixes/unit.yaml", document)
            errors = validate_evidence(destination, payload, result)["errors"]
            self.assertTrue(any("identity or count mismatch" in error for error in errors), errors)
            self.folder = previous

    def test_tidy_forged_summary_rejected(self):
        destination, payload, result, _ = self.tidy_fixture()
        payload["warnings"]["raw_total"] = 0
        payload["files"][0]["unique_diagnostics"] = 0
        errors = validate_evidence(destination, payload, result)["errors"]
        self.assertTrue(any("warning counts" in error for error in errors))
        self.assertTrue(any("per-TU" in error for error in errors))

    def test_header_warning_counts_against_the_translation_unit(self):
        destination, payload, result, document = self.tidy_fixture()
        header = self.folder / "shared.h"
        header.write_text("int example;\n")
        log = str(header) + ":1:5: warning: example [bugprone-example]\n"
        (destination / "logs/unit.err.log").write_text(log)
        (destination / "logs/unit.log").write_text("\n" + log)
        document["Diagnostics"][0]["DiagnosticMessage"]["FilePath"] = str(header)
        common.write_json(destination / "fixes/unit.yaml", document)
        payload["warnings"] = collect_diagnostics(destination / "logs")
        self.assertNotIn(str(self.source), payload["warnings"]["unique_by_file"])
        self.assertEqual(validate_evidence(destination, payload, result)["errors"], [])
        payload["files"][0]["unique_diagnostics"] = 0
        self.assertTrue(any("per-TU diagnostic count mismatch" in error
                            for error in validate_evidence(destination, payload, result)["errors"]))

    def test_tidy_raw_log_change_rejected(self):
        destination, payload, result, _ = self.tidy_fixture()
        (destination / "logs/unit.err.log").write_text("")
        errors = validate_evidence(destination, payload, result)["errors"]
        self.assertTrue(any("combined/raw log mismatch" in error for error in errors))

    def test_tidy_missing_native_for_warning_rejected(self):
        destination, payload, result, _ = self.tidy_fixture()
        (destination / "fixes/unit.yaml").unlink()
        self.assertTrue(any("Missing Clang-Tidy YAML" in error for error in validate_evidence(destination, payload, result)["errors"]))

    def test_tidy_wrong_main_source_rejected(self):
        destination, payload, result, document = self.tidy_fixture()
        document["MainSourceFile"] = str(self.folder / "other.c")
        common.write_json(destination / "fixes/unit.yaml", document)
        self.assertTrue(any("MainSourceFile mismatch" in error for error in validate_evidence(destination, payload, result)["errors"]))

    def test_tidy_duplicate_tu_summary_rejected(self):
        destination, payload, result, _ = self.tidy_fixture()
        payload["files"].append(copy.deepcopy(payload["files"][0]))
        self.assertTrue(any("TU count" in error for error in validate_evidence(destination, payload, result)["errors"]))

    def test_tidy_zero_diagnostics_needs_no_yaml(self):
        destination, payload, result, _ = self.tidy_fixture()
        for log in (destination / "logs").glob("*"):
            log.write_text("\n" if log.name == "unit.log" else "")
        (destination / "fixes/unit.yaml").unlink()
        payload["warnings"] = collect_diagnostics(destination / "logs")
        payload["files"][0]["unique_diagnostics"] = 0
        self.assertEqual(validate_evidence(destination, payload, result)["errors"], [])

    def test_yaml_positions_use_byte_offsets(self):
        source = self.folder / "unicode.c"
        source.write_text("// 中文\nint example;\n", encoding="utf-8")
        data = source.read_bytes()
        offset = data.index(b"example")
        path = self.folder / "unicode.yaml"
        common.write_json(path, {"MainSourceFile": str(source), "Diagnostics": [{"DiagnosticName": "check",
                         "DiagnosticMessage": {"FilePath": str(source), "FileOffset": offset}}]})
        self.assertEqual(yaml_diagnostics(path, self.folder, source), {(str(source), 2, 5, "check"): 1})

    def gate_run(self, gate_ok, single=False, execution_ok=True):
        common.write_json(self.folder / "compile_commands.json", [self.entry])
        def scan(entries, scopes, database, output, binaries):
            output.mkdir()
            return {"ok": execution_ok}
        scanners = [SimpleNamespace(NAME=name, POLICY={}, SUPPORTS_SOURCE=True, REPORT_PRIORITY=0,
                                    resolve_tools=lambda: {}, scan=scan, report=lambda *arguments: None)
                    for name in ("cpp_check", "clang_tidy", "clang_static_analyzer")]
        if single:
            scanners = scanners[:1]
        def finish(group, *arguments):
            group["release_gate"] = {"ok": gate_ok, "errors": [] if gate_ok else ["fixture gate failure"]}
        output = self.folder / "results"
        with patch.object(evidence, "begin"), patch.object(evidence, "finish", side_effect=finish), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = session.run([self.source], output, scanners)
        manifest = json.loads(next(output.glob("*/manifest.json")).read_text())
        return code, manifest

    def test_main_gate_failure_returns_three_without_claiming_execution_failed(self):
        code, manifest = self.gate_run(False)
        self.assertEqual(code, 3)
        self.assertTrue(manifest["ok"])
        self.assertTrue(manifest["evidence_gate_required"])
        self.assertFalse(manifest["release_ready"])
        self.assertEqual(manifest["exit_code"], 3)

    def test_main_evidence_checks_pass_returns_zero_but_not_release_certification(self):
        code, manifest = self.gate_run(True)
        self.assertEqual(code, 0)
        self.assertTrue(manifest["evidence_checks_ok"])
        self.assertFalse(manifest["release_ready"])
        self.assertFalse(manifest["downstream_compatible"])

    def test_single_tool_is_not_blocked_by_three_tool_gate(self):
        code, manifest = self.gate_run(False, single=True)
        self.assertEqual(code, 0)
        self.assertFalse(manifest["evidence_gate_required"])
        self.assertFalse(manifest["release_ready"])

    def test_execution_failure_takes_precedence_over_evidence_failure(self):
        code, manifest = self.gate_run(False, execution_ok=False)
        self.assertEqual(code, 1)
        self.assertFalse(manifest["ok"])


if __name__ == "__main__":
    unittest.main()
