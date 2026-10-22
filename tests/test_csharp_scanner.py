import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import msbuild_fixture
from csharp_roslyn import reporting, scanner

FINDING = {
    "ruleId": "CA2100",
    "level": "error",
    "message": {"text": "Potential SQL injection vulnerability"},
    "locations": [{"physicalLocation": {
        "artifactLocation": {"uri": "Program.cs"},
        "region": {"startLine": 10, "startColumn": 5}
    }}]
}


class CSharpScannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name).resolve()
        self.project = self.folder / "SampleApp"
        self.project.mkdir()
        self.csproj = self.project / "SampleApp.csproj"
        self.csproj.write_text("<Project Sdk=\"Microsoft.NET.Sdk\"><PropertyGroup>"
                               "<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n")
        self.cs_file = self.project / "Program.cs"
        self.cs_file.write_text("class Program { static void Main() {} }\n")
        self.helper_file = self.project / "Helper.cs"
        self.helper_file.write_text("static class Helper { }\n")
        self.external_file = self.folder / "packages/NullableAttributes.cs"

    def tearDown(self):
        self.temporary.cleanup()

    def make_fake_dotnet(self, frameworks=("net8.0",), results=None, compile_items=None, **options):
        items = compile_items if compile_items is not None else {
            framework: [str(self.cs_file), str(self.helper_file)] for framework in frameworks}
        properties = {
            "TargetFrameworks": ";".join(frameworks) if len(frameworks) > 1 else "",
            "TargetFramework": frameworks[0] if len(frameworks) == 1 else "",
            "_NETAnalyzersSDKAssemblyVersion": "8.0.0",
        }
        properties.update(options.pop("properties", {}))
        emitted = results if results is not None else {framework: [] for framework in frameworks}
        return msbuild_fixture.write_fake_dotnet(self.folder / "bin", self.csproj, items, emitted,
                                                 properties=properties, **options)

    def run_scan(self, tool, name="scan_out", entries=None):
        output = self.folder / name
        declared = entries if entries is not None else [{"file": str(self.cs_file)},
                                                        {"file": str(self.helper_file)}]
        return scanner.scan(declared, [self.project], None, output, {"dotnet": tool}), output, declared

    @staticmethod
    def reconciliation(output):
        with (output / reporting.RECONCILIATION_NAME).open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

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

    def test_declared_target_frameworks_are_read(self):
        tool = self.make_fake_dotnet(("net8.0", "netstandard2.0"), compile_items={
            "net8.0": [str(self.cs_file), str(self.helper_file)],
            "netstandard2.0": [str(self.cs_file), str(self.helper_file)]})
        output = self.folder / "frameworks"
        output.mkdir()
        self.assertEqual(scanner.target_frameworks(tool, self.csproj, output),
                         ["net8.0", "netstandard2.0"])

    def test_missing_target_framework_fails_closed(self):
        tool = self.make_fake_dotnet(properties={"TargetFrameworks": "", "TargetFramework": ""})
        output = self.folder / "frameworks"
        output.mkdir()
        with self.assertRaisesRegex(ValueError, "未声明 TargetFramework"):
            scanner.target_frameworks(tool, self.csproj, output)

    def test_single_framework_counts_compile_units(self):
        tool = self.make_fake_dotnet()
        receipt, output, _entries = self.run_scan(tool, "scan_out_single")

        self.assertTrue(receipt["ok"], receipt.get("error"))
        self.assertEqual(receipt["target_frameworks"], ["net8.0"])
        [build] = receipt["builds"]
        self.assertEqual(build["target_framework"], "net8.0")
        self.assertEqual(build["compile_items"], sorted([str(self.cs_file), str(self.helper_file)]))
        self.assertEqual((build["units_attempted"], build["units_completed"]), (2, 2))
        self.assertEqual((receipt["units_attempted"], receipt["units_completed"]), (2, 2))
        self.assertEqual(receipt["discovered"], 0)
        self.assertIn("-p:TargetFramework=net8.0", build["command"])
        self.assertEqual(build["exit_code"], 0)
        self.assertTrue(build["started"])
        self.assertTrue(build["finished"])
        self.assertEqual(receipt["analyzers"]["name"], "Microsoft.CodeAnalysis.NetAnalyzers")
        self.assertEqual(receipt["analyzers"]["version"], "8.0.0")
        self.assertEqual(receipt["analyzers"]["source"], scanner.ANALYZER_SDK_SOURCE)
        self.assertEqual(receipt["analyzers"]["limitations"], [])

        self.assertTrue((output / reporting.SARIF_NAME).is_file())
        self.assertTrue((output / reporting.sarif_name("net8.0")).is_file())
        runs = reporting.read_runs(output / reporting.SARIF_NAME)
        self.assertEqual([run["properties"]["targetFramework"] for run in runs], ["net8.0"])
        rows = self.reconciliation(output)
        self.assertIn("tfm", rows[0])
        self.assertEqual(list(rows[0]), ["tfm", "file", "status"])
        self.assertEqual({(row["tfm"], row["file"], row["status"]) for row in rows},
                         {("net8.0", str(self.cs_file), "analyzed"),
                          ("net8.0", str(self.helper_file), "analyzed")})

    def test_each_target_framework_builds_and_reports_separately(self):
        frameworks = ("net8.0", "netstandard2.1", "netstandard2.0")
        compile_items = {framework: [str(self.cs_file), str(self.helper_file)] for framework in frameworks}
        # netstandard2.0 additionally compiles a package-provided source file.
        compile_items["netstandard2.0"].append(str(self.external_file))
        results = {"net8.0": [FINDING], "netstandard2.1": [], "netstandard2.0": [FINDING, FINDING]}
        tool = self.make_fake_dotnet(frameworks, results=results, compile_items=compile_items)
        receipt, output, _entries = self.run_scan(tool, "scan_out_multi")

        self.assertTrue(receipt["ok"], receipt.get("error"))
        self.assertEqual([build["target_framework"] for build in receipt["builds"]], list(frameworks))
        self.assertEqual([build["diagnostics"] for build in receipt["builds"]], [1, 0, 2])
        self.assertEqual([build["units_completed"] for build in receipt["builds"]], [2, 2, 3])
        self.assertEqual(receipt["units_attempted"], 7)
        self.assertEqual(receipt["units_completed"], 7)
        self.assertEqual(receipt["diagnostics"], 3)
        reports = [Path(build["sarif"]).name for build in receipt["builds"]]
        self.assertEqual(len(set(reports)), 3)
        for build in receipt["builds"]:
            self.assertIn("-p:TargetFramework=" + build["target_framework"], build["command"])
            self.assertTrue(Path(build["sarif"]).is_file())
        self.assertEqual(reports, [reporting.sarif_name(framework) for framework in frameworks])

        runs = reporting.read_runs(output / reporting.SARIF_NAME)
        self.assertEqual([run["properties"]["targetFramework"] for run in runs], list(frameworks))
        self.assertEqual([len(run["results"]) for run in runs], [1, 0, 2])
        self.assertEqual(len(reporting.parse_sarif(output / reporting.SARIF_NAME)), 3)
        rows = self.reconciliation(output)
        self.assertEqual(sum(row["status"] == "analyzed" for row in rows), 7)
        self.assertIn(("netstandard2.0", str(self.external_file), "analyzed"),
                      {(row["tfm"], row["file"], row["status"]) for row in rows})

    def test_compile_item_query_failure_is_fail_closed(self):
        tool = self.make_fake_dotnet(fail_item_query=True)
        receipt, output, _entries = self.run_scan(tool, "scan_out_items_failed")

        self.assertFalse(receipt["ok"])
        self.assertIn("Compile", receipt["error"])
        self.assertEqual((receipt["units_attempted"], receipt["units_completed"]), (0, 0))
        self.assertFalse((output / reporting.SARIF_NAME).exists())
        rows = self.reconciliation(output)
        self.assertNotIn("analyzed", {row["status"] for row in rows})
        self.assertEqual({row["status"] for row in rows}, {"discovered"})
        self.assertEqual(len(rows), 2)

    def test_empty_compile_item_inventory_is_fail_closed(self):
        tool = self.make_fake_dotnet(compile_items={"net8.0": []})
        receipt, output, _entries = self.run_scan(tool, "scan_out_empty_items")

        self.assertFalse(receipt["ok"])
        self.assertIn("未声明任何 Compile 编译单元", receipt["error"])
        self.assertEqual(receipt["units_attempted"], 0)
        self.assertFalse((output / reporting.SARIF_NAME).exists())
        self.assertEqual({row["status"] for row in self.reconciliation(output)}, {"discovered"})

    def test_missing_restore_assets_records_a_limitation(self):
        tool = self.make_fake_dotnet(write_assets=False,
                                     properties={"_NETAnalyzersSDKAssemblyVersion": ""})
        receipt, _output, _entries = self.run_scan(tool, "scan_out_no_assets")

        self.assertTrue(receipt["ok"], receipt.get("error"))
        self.assertIsNone(receipt["analyzers"]["version"])
        self.assertIsNone(receipt["analyzers"]["source"])
        self.assertTrue(receipt["analyzers"]["limitations"])
        self.assertIn("project.assets.json", " ".join(receipt["analyzers"]["limitations"]))

    def test_analyzer_version_comes_from_the_restore_assets(self):
        tool = self.make_fake_dotnet(assets_analyzer_version="9.0.1")
        receipt, _output, _entries = self.run_scan(tool, "scan_out_assets")

        self.assertEqual(receipt["analyzers"]["version"], "9.0.1")
        self.assertEqual(receipt["analyzers"]["source"], scanner.ANALYZER_ASSETS_SOURCE)
        self.assertEqual(receipt["analyzers"]["package_versions"], ["9.0.1"])
        self.assertEqual(receipt["analyzers"]["limitations"], [])

    def test_source_outside_every_target_framework_is_not_counted_as_analyzed(self):
        legacy = self.project / "Legacy/Old.cs"
        legacy.parent.mkdir()
        legacy.write_text("class Old { }\n")
        tool = self.make_fake_dotnet(compile_items={
            "net8.0": [str(self.cs_file), str(self.helper_file)]})
        receipt, output, _entries = self.run_scan(
            tool, "scan_out_legacy",
            entries=[{"file": str(self.cs_file)}, {"file": str(self.helper_file)},
                     {"file": str(legacy)}])

        self.assertFalse(receipt["ok"])
        self.assertIn("Old.cs", receipt["error"])
        self.assertEqual(receipt["units_completed"], 2)
        self.assertEqual(receipt["discovered"], 1)
        rows = self.reconciliation(output)
        self.assertIn(("", str(legacy), "discovered"),
                      {(row["tfm"], row["file"], row["status"]) for row in rows})
        self.assertNotIn(str(legacy), receipt["builds"][0]["compile_items"])

    def test_scan_and_report_with_clean_sarif(self):
        tool = self.make_fake_dotnet()
        receipt, output, entries = self.run_scan(tool, "scan_out_clean")
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["diagnostics"], 0)

        scanner.report(receipt, {}, output, entries, [self.project], {"dotnet": tool})
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["diagnostics"], 0)
        self.assertEqual(summary["target_frameworks"], ["net8.0"])
        self.assertEqual([build["target_framework"] for build in summary["builds"]], ["net8.0"])
        self.assertEqual(summary["analyzers"]["version"], "8.0.0")
        self.assertTrue((output / "index.html").is_file())
        self.assertTrue((output / "index.md").is_file())

    def test_scan_with_security_finding(self):
        tool = self.make_fake_dotnet(results={"net8.0": [FINDING]})
        receipt, output, entries = self.run_scan(tool, "scan_out_finding")
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["diagnostics"], 1)

        scanner.report(receipt, {}, output, entries, [self.project], {"dotnet": tool})
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["status"], "issues")
        self.assertEqual(summary["by_rule"], {"CA2100": 1})
        self.assertEqual(summary["by_level"], {"error": 1})

    def test_export_evidence_bundles_native_reports_and_receipts(self):
        tool = self.make_fake_dotnet(("net8.0", "netstandard2.0"), compile_items={
            "net8.0": [str(self.cs_file), str(self.helper_file)],
            "netstandard2.0": [str(self.cs_file), str(self.helper_file)]})
        receipt, output, entries = self.run_scan(tool, "scan_out_evidence")
        scanner.report(receipt, {}, output, entries, [self.project], {"dotnet": tool})

        bundle = self.folder / "evidence-bundle"
        meta = bundle / "01-metadata"
        meta.mkdir(parents=True)
        (meta / "dotnet.version.stdout.txt").write_text(".NET SDK 8.0.100\n")
        evidence_result = scanner.export_evidence(output, bundle, receipt)

        self.assertEqual(evidence_result["errors"], [])
        self.assertEqual(sorted(evidence_result["files"]), sorted([str(self.cs_file), str(self.helper_file)]))
        artifacts = bundle / "06-csharp-roslyn/csharp-artifacts"
        self.assertTrue((bundle / "06-csharp-roslyn/result/csharp-report.sarif").is_file())
        for framework in ("net8.0", "netstandard2.0"):
            self.assertTrue((artifacts / reporting.PER_FRAMEWORK_FOLDER
                             / reporting.sarif_name(framework)).is_file())
        self.assertTrue((artifacts / "version.txt").is_file())
        self.assertTrue((artifacts / "csharp-reconciliation.csv").is_file())
        self.assertEqual(len((artifacts / "command.txt").read_text().splitlines()), 2)
        self.assertEqual((artifacts / "exit-code.txt").read_text(), "net8.0 0\nnetstandard2.0 0\n")

    def test_export_evidence_reports_selected_files_no_framework_compiled(self):
        tool = self.make_fake_dotnet(compile_items={"net8.0": [str(self.cs_file)]})
        receipt, output, _entries = self.run_scan(tool, "scan_out_partial")
        bundle = self.folder / "partial-bundle"
        (bundle / "01-metadata").mkdir(parents=True)
        (bundle / "01-metadata/dotnet.version.stdout.txt").write_text(".NET SDK 8.0.100\n")

        evidence_result = scanner.export_evidence(output, bundle, receipt)

        self.assertEqual(evidence_result["files"], [str(self.cs_file)])
        self.assertTrue(any("不是任何 TargetFramework 的编译单元" in error
                            for error in evidence_result["errors"]))

    def test_parent_compile_commands_does_not_block_csharp_scan(self):
        # Finding 1: ancestor dir has C++ compile_commands.json
        parent_compdb = self.folder / "compile_commands.json"
        parent_compdb.write_text(json.dumps([{"directory": str(self.folder), "file": "dummy.cpp", "arguments": ["g++", "-c", "dummy.cpp"]}]))

        tool = self.make_fake_dotnet()
        receipt, _output, _entries = self.run_scan(tool, "scan_out_mixed_parent")
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
