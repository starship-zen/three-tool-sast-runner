import csv
import hashlib
import html
import json
import shlex
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse
from common import link, page, write_json

SARIF_NAME = "csharp-report.sarif"
RECONCILIATION_NAME = "csharp-reconciliation.csv"
PER_FRAMEWORK_FOLDER = "per-tfm"


def sarif_name(framework):
    return "csharp-report." + framework + ".sarif"


def rule_definitions(run):
    tool = run.get("tool")
    driver = tool.get("driver") if isinstance(tool, dict) else None
    rules = driver.get("rules") if isinstance(driver, dict) else None
    return rules if isinstance(rules, list) else []


def read_runs(path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(document, dict) or document.get("version") != "2.1.0"
            or not isinstance(document.get("runs"), list) or not document["runs"]):
        raise ValueError("SARIF 报告必须为 2.1.0 版本且包含非空 runs 数组: " + str(path))
    return document["runs"]


def write_report(path, runs):
    """Concatenate the compiler's per-framework runs into one SARIF 2.1.0 report.

    Each run keeps the compiler's own tool driver, rules and results; only a
    `properties.targetFramework` marker is added so a run can be attributed to
    the target framework that produced it.
    """
    document = {"$schema": "https://json.schemastore.org/sarif-2.1.0.json",
                "version": "2.1.0", "runs": runs}
    Path(path).write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def artifact_path(artifact):
    uri = artifact.get("uri") if isinstance(artifact, dict) else None
    if not isinstance(uri, str) or not uri:
        return None
    parsed = urlparse(uri)
    return unquote(parsed.path) if parsed.scheme == "file" else unquote(uri)


def diagnostic(result, rules):
    if not isinstance(result, dict):
        raise ValueError("SARIF result 必须为对象")
    rule = result.get("rule") if isinstance(result.get("rule"), dict) else {}
    rule_id = result.get("ruleId") or rule.get("id")
    index = result.get("ruleIndex")
    if not isinstance(rule_id, str) or not rule_id:
        if type(index) is int and 0 <= index < len(rules) and isinstance(rules[index], dict):
            rule_id = rules[index].get("id")
    if not isinstance(rule_id, str) or not rule_id:
        raise ValueError("SARIF result 缺少 ruleId")
    level = result.get("level")
    if not isinstance(level, str) or not level:
        configuration = rule.get("defaultConfiguration")
        level = configuration.get("level") if isinstance(configuration, dict) else None
    if not isinstance(level, str) or not level:
        level = "warning"
    message = result.get("message") if isinstance(result.get("message"), dict) else {}
    text = message.get("text") or message.get("markdown")
    if not isinstance(text, str):
        raise ValueError("SARIF result 缺少 message.text")
    locations = []
    for location in result.get("locations") or []:
        physical = location.get("physicalLocation") if isinstance(location, dict) else None
        if not isinstance(physical, dict):
            physical = {}
        artifact = physical.get("artifactLocation")
        region = physical.get("region")
        locations.append({"file": artifact_path(artifact),
                          "line": (region or {}).get("startLine") if isinstance(region, dict) else None,
                          "column": (region or {}).get("startColumn") if isinstance(region, dict) else None})
    return {"rule_id": rule_id, "level": level, "message": text, "locations": locations}


def parse_sarif(path):
    diagnostics = []
    for run in read_runs(path):
        if not isinstance(run, dict) or not isinstance(run.get("results"), list):
            raise ValueError("SARIF run 必须包含 results 数组: " + str(path))
        rules = rule_definitions(run)
        diagnostics.extend(diagnostic(result, rules) for result in run["results"])
    return diagnostics


def location_text(location):
    if not location["file"]:
        return "unknown"
    if location["line"] is None:
        return location["file"]
    if location["column"] is None:
        return "{}:{}".format(location["file"], location["line"])
    return "{}:{}:{}".format(location["file"], location["line"], location["column"])


def build_rows(result):
    return [{"target_framework": build.get("target_framework"), "exit_code": build.get("exit_code"),
             "units_attempted": build.get("units_attempted"), "units_completed": build.get("units_completed"),
             "diagnostics": build.get("diagnostics"), "ok": bool(build.get("ok")),
             "started": build.get("started"), "finished": build.get("finished"),
             "error": build.get("error")}
            for build in result.get("builds") or []]


