import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import common


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
                    self.assertTrue(path.is_relative_to(vendor.resolve()))
                    self.assertRegex(expected, r"^[0-9a-f]{64}$")
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected, relative)
                self.assertFalse((vendor / ".git").exists())

    def test_csharp_sample_is_explicitly_rejected_by_the_cpp_runner(self):
        source = ROOT / "examples/csharp/GuardClauses/src/GuardClauses/Guard.cs"
        self.assertTrue(source.is_file())
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            output = folder / "must-not-be-created"
            with self.assertRaisesRegex(ValueError, "只支持 C/C\\+\\+"):
                common.inputs(["--scan-path", str(source), "--result-path", str(output)],
                              config_path=folder / "absent.config")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
