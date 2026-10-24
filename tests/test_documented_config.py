import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import common
import handoff


class DocumentedConfigTests(unittest.TestCase):
    def test_shipped_template_is_accepted_by_the_actual_config_parser(self):
        template = ROOT / "analy.config.example"
        config = json.loads(template.read_text(encoding="utf-8"))
        self.assertEqual(set(config), {"scan_paths", "result_path"})
        self.assertIsInstance(config["scan_paths"], list)
        self.assertTrue(config["scan_paths"])
        self.assertTrue(all(isinstance(path, str) and path.strip() for path in config["scan_paths"]))
        self.assertIsInstance(config["result_path"], str)
        self.assertTrue(config["result_path"].strip())
        self.assertEqual(common.CONFIG, ROOT / "scan" / "analy.config")
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary).resolve()
            output = source / "results"
            paths, result = common.inputs(
                ["--scan-path", str(source), "--result-path", str(output)], config_path=template)
            self.assertEqual(paths, [source])
            self.assertEqual(result, output)

    def test_operator_guide_profile_examples_match_the_exporter_contract(self):
        guide = (ROOT / "docs" / "operator-guide.zh-CN.md").read_text(encoding="utf-8")
        template = json.loads((ROOT / "examples" / "runner-handoff" / "product-profile.example.json")
                              .read_text(encoding="utf-8"))
        handoff._validate_profile(template)

        marker = 'cat > "$WORK/sample-profile.json" <<\'JSON\'\n'
        self.assertEqual(guide.count(marker), 1)
        sample_text = guide.split(marker, 1)[1].split("\nJSON\n", 1)[0]
        sample = json.loads(sample_text)
        handoff._validate_profile(sample)
        self.assertEqual(sample["included_paths"], ["examples/cpp/**"])
        self.assertEqual(sample["unsupported_languages"], [])
        self.assertIn("uv run --frozen --offline python scan/deliver.py", guide)
        self.assertIn("--result-path", guide)
        self.assertIn("--output", guide)
        self.assertIn("本机执行最低版本", guide)
        self.assertIn("Cppcheck 2.22.0", guide)
        self.assertIn("联合验收", guide)


if __name__ == "__main__":
    unittest.main()
