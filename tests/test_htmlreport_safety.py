import os
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import quote
from xml.sax.saxutils import quoteattr


REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scan/cpp_check/htmlreport/cppcheck-htmlreport"
SHIM_PATH = REPO / "scan/cpp_check"


def escaped(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


class HTMLReportParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attributes = []
        self.text = []
        self.links = []
        self.current_link = None

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(name for name, _ in attrs if name)
        if tag == "a":
            self.current_link = [dict(attrs).get("href"), []]

    def handle_data(self, data):
        self.text.append(data)
        if self.current_link is not None:
            self.current_link[1].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current_link is not None:
            href, text = self.current_link
            self.links.append((href, "".join(text).strip()))
            self.current_link = None


class CppcheckHTMLReportSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_dir = self.root / "source"
        self.source_dir.mkdir()
        self.output_dir = self.root / "html"
        self.xml_path = self.root / "cppcheck.xml"

        self.hostile_filename = 'evil&"><svg onload=alert(1)>.cpp'
        self.ordinary_filename = "normal.cpp"
        self.title = 'Report &"><svg onload=alert(2)>'
        self.version = '2.22"><svg onload=alert(6)>'
        self.hostile_id = 'rule"><svg onload=alert(3)>'
        self.hostile_severity = 'warning" onmouseover="alert(4)'
        self.hostile_cwe = '120"><svg onload=alert(5)>'
        for filename in (self.hostile_filename, self.ordinary_filename):
            (self.source_dir / filename).write_text("int main() { return 0; }\n", encoding="utf-8")

        errors = [
            (self.hostile_filename, self.hostile_id, "warning", "120"),
            (self.ordinary_filename, "normalRule", "warning", "CWE-120"),
            (self.ordinary_filename, "severityRule", self.hostile_severity, self.hostile_cwe),
        ]
        xml_errors = "".join(
            '<error id={id} severity={severity} msg={message} cwe={cwe}>'
            '<location file={filename} line="1"/></error>'.format(
                id=quoteattr(rule_id), severity=quoteattr(severity),
                message=quoteattr("Synthetic finding & <safe text>"), cwe=quoteattr(cwe),
                filename=quoteattr(filename))
            for filename, rule_id, severity, cwe in errors)
        self.xml_path.write_text(
            '<results version="2"><cppcheck version={}/><errors>{}</errors></results>'.format(
                quoteattr(self.version), xml_errors),
            encoding="utf-8")

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(SHIM_PATH),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.env = env
        command = [
            sys.executable, str(HELPER), "--file=" + str(self.xml_path),
            "--report-dir=" + str(self.output_dir), "--source-dir=" + str(self.source_dir),
            "--title=" + self.title,
        ]
        self.completed = subprocess.run(
            command, cwd=self.source_dir, env=env, capture_output=True, text=True, timeout=30, check=False)

    def test_untrusted_xml_and_filename_values_are_escaped_in_all_html_views(self):
        self.assertEqual(self.completed.returncode, 0, self.completed.stdout + self.completed.stderr)
        pages = {
            name: (self.output_dir / name).read_text(encoding="utf-8")
            for name in ("index.html", "0.html", "stats.html")
        }

        for name, markup in pages.items():
            with self.subTest(page=name):
                self.assertIn(escaped(self.hostile_filename), markup)
                self.assertIn(escaped(self.title), markup)
                self.assertIn(escaped(self.version), markup)
                if name != "stats.html":
                    self.assertIn(escaped(self.hostile_id), markup)
                parser = HTMLReportParser()
                parser.feed(markup)
                self.assertNotIn("svg", parser.tags)
                unsafe_events = {attribute.lower() for attribute in parser.attributes
                                 if attribute.lower().startswith("on")
                                 and attribute.lower() not in {"onclick", "oninput", "onload"}}
                self.assertFalse(unsafe_events)
                self.assertIn(self.hostile_filename, "".join(parser.text))

        index = pages["index.html"]
        self.assertIn(escaped(self.hostile_severity), index)
        self.assertIn(escaped(self.hostile_cwe), index)
        index_parser = HTMLReportParser()
        index_parser.feed(index)
        self.assertIn(("1.html", self.ordinary_filename), index_parser.links)
        self.assertIn(("1.html#line-1", "1"), index_parser.links)
        cwe_href = "https://cwe.mitre.org/data/definitions/" + quote(self.hostile_cwe, safe="") + ".html"
        self.assertIn((cwe_href, self.hostile_cwe), index_parser.links)
        self.assertTrue((self.output_dir / "1.html").is_file())
        self.assertIn("int main()", pages.get("1.html", "") or
                      (self.output_dir / "1.html").read_text(encoding="utf-8"))

    def test_remote_source_links_encode_filename_path_and_keep_normal_links(self):
        remote_dir = self.root / "remote-html"
        base_url = "https://example.invalid/repo/blob/main"
        command = [
            sys.executable, str(HELPER), "--file=" + str(self.xml_path),
            "--report-dir=" + str(remote_dir), "--source-dir=" + base_url,
            "--title=" + self.title,
        ]
        completed = subprocess.run(
            command, cwd=self.source_dir, env=self.env, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

        expected = {
            (base_url + "/" + quote(self.hostile_filename, safe="/") + "#L1", self.hostile_filename),
            (base_url + "/" + quote(self.ordinary_filename, safe="/") + "#L1", self.ordinary_filename),
        }
        for name in ("index.html", "stats.html"):
            parser = HTMLReportParser()
            parser.feed((remote_dir / name).read_text(encoding="utf-8"))
            self.assertTrue(expected.issubset(set(parser.links)))
            self.assertNotIn("svg", parser.tags)
            self.assertFalse(any(attribute.lower() not in {"onclick", "oninput", "onload"}
                                 and attribute.lower().startswith("on") for attribute in parser.attributes))


if __name__ == "__main__":
    unittest.main()
