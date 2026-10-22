import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import common


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


if __name__ == "__main__":
    unittest.main()
