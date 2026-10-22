from pathlib import Path
import html
from common import link, page


def run_index(manifest, folder):
    rows = []
    for position, group in enumerate(manifest["groups"], 1):
        for tool, result in group["results"].items():
            target = Path("group-" + str(position)) / tool / "index.html"
            report = link(target, "Report") if (folder / target).exists() else html.escape(result.get("error", "Unavailable"))
            rows.append("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                position, tool, "ok" if result["ok"] and not result.get("report_error") else "failed/issues", report))
    bundles = "".join("<li>" + link(Path(group["evidence_bundle"]) / "manifest.json", "Group " + str(position) + " evidence")
                      + "; evidence checks: " + str(group.get("release_gate", {}).get("ok", False)) + "</li>"
                      for position, group in enumerate(manifest["groups"], 1) if group.get("evidence_bundle"))
    page(folder / "index.html", "Three-tool scan reports", "<p>Execution status: " + str(manifest["ok"])
         + "; evidence checks: " + str(manifest.get("evidence_checks_ok", False))
         + "; exit code: " + str(manifest.get("exit_code", "unknown"))
         + "; release ready: " + str(manifest.get("release_ready", False))
         + "; downstream ingestion not verified.</p><ul>" + bundles + "</ul><p>"
         + "</p><p>" + link("manifest.json") + "</p><table><tr><th>Group</th><th>Tool</th><th>Status</th>"
         "<th>Report</th></tr>" + "".join(rows) + "</table>")
    (folder / "index.md").write_text("# Three-tool scan reports\n\n- [HTML report index](index.html)\n"
                                    "- [Execution manifest](manifest.json)\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit("Internal run-index helper; use a scan.sh entry.")
