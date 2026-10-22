import json
import sys
import tempfile
import unittest
import contextlib
import io
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import common
from clang_static_analyzer.dependencies import capture, parse_dependencies
from cpp_check.cache import build_cache
import evidence
import session
from cpp_check import scanner as cppcheck


class DependencyCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sast-inputs-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "source.c"
        self.source.write_text("int example;\n")
        self.binary = self.folder / "cppcheck"
        self.binary.write_text("tool-identity")
        self.entries = [{"file": str(self.source), "directory": str(self.folder),
                         "arguments": ["cc", "-c", str(self.source)]}]
        common.CANCELLED.clear()

    def output(self, name, baseline=None):
        output = self.folder / "results" / name / "group-1" / "cpp_check"
        output.mkdir(parents=True)
        if baseline is None:
            baseline = {"ok": True, "environment": common.environment(),
                        "files": {str(self.source): common.digest(self.source)},
                        "resolved_paths": {str(self.source): str(self.source.resolve())}}
        common.write_json(output.parent / "input-baseline.json", baseline)
        return output

    def scan(self, output, policy=None):
        with build_cache(output, self.entries, str(self.binary), policy or {}, self.folder / "db.json") as receipt:
            restored = (output / "cppcheck-build" / "native-cache").exists()
            (output / "cppcheck-build" / "native-cache").write_text("valid-cache")
            receipt["scan_ok"] = True
        return receipt, restored

    def test_cold_then_hot_and_run_snapshot_independent(self):
        first, restored = self.scan(self.output("first"))
        self.assertEqual(first["status"], "cold")
        self.assertFalse(restored)
        second_output = self.output("second")
        second, restored = self.scan(second_output)
        self.assertEqual(second["status"], "hit")
        self.assertTrue(restored)
        (Path(second["path"]) / "content/native-cache").write_text("later-change")
        self.assertEqual((second_output / "cppcheck-build/native-cache").read_text(), "valid-cache")

    def test_input_and_policy_and_binary_changes_invalidate(self):
        first, _ = self.scan(self.output("first"))
        self.source.write_text("int changed;\n")
        second, _ = self.scan(self.output("second"))
        third, _ = self.scan(self.output("third"), {"flags": ["--enable=all"]})
        self.binary.write_text("new-tool")
        fourth, _ = self.scan(self.output("fourth"))
        self.assertEqual(len({item["key"] for item in (first, second, third, fourth)}), 4)

    def test_corrupt_cache_rebuilds(self):
        first, _ = self.scan(self.output("first"))
        (Path(first["path"]) / "content/native-cache").write_text("corrupted")
        second, restored = self.scan(self.output("second"))
        self.assertEqual(second["status"], "rebuilt")
        self.assertFalse(restored)

    def test_symlink_cache_rejected(self):
        first, _ = self.scan(self.output("first"))
        content = Path(first["path"]) / "content"
        (content / "extra").symlink_to(self.source)
        second, _ = self.scan(self.output("second"))
        self.assertEqual(second["status"], "rebuilt")

    def test_missing_baseline_disables_persistent_cache(self):
        receipt, _ = self.scan(self.output("first", {"ok": False}))
        self.assertEqual(receipt["status"], "disabled")
        self.assertFalse(receipt["published"])

    def test_changed_during_scan_not_published(self):
        output = self.output("first")
        with build_cache(output, self.entries, str(self.binary), {}, self.folder / "db.json") as receipt:
            receipt["scan_ok"] = True
            self.source.write_text("int changed;\n")
        self.assertFalse(receipt["published"])

    def test_failure_not_published(self):
        output = self.output("first")
        with build_cache(output, self.entries, str(self.binary), {}, self.folder / "db.json") as receipt:
            receipt["scan_ok"] = False
        self.assertFalse(receipt["published"])

    def test_cancelled_scan_not_published_and_lock_released(self):
        output = self.output("first")
        with self.assertRaises(KeyboardInterrupt):
            with build_cache(output, self.entries, str(self.binary), {}, self.folder / "db.json") as receipt:
                receipt["scan_ok"] = True
                raise KeyboardInterrupt
        self.assertFalse(receipt["published"])
        second, _ = self.scan(self.output("second"))
        self.assertEqual(second["status"], "cold")

    def test_concurrent_runs_serialize_restore_and_publish(self):
        from concurrent.futures import ThreadPoolExecutor, TimeoutError
        from threading import Event
        entered = Event()
        release = Event()
        first_output = self.output("first")
        second_output = self.output("second")

        def first_scan():
            with build_cache(first_output, self.entries, str(self.binary), {}, self.folder / "db.json") as receipt:
                (first_output / "cppcheck-build/native-cache").write_text("valid-cache")
                entered.set()
                release.wait(5)
                receipt["scan_ok"] = True
            return receipt

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(first_scan)
            self.assertTrue(entered.wait(5))
            second = executor.submit(self.scan, second_output)
            try:
                with self.assertRaises(TimeoutError):
                    second.result(timeout=0.1)
            finally:
                release.set()
            self.assertTrue(first.result()["published"])
            self.assertEqual(second.result()[0]["status"], "hit")

    def test_header_content_and_symlink_target_invalidate(self):
        header = self.folder / "header.h"
        alternative = self.folder / "alternative.h"
        header.write_text("one")
        alternative.write_text("one")
        alias = self.folder / "alias.h"
        alias.symlink_to(header)

        def baseline():
            return {"ok": True, "environment": common.environment(),
                    "files": {str(path): common.digest(path) for path in (self.source, alias)},
                    "resolved_paths": {str(path): str(path.resolve()) for path in (self.source, alias)}}

        first, _ = self.scan(self.output("first", baseline()))
        alias.unlink()
        alias.symlink_to(alternative)
        second, _ = self.scan(self.output("second", baseline()))
        alternative.write_text("two")
        third, _ = self.scan(self.output("third", baseline()))
        self.assertEqual(len({item["key"] for item in (first, second, third)}), 3)

    def test_make_escaping_and_continuations(self):
        names = parse_dependencies("scanner-dependencies: source.c include/a\\ b.h " + "\\\n"
                                   + " include/dollar$$.h hash\\#file.h\n", self.folder)
        self.assertEqual(names, [str(self.folder / name) for name in
                                ("source.c", "include/a b.h", "include/dollar$.h", "hash#file.h")])

    def test_invalid_dependency_file_rejected(self):
        for text in ("", "other: a.c", "scanner-dependencies:"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_dependencies(text, self.folder)

    def check_evidence(self, before, after):
        folder = self.folder / "run" / "group-1"
        folder.mkdir(parents=True)
        database = self.folder / "compile_commands.json"
        common.write_json(database, self.entries)
        group = {"database": str(database), "selected_database": str(database),
                 "database_sha256": common.digest(database), "scan_paths": [str(self.source)],
                 "selected_files": [str(self.source)], "results": {},
                 "input_baseline": before, "input_baseline_after": after}
        manifest = {"versions": {}, "policy": {}}
        evidence.begin(group, folder, [self.source], self.folder / "results", manifest)
        evidence.finish(group, folder, [], manifest)
        return group["release_gate"]

    def test_dependency_change_fails_gate(self):
        before = {"ok": True, "files": {str(self.source): "before"}, "resolved_paths": {}, "environment": {}}
        after = dict(before, files={str(self.source): "after"})
        gate = self.check_evidence(before, after)
        self.assertIn("Compiler dependency closure or content changed during scan", gate["errors"])

    def test_failed_dependency_capture_fails_gate(self):
        gate = self.check_evidence({"ok": False}, {"ok": False})
        self.assertIn("Compiler dependency baseline unavailable or incomplete", gate["errors"])

    def test_missing_snapshot_fails_gate(self):
        baseline = {"ok": True, "files": {}, "resolved_paths": {}, "environment": {},
                    "snapshots": {"missing": "content/missing"}}
        gate = self.check_evidence(baseline, baseline)
        self.assertIn("Dependency snapshot missing or changed: content/missing", gate["errors"])

    def test_malformed_baseline_keeps_private_cache(self):
        output = self.output("first")
        (output.parent / "input-baseline.json").write_text("invalid-json")
        receipt, _ = self.scan(output)
        self.assertEqual(receipt["status"], "disabled")

    def test_cache_publication_failure_preserves_success(self):
        output = self.output("first")
        with patch.object(Path, "rename", side_effect=PermissionError("cache publication denied")):
            receipt, _ = self.scan(output)
        self.assertTrue(receipt["scan_ok"])
        self.assertFalse(receipt["published"])
        self.assertIn("cache publication denied", receipt["publication_error"])
        self.assertTrue((output / "cppcheck-build/native-cache").is_file())
        self.assertTrue(json.loads((output / "cache-receipt.json").read_text())["warnings"])
        second, _ = self.scan(self.output("second"))
        self.assertTrue(second["published"])

    def test_shared_cache_preparation_failure_uses_private_cache(self):
        output = self.output("first")
        with patch("cpp_check.cache.runtime_files", side_effect=PermissionError("cache setup denied")):
            receipt, restored = self.scan(output)
        self.assertEqual(receipt["status"], "disabled")
        self.assertFalse(restored)
        self.assertTrue(receipt["scan_ok"])
        self.assertIn("cache setup denied", receipt["reason"])

    def test_partial_cache_restore_failure_cleans_private_copy(self):
        import shutil
        self.scan(self.output("first"))
        output = self.output("second")
        original = shutil.copytree

        def fail_restore(source, destination, **kwargs):
            if Path(destination) == output / "cppcheck-build":
                (Path(destination) / "partial").write_text("corrupted")
                raise PermissionError("restore interrupted")
            return original(source, destination, **kwargs)

        with patch("cpp_check.cache.shutil.copytree", side_effect=fail_restore):
            receipt, _ = self.scan(output)
        self.assertFalse((output / "cppcheck-build/partial").exists())
        self.assertEqual(receipt["status"], "rebuilt")

    def test_cache_receipt_write_failure_preserves_success(self):
        output = self.output("first")
        original = common.write_json

        def write_json(path, value):
            if path.name == "cache-receipt.json":
                raise PermissionError("cache receipt denied")
            return original(path, value)

        with patch.object(common, "write_json", side_effect=write_json):
            receipt, _ = self.scan(output)
        self.assertTrue(receipt["scan_ok"])
        self.assertTrue(receipt["warnings"])

    def test_native_scan_exception_not_swallowed(self):
        output = self.output("first")
        with self.assertRaisesRegex(OSError, "native scan failed"):
            with build_cache(output, self.entries, str(self.binary), {}, self.folder / "db.json") as receipt:
                receipt["scan_ok"] = True
                raise OSError("native scan failed")
        self.assertFalse(receipt["published"])
        self.assertFalse(receipt["scan_ok"])

    def test_cppcheck_interface_keeps_result_after_publication_failure(self):
        output = self.output("first")
        output.rmdir()

        def execute(command, cwd, stdout, stderr):
            stdout.write_text("")
            stderr.write_text('<results version="2"><errors/></results>')
            return {"exit_code": 0, "stdout": str(stdout), "stderr": str(stderr)}

        with patch.object(common, "execute", side_effect=execute), \
                patch.object(Path, "rename", side_effect=PermissionError("publish denied")):
            result = cppcheck.scan(self.entries, [self.source], self.folder / "db.json", output,
                                   {"cppcheck": str(self.binary)})
        self.assertTrue(result["ok"])
        self.assertIn("publish denied", result["cache"]["publication_error"])
        self.assertTrue(json.loads((output / "receipt.json").read_text())["ok"])

    def run_capture_failure(self, failed_phase, summary_failure=False, copy_failure=False):
        database = self.folder / "compile_commands.json"
        common.write_json(database, self.entries)
        scanned = []
        exported = []

        def scan(entries, scopes, database, output, binaries):
            output.mkdir()
            scanned.append(output.name)
            return {"ok": True}

        def capture_inputs(entries, output, binaries):
            output.mkdir(parents=True)
            if output.name == failed_phase:
                raise PermissionError("dependency capture denied: " + failed_phase)
            return {"ok": True, "files": {}, "resolved_paths": {}, "snapshots": {}, "environment": {}}

        def export(output, target, result):
            exported.append(output.name)
            return {"files": [str(self.source)], "errors": []}

        scanners = [SimpleNamespace(NAME=name, POLICY={}, SUPPORTS_SOURCE=True, REPORT_PRIORITY=0,
                                    resolve_tools=lambda: {}, scan=scan, report=lambda *args: None,
                                    export_evidence=export)
                    for name in ("cpp_check", "clang_tidy", "clang_static_analyzer")]
        scanners[-1].capture_inputs = capture_inputs
        original = common.write_json

        def write_json(path, value):
            if summary_failure and path.name == "input-baseline.json":
                raise PermissionError("dependency summary denied")
            return original(path, value)

        original_copy = shutil.copytree

        def copytree(source, destination, *args, **kwargs):
            if copy_failure and Path(source).name == "input-baseline":
                raise PermissionError("dependency evidence copy denied")
            return original_copy(source, destination, *args, **kwargs)

        output = self.folder / "scan-results"
        with patch.object(common, "write_json", side_effect=write_json), \
                patch.object(shutil, "copytree", side_effect=copytree), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = session.run([self.source], output, scanners)
        manifest = json.loads(next(output.glob("*/manifest.json")).read_text())
        self.assertEqual(code, 3)
        self.assertTrue(manifest["ok"])
        self.assertFalse(manifest["evidence_checks_ok"])
        self.assertEqual(scanned, [scanner.NAME for scanner in scanners])
        self.assertEqual(set(exported), set(scanned))
        return manifest["groups"][0]

    def test_before_capture_exception_does_not_skip_any_analyzer(self):
        group = self.run_capture_failure("before")
        self.assertFalse(group["input_baseline"]["ok"])
        self.assertTrue(any("dependency capture denied: before" in error for error in group["release_gate"]["errors"]))

    def test_after_capture_exception_preserves_exports_and_execution(self):
        group = self.run_capture_failure("after")
        self.assertFalse(group["input_baseline_after"]["ok"])

    def test_dependency_summary_write_failure_does_not_skip_analyzers(self):
        group = self.run_capture_failure(None, summary_failure=True)
        self.assertFalse(group["input_baseline"]["ok"])

    def test_dependency_evidence_copy_failure_preserves_tool_exports(self):
        group = self.run_capture_failure(None, copy_failure=True)
        self.assertNotIn("evidence_error", group)
        self.assertIn("Dependency evidence copy failed: dependency evidence copy denied", group["release_gate"]["errors"])

    def test_real_compiler_headers_including_untracked_and_system(self):
        import shutil
        from clang_static_analyzer.scanner import compiler_arguments
        compiler = shutil.which("clang")
        if not compiler:
            self.skipTest("clang unavailable")
        header = self.folder / "untracked header.h"
        header.write_text("#define VALUE 1\n")
        self.source.write_text('#include "untracked header.h"\n#include <stddef.h>\nint example = VALUE;\n')
        result = capture(self.entries, self.folder / "before", {"clang": compiler}, compiler_arguments, [])
        self.assertTrue(result["ok"], result["errors"])
        self.assertIn(str(header), result["files"])
        self.assertGreater(len(result["files"]), 2)
        for digest, relative in result["snapshots"].items():
            self.assertEqual(common.digest(self.folder / "before" / relative), digest)
        header.write_text("#define VALUE 2\n")
        after = capture(self.entries, self.folder / "after", {"clang": compiler}, compiler_arguments, [])
        self.assertNotEqual(result["files"], after["files"])


if __name__ == "__main__":
    unittest.main()
