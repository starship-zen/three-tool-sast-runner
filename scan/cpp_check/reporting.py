import csv
import json
import shutil
import sys
import shlex
import xml.etree.ElementTree as element_tree
from collections import Counter
from pathlib import Path
from common import ROOT, environment, link, page, source_root, write_json, execute


def generate(output, result, group, entries, scopes, policy, binaries):
    report = Path(result["stderr"])
    tree = element_tree.parse(report)
    errors = tree.findall(".//errors/error")
    counts = Counter(error.get("severity", "unknown") for error in errors)
    rules = Counter(error.get("id", "unknown") for error in errors)
    summary = {"mode": result["mode"], "diagnostics": len(errors), "by_severity": dict(counts),
               "diagnostic_count": len(errors), "by_rule": dict(rules), "xml_report": str(report), "xml_parse": "ok",
               "exit_code": result["exit_code"], "ok": result["ok"]}
    write_json(output / "summary.json", summary)
    (output / "summary.md").write_text("# Cppcheck Summary\n\nMode: " + result["mode"]
                                      + "\n\n| Severity | Count |\n| --- | --- |\n"
                                      + "".join("| {} | {} |\n".format(key, value)
                                                for key, value in sorted(counts.items())), encoding="utf-8")
    with (output / "selected-tus.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("file", "status", "reason"))
        selected = {entry["file"] for entry in entries}
        for entry in entries:
            writer.writerow((entry["file"], "selected", result["mode"]))
        if group["database"]:
            for entry in json.loads(Path(group["database"]).read_text(encoding="utf-8")):
                source = (Path(entry.get("directory") or Path(group["database"]).parent) / entry["file"]).resolve()
                if str(source) not in selected:
                    writer.writerow((str(source), "excluded", "outside_scope_or_thirdparty"))
    candidates = [shutil.which("cppcheck-htmlreport"), str(Path.home() / ".local/bin/cppcheck-htmlreport"),
                  str(Path(__file__).parent / "htmlreport/cppcheck-htmlreport")]
    binary = next((path for path in candidates if path and Path(path).is_file()), None)
    if not binary:
        raise ValueError("cppcheck-htmlreport missing; install the report helper or restore scan/cpp_check/htmlreport")
    target = output / "html"
    target.mkdir()
    command = [sys.executable, binary, "--file=" + str(report), "--report-dir=" + str(target),
               "--source-dir=" + str(source_root(scopes, group["database"]))]
    env = environment()
    env.update(PYTHONPATH=str(ROOT / "scan/cpp_check"), PYTHONDONTWRITEBYTECODE="1",
               CPPCHECK_HTML_ONLY_ALERT_LINES="1", CPPCHECK_HTML_CONTEXT_LINES="0")
    completed = execute(command, ROOT, output / "html-report.log", output / "html-report.err.log", env=env)
    result["html_execution"] = completed
    if completed["exit_code"] or not (target / "index.html").is_file():
        raise ValueError("Cppcheck HTML generation failed; see " + str(output / "html-report.log"))
    page(output / "index.html", "Cppcheck reports", "<ul>" + "".join("<li>" + link(name) + "</li>"
         for name in ("html/index.html", "summary.json", "summary.md", report.name, "selected-tus.csv")) + "</ul>")
    result["by_severity"] = dict(counts)
    write_json(output / "receipt.json", result)


def export_evidence(output, target, result):
    destination = target / "03-cppcheck"
    report_folder = destination / "result"
    report_folder.mkdir(parents=True)
    raw = Path(result["stderr"])
    shutil.copy2(raw, report_folder / "cppcheck-compile-report.xml")
    errors = []
    tree = element_tree.parse(raw)
    if tree.getroot().tag != "results" or tree.getroot().get("version") != "2" or tree.find("errors") is None:
        errors.append("Invalid Cppcheck XML v2")
    if (output / "html").is_dir():
        shutil.copytree(output / "html", report_folder / "cppcheck-compile-html")
    artifacts = destination / "cppcheck-artifacts"
    artifacts.mkdir()
    if (output / "cache-receipt.json").is_file():
        shutil.copy2(output / "cache-receipt.json", artifacts / "cache-receipt.json")
    if (output / "cache-key-inputs.json").is_file():
        shutil.copy2(output / "cache-key-inputs.json", artifacts / "cache-key-inputs.json")
    (artifacts / "command.txt").write_text(shlex.join(result["command"]) + "\n", encoding="utf-8")
    (artifacts / "exit-code.txt").write_text(str(result["exit_code"]) + "\n", encoding="utf-8")
    version = target / "01-metadata" / "cppcheck.version.stdout.txt"
    shutil.copy2(version, artifacts / "version.txt")
    for name in ("summary.json", "summary.md", "receipt.json", "stdout.log", "html-report.log", "html-report.err.log"):
        if (output / name).exists():
            shutil.copy2(output / name, artifacts / name)
    with (output / "selected-tus.csv").open(encoding="utf-8", newline="") as stream:
        files = [row["file"] for row in csv.DictReader(stream) if row["status"] == "selected"]
    with (artifacts / "input-files.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("file",))
        writer.writerows((name,) for name in files)
    if (output / "cppcheck-build").exists():
        shutil.copytree(output / "cppcheck-build", destination / "cppcheck-build")
    return {"files": files, "errors": errors}


if __name__ == "__main__":
    raise SystemExit("Internal tool report helper; use this tool's scan.sh.")