def generate(output, result, group, entries, scopes, policy, binaries):
    sarif = Path(result.get("sarif") or (output / SARIF_NAME))
    diagnostics = parse_sarif(sarif) if sarif.is_file() else []
    levels = Counter(item["level"] for item in diagnostics)
    rules = Counter(item["rule_id"] for item in diagnostics)
    files = sorted({location["file"] for item in diagnostics
                    for location in item["locations"] if location["file"]})
    status = "failed" if not result.get("ok") else "issues" if diagnostics else "ok"
    result.update(status=status, diagnostics=len(diagnostics))
    builds = build_rows(result)
    payload = {"ok": bool(result.get("ok")), "status": status, "project": result.get("project"),
               "sarif": sarif.name, "sarif_present": sarif.is_file(), "diagnostics": len(diagnostics),
               "by_level": dict(sorted(levels.items())), "by_rule": dict(sorted(rules.items())),
               "files": files, "exit_code": result.get("exit_code"), "error": result.get("error"),
               "artifact_error": result.get("artifact_error"), "policy": policy, "results": diagnostics,
               "target_frameworks": list(result.get("target_frameworks") or []),
               "analyzers": result.get("analyzers"), "builds": builds,
               "generated_at": datetime.now(timezone.utc).isoformat()}
    write_json(output / "summary.json", payload)
    rows = "".join("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
        html.escape(item["level"]), html.escape(item["rule_id"]),
        html.escape(", ".join(location_text(location) for location in item["locations"]) or "unknown"),
        html.escape(item["message"])) for item in diagnostics)
    build_cells = "".join("<tr><td>{}</td><td>{}</td><td>{}/{}</td><td>{}</td><td>{}</td></tr>".format(
        html.escape(str(build["target_framework"])), html.escape(str(build["exit_code"])),
        html.escape(str(build["units_completed"])), html.escape(str(build["units_attempted"])),
        html.escape(str(build["diagnostics"])), html.escape(str(build["ok"]))) for build in builds)
    page(output / "index.html", "C# Roslyn reports", "<p>Status: " + status + "; diagnostics: "
         + str(len(diagnostics)) + "; project: " + html.escape(str(result.get("project")))
         + "</p><table><tr><th>Level</th><th>Rule</th><th>Location</th><th>Message</th></tr>"
         + rows + "</table><table><tr><th>Target framework</th><th>Exit code</th><th>Units</th>"
         "<th>Diagnostics</th><th>Ok</th></tr>" + build_cells + "</table><p>"
         + link("summary.json") + "</p>")
    lines = ["# C# Roslyn Summary", "",
             "- Project: `{}`".format(result.get("project")), "- Status: " + status,
             "- Diagnostics: " + str(len(diagnostics)), "- Exit code: " + str(result.get("exit_code")), ""]
    analyzers = result.get("analyzers") if isinstance(result.get("analyzers"), dict) else {}
    lines.extend(["- Analyzer: `" + str(analyzers.get("name")) + "` "
                  + (str(analyzers.get("version")) if analyzers.get("version") else "unknown"), ""])
    lines.extend(["| Target framework | Exit code | Units completed/attempted | Diagnostics | Ok |",
                  "| --- | --- | --- | --- | --- |"])
    lines.extend("| {} | {} | {}/{} | {} | {} |".format(
        build["target_framework"], build["exit_code"], build["units_completed"], build["units_attempted"],
        build["diagnostics"], build["ok"]) for build in builds)
    lines.extend(["", "| Level | Count |", "| --- | --- |"])
    lines.extend("| {} | {} |".format(key, value) for key, value in sorted(levels.items()))
    lines.extend(["", "| Level | Rule | Location | Message |", "| --- | --- | --- | --- |"])
    lines.extend("| {} | {} | {} | {} |".format(
        item["level"], item["rule_id"],
        ", ".join(location_text(location) for location in item["locations"]) or "unknown",
        item["message"].replace("|", "\\|").replace("\n", " ")) for item in diagnostics)
    (output / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def export_evidence(output, target, result):
    destination = target / "06-csharp-roslyn"
    raw = destination / "result"
    raw.mkdir(parents=True)
    errors = []
    sarif = Path(result.get("sarif") or (output / SARIF_NAME))
    if not sarif.is_file():
        errors.append("缺少 C# SARIF 报告: " + str(sarif))
    else:
        shutil.copy2(sarif, raw / SARIF_NAME)
        try:
            parse_sarif(raw / SARIF_NAME)
        except (OSError, ValueError) as error:
            errors.append("C# SARIF 报告无效: " + str(error))
    artifacts = destination / "csharp-artifacts"
    artifacts.mkdir()
    builds = result.get("builds") or []
    if not builds:
        errors.append("缺少 C# 逐目标框架构建收据")
    (artifacts / "command.txt").write_text(
        "\n".join(shlex.join(build.get("command", [])) for build in builds) + "\n", encoding="utf-8")
    (artifacts / "exit-code.txt").write_text(
        "".join("{} {}\n".format(build.get("target_framework"), build.get("exit_code"))
                for build in builds), encoding="utf-8")
    per_framework = artifacts / PER_FRAMEWORK_FOLDER
    per_framework.mkdir()
    # The compiler's own per-framework report is preserved byte for byte; the
    # delivered csharp-report.sarif is their run-by-run concatenation.
    for build in builds:
        source = Path(str(build.get("sarif") or ""))
        name = source.name
        if not name or not source.is_file():
            errors.append("缺少 " + str(build.get("target_framework")) + " 的 SARIF 报告: " + str(source))
            continue
        shutil.copy2(source, per_framework / name)
        if hashlib.sha256((per_framework / name).read_bytes()).hexdigest() != build.get("sarif_sha256"):
            errors.append("逐目标框架 SARIF 哈希与扫描收据不一致: " + name)
    version = target / "01-metadata" / "dotnet.version.stdout.txt"
    if version.is_file():
        shutil.copy2(version, artifacts / "version.txt")
    else:
        errors.append("缺少 dotnet 版本收据")
    for name in ("receipt.json", "summary.json"):
        if (output / name).is_file():
            shutil.copy2(output / name, artifacts / name)
    for path in sorted(output.glob("*.log")):
        shutil.copy2(path, artifacts / path.name)
    reconciliation = output / RECONCILIATION_NAME
    files = []
    if reconciliation.is_file():
        shutil.copy2(reconciliation, artifacts / RECONCILIATION_NAME)
        with reconciliation.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        analyzed = {row["file"] for row in rows if row["status"] == "analyzed"}
        selected = [str(unit) for unit in result.get("selected_files") or []]
        files = [unit for unit in selected if unit in analyzed]
        missing = [unit for unit in selected if unit not in analyzed]
        if missing:
            errors.append("以下选中源文件不是任何 TargetFramework 的编译单元: " + ", ".join(missing))
    else:
        errors.append("缺少 C# 对账 CSV")
    return {"files": files, "errors": errors}


if __name__ == "__main__":
    raise SystemExit("Internal tool report helper; use this tool's scan.sh.")
