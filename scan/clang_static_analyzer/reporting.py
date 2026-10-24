import html
import csv
import plistlib
import shutil
import json
from pathlib import Path
from common import link, page, write_json


def export_evidence(output, target, result):
    destination = target / "05-clang-static-analyzer" / "result"
    destination.mkdir(parents=True)
    native = destination / "csa-native"
    native.mkdir()
    with (output / "tu-mapping.csv").open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["slug", "file", "diagnostics", "exit_code"]:
            raise ValueError("Invalid CSA mapping columns")
        rows = list(reader)
    errors = []
    if len({row["file"] for row in rows}) != len(rows) or len(rows) != result.get("total"):
        errors.append("CSA duplicate or missing TU mapping rows")
    receipts = {item["file"]: item for item in result.get("files", [])}
    for row in rows:
        item = receipts.get(row["file"], {})
        if (row["slug"] != item.get("unit_id") or row["exit_code"] != str(item.get("exit_code"))
                or row["diagnostics"] != str(item.get("diagnostics", ""))):
            errors.append("CSA mapping/receipt mismatch: " + row["file"])
    for item in result.get("files", []):
        artifact = Path(item["artifact"])
        if artifact.exists():
            shutil.copy2(artifact, native / artifact.name)
            with artifact.open("rb") as stream:
                diagnostics = plistlib.load(stream)["diagnostics"]
            if not isinstance(diagnostics, list) or len(diagnostics) != item.get("diagnostics"):
                errors.append("CSA native diagnostics mismatch: " + item["file"])
        else:
            errors.append("Missing CSA Plist: " + item["file"])
    if len(list(native.glob("*.plist"))) != len(rows):
        errors.append("CSA Plist/TU count mismatch")
    shutil.copy2(output / "tu-mapping.csv", destination / "tu-mapping.csv")
    shutil.copy2(output / "analyzer-summary.md", destination / "analyzer-summary.md")
    compilation = destination / "compile-db"
    shutil.copytree(output, compilation, ignore=shutil.ignore_patterns("reports"))
    shutil.copytree(output / "reports", compilation / "reports")
    (compilation / "analyzer-files.log").write_text("\n".join(row["file"] for row in rows) + "\n", encoding="utf-8")
    (compilation / "analyzer.log").write_text("\n".join(path.read_text(encoding="utf-8", errors="replace")
        for path in sorted((output / "logs").glob("*.log")) if ".attempt-" not in path.name), encoding="utf-8")
    status = json.loads((output / "analyzer-status.json").read_text())
    if not all(key in status for key in ("total", "analyzed", "failed", "reports", "elapsed_seconds")):
        errors.append("CSA status fields missing")
    return {"files": [row["file"] for row in rows], "errors": errors}


def generate(output, result, group, entries, scopes, policy, binaries):
    files = result.get("files", [])
    bugs = sum(item.get("diagnostics", 0) for item in files)
    failed = [item["file"] for item in files if not item["ok"]]
    status = "failed" if files and len(failed) == len(files) else "partial" if failed else "issues" if bugs else "ok"
    result.update(status=status, reports=bugs, failed=failed)
    lines = ["# Static Analyzer Summary", "", "- Engine: clang --analyze over compile_commands.json",
             "- Analyzed translation units: {}/{}".format(result.get("analyzed", 0), result.get("total", 0)),
             "- Bugs: " + str(bugs), "", "| Scope | Status | Bugs | Failed TUs | Analyzed | Report | Log |",
             "| --- | --- | --- | --- | --- | --- | --- |",
             "| `compile-db` | {} | {} | {} | {}/{} | `index.html` | `logs/` |".format(
                 status, bugs, len(failed), result.get("analyzed", 0), result.get("total", 0))]
    (output / "analyzer-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    rows = []
    for item in files:
        links = [link(Path(path).relative_to(output)) for path in item.get("html_reports", [])]
        artifact = Path(item.get("artifact", output / "reports" / (item["unit_id"] + ".plist")))
        links.append(link(artifact.relative_to(output), "Plist"))
        rows.append("<tr><td>" + html.escape(item["file"]) + "</td><td>"
                    + str(item.get("diagnostics", "unknown")) + "</td><td>" + " ".join(links) + "</td></tr>")
    page(output / "index.html", "Clang Static Analyzer reports", "<p>Status: " + status
         + "; bugs: " + str(bugs) + "</p><table><tr><th>File</th><th>Bugs</th><th>Reports</th></tr>"
         + "".join(rows) + "</table><p>" + link("analyzer-status.json") + "</p>")
    write_json(output / "analyzer-status.json", result)


if __name__ == "__main__":
    raise SystemExit("Internal tool report helper; use this tool's scan.sh.")
