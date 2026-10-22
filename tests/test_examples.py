import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
import msbuild_fixture
from csharp_roslyn import reporting, scanner


class ExampleTests(unittest.TestCase):
    def test_vendored_files_match_the_pinned_upstream_manifests(self):
        examples = (
            ("cpp/tinyxml2", "9148bdf719e997d1f474be6bcc7943881046dba1", "LICENSE.txt"),
            ("csharp/GuardClauses", "ad43aa02babf3bc8aee8efc2258f5ad3571c8ec5", "LICENSE"),
        )
        for folder, commit, license_file in examples:
            with self.subTest(example=folder):
                vendor = ROOT / "examples" / folder
                manifest = json.loads((vendor / "UPSTREAM.json").read_text())
                self.assertEqual(manifest["commit"], commit)
                self.assertTrue(manifest["repository"].startswith("https://github.com/"))
                self.assertTrue(manifest["tag"])
                self.assertIn(license_file, manifest["files"])
                actual_files = {
                    str(path.relative_to(vendor)) for path in vendor.rglob("*")
                    if path.is_file() and path.name != "UPSTREAM.json"
                    and not {"bin", "obj", "build"}.intersection(path.relative_to(vendor).parts)
                }
                self.assertEqual(actual_files, set(manifest["files"]))
                for relative, expected in manifest["files"].items():
                    path = (vendor / relative).resolve()
                    try:
                        path.relative_to(vendor.resolve())
                        in_vendor = True
                    except ValueError:
                        in_vendor = False
                    self.assertTrue(in_vendor)
                    self.assertRegex(expected, r"^[0-9a-f]{64}$")
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected, relative)
                self.assertFalse((vendor / ".git").exists())

    def test_csharp_sample_routes_to_the_csharp_scanner_only(self):
        source = ROOT / "examples/csharp/GuardClauses/src/GuardClauses/Guard.cs"
        self.assertTrue(source.is_file())
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            paths, output = common.inputs(
                ["--scan-path", str(source), "--result-path", str(folder / "results")],
                config_path=folder / "absent.config")
            self.assertEqual(paths, [source.resolve()])
            with self.assertRaisesRegex(ValueError, "未匹配任何源文件"):
                common.plan(paths, source_fallback=True, suffixes=common.CPP_SOURCE_SUFFIXES)
            database, _, entries = common.plan(paths, source_fallback=True, suffixes=common.CSHARP_SUFFIXES)[0]
            self.assertIsNone(database)
            self.assertEqual([entry["file"] for entry in entries], [str(source.resolve())])
            tool = msbuild_fixture.write_fake_dotnet(
                folder, source.parent / "GuardClauses.csproj",
                {"net8.0": [str(path.resolve()) for path in sorted(source.parent.rglob("*.cs"))
                             if not {"bin", "obj"}.intersection(path.relative_to(source.parent).parts)]},
                {"net8.0": [{"ruleId": "CA2100", "level": "warning", "message": {"text": "Review SQL query"},
                             "locations": [{"physicalLocation": {"artifactLocation": {"uri": "Guard.cs"},
                                                                  "region": {"startLine": 1, "startColumn": 1}}}]}]},
                # The vendored example must not be mutated: this fixture must not
                # write restore assets into the repository checkout.
                write_assets=False)
            result_folder = output / "csharp_roslyn"
            receipt = scanner.scan(entries, paths, None, result_folder, {"dotnet": str(tool)})
            self.assertTrue(receipt["ok"], receipt.get("error"))
            self.assertEqual(receipt["project"], str((source.parent / "GuardClauses.csproj").resolve()))
            diagnostics = reporting.parse_sarif(result_folder / reporting.SARIF_NAME)
            self.assertEqual([item["rule_id"] for item in diagnostics], ["CA2100"])
            scanner.report(receipt, {}, result_folder, entries, paths, {"dotnet": str(tool)})
            summary = json.loads((result_folder / "summary.json").read_text())
            self.assertEqual((summary["status"], summary["diagnostics"], summary["by_rule"]), ("issues", 1, {"CA2100": 1}))
            self.assertTrue((result_folder / "index.html").is_file())
            self.assertTrue((result_folder / "index.md").is_file())


if __name__ == "__main__":
    unittest.main()
