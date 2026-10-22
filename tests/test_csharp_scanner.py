import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import common
import session
from csharp_roslyn import reporting, scanner
from registry import CSHARP_SCANNERS, SCANNERS


class CSharpScannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.project = self.folder / "SampleApp"
        self.project.mkdir()
        self.csproj = self.project / "SampleApp.csproj"
        self.csproj.write_text("<Project Sdk=\"Microsoft.NET.Sdk\"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n")
        self.cs_file = self.project / "Program.cs"
        self.cs_file.write_text("class Program { static void Main() {} }\n")

    def tearDown(self):
        self.temporary.cleanup()

    def make_fake_dotnet(self, sarif_results=None, exit_code=0):
        tool = self.folder / "fake-dotnet"
        results = sarif_results if sarif_results is not None else []
        sarif_content = {
            "version": "2.1.0",
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{
                "tool": {"driver": {"name": "Microsoft.CodeAnalysis.NetAnalyzers", "version": "8.0.0"}},
                "results": results
            }]
        }
        script = f"""#!{sys.executable}
import json, sys
from pathlib import Path

error_log = next((item.split("=", 1)[1] for item in sys.argv if item.startswith("-p:ErrorLog=")), None)
if error_log:
    path, _ = error_log.rsplit(",", 1)
    Path(path).write_text(json.dumps({json.dumps(sarif_content)}))
sys.exit({exit_code})
"""
        tool.write_text(script)
        tool.chmod(0o755)
        return str(tool)

    def test_scanner_attributes_and_policy(self):
        self.assertEqual(scanner.NAME, "csharp_roslyn")
        self.assertTrue(scanner.SUPPORTS_SOURCE)
        self.assertEqual(scanner.INPUT_SUFFIXES, {".cs"})
        self.assertEqual(scanner.REQUIRED_TOOLS, ("dotnet",))
        self.assertIn("analyzers", scanner.POLICY)
        self.assertIn("flags", scanner.POLICY)

    def test_find_project_locates_csproj(self):
        found = scanner.find_project([{"file": str(self.cs_file)}], [self.project])
        self.assertEqual(found, self.csproj.resolve())

    def test_scan_and_report_with_clean_sarif(self):
        fake_dotnet = self.make_fake_dotnet(sarif_results=[])
        output = self.folder / "scan_out"
        entries = [{"file": str(self.cs_file)}]
        receipt = scanner.scan(entries, [self.project], None, output, {"dotnet": fake_dotnet})

        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["diagnostics"], 0)
        self.assertTrue((output / reporting.SARIF_NAME).is_file())
        self.assertTrue((output / reporting.RECONCILIATION_NAME).is_file())
        self.assertTrue((output / "receipt.json").is_file())

        scanner.report(receipt, {}, output, entries, [self.project], {"dotnet": fake_dotnet})
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["diagnostics"], 0)
        self.assertTrue((output / "index.html").is_file())
        self.assertTrue((output / "index.md").is_file())

    def test_scan_with_security_finding(self):
        finding = {
            "ruleId": "CA3001",
            "level": "error",
            "message": {"text": "Potential SQL injection vulnerability"},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": "Program.cs"},
                "region": {"startLine": 10, "startColumn": 5}
            }}]
        }
        fake_dotnet = self.make_fake_dotnet(sarif_results=[finding])
        output = self.folder / "scan_out_finding"
        entries = [{"file": str(self.cs_file)}]
        receipt = scanner.scan(entries, [self.project], None, output, {"dotnet": fake_dotnet})

        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["diagnostics"], 1)

        scanner.report(receipt, {}, output, entries, [self.project], {"dotnet": fake_dotnet})
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["status"], "issues")
        self.assertEqual(summary["by_rule"], {"CA3001": 1})
        self.assertEqual(summary["by_level"], {"error": 1})

    def test_export_evidence_bundles_raw_and_receipts(self):
        fake_dotnet = self.make_fake_dotnet(sarif_results=[])
        output = self.folder / "scan_out_evidence"
        entries = [{"file": str(self.cs_file)}]
        receipt = scanner.scan(entries, [self.project], None, output, {"dotnet": fake_dotnet})
        scanner.report(receipt, {}, output, entries, [self.project], {"dotnet": fake_dotnet})

        bundle = self.folder / "evidence-bundle"
        meta = bundle / "01-metadata"
        meta.mkdir(parents=True)
        (meta / "dotnet.version.stdout.txt").write_text(".NET SDK 8.0.100\n")
        evidence_result = scanner.export_evidence(output, bundle, receipt)

        self.assertEqual(evidence_result["errors"], [])
        self.assertTrue((bundle / "06-csharp-roslyn/result/csharp-report.sarif").is_file())
        self.assertTrue((bundle / "06-csharp-roslyn/csharp-artifacts/command.txt").is_file())
        self.assertTrue((bundle / "06-csharp-roslyn/csharp-artifacts/version.txt").is_file())
        self.assertTrue((bundle / "06-csharp-roslyn/csharp-artifacts/csharp-reconciliation.csv").is_file())

    def test_parent_compile_commands_does_not_block_csharp_scan(self):
        # Finding 1: ancestor dir has C++ compile_commands.json
        parent_compdb = self.folder / "compile_commands.json"
        parent_compdb.write_text(json.dumps([{"directory": str(self.folder), "file": "dummy.cpp", "arguments": ["g++", "-c", "dummy.cpp"]}]))
        
        fake_dotnet = self.make_fake_dotnet(sarif_results=[])
        output = self.folder / "scan_out_mixed_parent"
        entries = [{"file": str(self.cs_file)}]
        receipt = scanner.scan(entries, [self.project], None, output, {"dotnet": fake_dotnet})
        self.assertTrue(receipt["ok"])

    def test_multiple_independent_projects_are_rejected(self):
        # Finding 2: multiple disparate projects must fail-closed rather than silently dropping
        other_project = self.folder / "OtherApp"
        other_project.mkdir()
        (other_project / "OtherApp.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk\"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n")
        other_cs = other_project / "Other.cs"
        other_cs.write_text("class Other {}\n")

        with self.assertRaisesRegex(ValueError, "输入包含多个独立的 C# 项目文件"):
            scanner.find_project([{"file": str(self.cs_file)}, {"file": str(other_cs)}], [self.project, other_project])

    def test_entry_and_cli_help(self):
        entry_script = ROOT / "scan/csharp_roslyn/entry.py"
        completed = subprocess.run([sys.executable, str(entry_script), "--help"],
                                   text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--scan-path", completed.stdout)
        self.assertIn("--result-path", completed.stdout)


if __name__ == "__main__":
    unittest.main()
