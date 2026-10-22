import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import common
import session
from registry import SCANNERS


class ToolStructureTests(unittest.TestCase):
    def test_shared_layer_has_no_analyzer_commands_or_tool_policies(self):
        forbidden = {"--analyze", "-analyzer-output=plist", "-analyzer-output=html", "--inconclusive",
                     "--export-fixes=", "--enable=all", "-Wno-non-pod-varargs", "clang-analyzer-*"}
        for name in ("runner.py", "session.py", "common.py", "reporting.py"):
            tree = ast.parse((ROOT / "scan" / name).read_text())
            values = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                      and isinstance(node.value, str)}
            self.assertFalse(values & forbidden, (name, values & forbidden))
            self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name in
                                 ("run_units", "run_cppcheck", "tidy_report", "cppcheck_report", "analyzer_report")
                                 for node in ast.walk(tree)), name)

    def test_each_tool_owns_its_scan_report_and_policy(self):
        for scanner in SCANNERS:
            directory = ROOT / "scan" / scanner.NAME
            self.assertEqual(Path(scanner.__file__).parent, directory)
            self.assertEqual(Path(scanner.reporting.__file__).parent, directory)
            self.assertEqual(scanner.scan.__module__, scanner.__name__)
            self.assertEqual(scanner.report.__module__, scanner.__name__)
            self.assertEqual(scanner.POLICY["jobs"], 4)
            self.assertEqual(scanner.SUPPORTS_SOURCE, scanner.NAME == "cpp_check")

    def test_single_tool_does_not_import_main_or_other_analyzers(self):
        for scanner in SCANNERS:
            command = [sys.executable, "-c", "import sys,importlib,json; sys.path.insert(0,sys.argv[1]); "
                       "importlib.import_module(sys.argv[2]+'.entry'); "
                       "print(json.dumps(sorted(sys.modules)))", str(ROOT / "scan"), scanner.NAME]
            completed = subprocess.run(command, text=True, capture_output=True, check=True)
            modules = json.loads(completed.stdout)
            self.assertIn(scanner.NAME + ".scanner", modules)
            self.assertNotIn("runner", modules)
            self.assertNotIn("registry", modules)
            for other in SCANNERS:
                if other != scanner:
                    self.assertFalse(any(module == other.NAME or module.startswith(other.NAME + ".")
                                         for module in modules), (scanner.NAME, other.NAME))

    def test_subentry_works_when_main_runner_and_registry_are_unavailable(self):
        script = '''import importlib.abc, runpy, sys
class RejectMain(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ("runner", "registry"):
            raise ImportError("main orchestration unavailable")
sys.meta_path.insert(0, RejectMain())
entry = sys.argv[1]
sys.argv = [entry, "--help"]
runpy.run_path(entry, run_name="__main__")
'''
        for scanner in SCANNERS:
            completed = subprocess.run([sys.executable, "-c", script,
                                        str(ROOT / "scan" / scanner.NAME / "entry.py")],
                                       text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("--scan-path", completed.stdout)

    def test_session_dispatch_is_generic_not_a_hidden_tool_switch(self):
        calls = []
        scanners = []
        for name in ("first_custom_tool", "second_custom_tool"):
            def scan(entries, scopes, database, output, binaries, name=name):
                calls.append((name, "scan"))
                output.mkdir(parents=True)
                return {"ok": True}

            def report(result, group, output, entries, scopes, binaries, name=name):
                calls.append((name, "report"))
                common.page(output / "index.html", name, "custom report")

            scanners.append(SimpleNamespace(NAME=name, POLICY={"jobs": 4}, SUPPORTS_SOURCE=True,
                                            REPORT_PRIORITY=0, resolve_tools=lambda: {}, scan=scan, report=report))
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / "source.c"
            source.write_text("int main(void) { return 0; }\n")
            common.write_json(folder / "compile_commands.json", [{"directory": str(folder), "file": str(source),
                              "arguments": ["cc", "-c", str(source)]}])
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(session.run([source], folder / "results", scanners), 0)
            self.assertEqual(calls, [("first_custom_tool", "scan"), ("second_custom_tool", "scan"),
                                     ("first_custom_tool", "report"), ("second_custom_tool", "report")])
            manifest = json.loads(next((folder / "results").glob("*/manifest.json")).read_text())
            self.assertEqual(set(manifest["policy"]["tools"]), {scanner.NAME for scanner in scanners})


if __name__ == "__main__":
    unittest.main()
