import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from common import link, page, write_json, source_root
from .diagnostics import WARNING_RE, collect_diagnostics, write_summary
from .dashboard import render_html
from .severity import generate as severity_report


def generate(output, result, group, entries, scopes, policy, binaries):
    root = source_root(scopes, group["database"])
    files = []
    for item in result.get("files", []):
        identity = item["unit_id"]
        combined = output / "logs" / (identity + ".log")
        combined.write_text("\n".join(Path(item[key]).read_text(encoding="utf-8", errors="replace")
                                      for key in ("stdout", "stderr")), encoding="utf-8")
        try:
            name = str(Path(item["file"]).relative_to(root))
        except ValueError:
            name = item["file"]
        files.append(dict(item, file=name, status="ok" if item["ok"] else "failed",
                          log=str(combined.relative_to(output)), fixes="fixes/" + identity + ".yaml"))
    warnings = collect_diagnostics(output / "logs")
    for item in files:
        diagnostics = log_diagnostics(output / item["log"], Path(item["cwd"]))
        item["unique_diagnostics"] = len(diagnostics)
    directories = Counter(item["file"].split("/", 1)[0] if "/" in item["file"] else "." for item in files)
    versions = group.get("tool_versions", {}).get("clang-tidy", {})
    version_text = "unknown (version receipt unavailable)"
    if versions.get("stdout"):
        version_text = Path(versions["stdout"]).read_text(encoding="utf-8", errors="replace").strip()
    payload = dict(result, files=files, warnings=warnings, generated_at=datetime.now(timezone.utc).isoformat(),
                   tool_version=version_text, scan_paths=list(map(str, scopes)),
                   clang_tidy_bin=binaries.get("clang-tidy"), compile_db_dir=str(Path(group["selected_database"]).parent),
                   checks=policy["checks"], header_filter=next((argument.split("=", 1)[1] for item in result.get("files", [])
                       for argument in item.get("command", []) if argument.startswith("--config=")), "fixed by scan scope"),
                   jobs=policy["jobs"], scan_dirs=sorted(directories), selected_file_count=len(files), files_by_dir=dict(directories),
                   failures=[item["file"] for item in files if item["status"] != "ok"])
    if payload["header_filter"].startswith("{"):
        payload["header_filter"] = json.loads(payload["header_filter"])["HeaderFilterRegex"]
    write_summary(output / "clang-tidy-summary.md", output / "clang-tidy-summary.json", payload)
    (output / "clang-tidy-report.html").write_text(render_html(payload, output,
                                                             output.parent / "clang_static_analyzer"), encoding="utf-8")
    severity_report(SimpleNamespace(result_dir=str(output), project_root=str(root),
                                    output_md=str(output / "severity.md"), output_csv=str(output / "severity.csv")))
    page(output / "index.html", "Clang-Tidy reports", "<ul>" + "".join(
        "<li>" + link(name) + "</li>" for name in ("clang-tidy-report.html", "clang-tidy-summary.md",
                                                  "clang-tidy-summary.json", "severity.md", "severity.csv")) + "</ul>")
    result["warnings"] = warnings


def log_diagnostics(path, directory):
    diagnostics = Counter()
    text = path.read_text(encoding="utf-8", errors="replace")
    for match in WARNING_RE.finditer(text):
        location = str((directory / match.group("file")).resolve())
        for check in match.group("check").split(","):
            diagnostics[(location, int(match.group("line")), int(match.group("column")), check.strip())] += 1
    return diagnostics


def yaml_diagnostics(path, directory, source):
    import yaml
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("Diagnostics"), list):
        raise ValueError("Invalid Clang-Tidy YAML structure")
    main_file = document.get("MainSourceFile")
    if not isinstance(main_file, str) or (directory / main_file).resolve() != source:
        raise ValueError("Clang-Tidy YAML MainSourceFile mismatch")
    diagnostics = Counter()
    for diagnostic in document["Diagnostics"]:
        if not isinstance(diagnostic, dict):
            raise ValueError("Invalid Clang-Tidy diagnostic")
        message = diagnostic.get("DiagnosticMessage", diagnostic)
        if not isinstance(message, dict):
            raise ValueError("Invalid Clang-Tidy diagnostic message")
        location = message.get("FilePath")
        offset = message.get("FileOffset")
        check = diagnostic.get("DiagnosticName")
        if not isinstance(location, str) or not location or type(offset) is not int or not isinstance(check, str) or not check:
            raise ValueError("Clang-Tidy diagnostic lacks a valid location or rule")
        filename = (directory / location).resolve()
        contents = filename.read_bytes()
        if not 0 <= offset <= len(contents):
            raise ValueError("Clang-Tidy diagnostic byte offset outside source file")
        line = contents.count(b"\n", 0, offset) + 1
        column = offset - contents.rfind(b"\n", 0, offset)
        for name in check.split(","):
            diagnostics[(str(filename), line, column, name.strip())] += 1
    return diagnostics


def validate_evidence(destination, payload, result):
    errors = []
    receipts = result.get("files", [])
    root_files = {item["unit_id"]: item for item in receipts}
    summary_files = payload["files"]
    identities = [item["unit_id"] for item in summary_files]
    if (len(identities) != len(set(identities)) or set(identities) != set(root_files)
            or len(root_files) != len(receipts) or payload.get("selected_file_count") != len(receipts)
            or result.get("total") != len(receipts)):
        errors.append("Clang-Tidy summary/receipt TU count or identity mismatch")
    expected_warnings = collect_diagnostics(destination / "logs")
    if payload.get("warnings") != expected_warnings:
        errors.append("Clang-Tidy summary/log warning counts or rules mismatch")
    for item in summary_files:
        receipt = root_files.get(item["unit_id"])
        if receipt is None:
            continue
        source = Path(receipt["file"]).resolve()
        directory = Path(receipt["cwd"])
        item["file"] = str(source)
        if (item.get("status") != ("ok" if receipt["ok"] else "failed")
                or item.get("exit_code") != receipt["exit_code"]):
            errors.append("Clang-Tidy per-TU summary/receipt mismatch: " + str(source))
        fixes = destination / "fixes" / (item["unit_id"] + ".yaml")
        log = destination / "logs" / (item["unit_id"] + ".log")
        if not log.is_file():
            errors.append("Missing Clang-Tidy execution log: " + str(source))
            continue
        expected_log = "\n".join((destination / "logs" / Path(receipt[key]).name).read_text(
            encoding="utf-8", errors="replace") for key in ("stdout", "stderr"))
        if log.read_text(encoding="utf-8", errors="replace") != expected_log:
            errors.append("Clang-Tidy combined/raw log mismatch: " + str(source))
        diagnostics = log_diagnostics(log, directory)
        if item.get("unique_diagnostics") != len(diagnostics):
            errors.append("Clang-Tidy per-TU diagnostic count mismatch: " + str(source))
        if diagnostics and not fixes.is_file():
            errors.append("Missing Clang-Tidy YAML for warning TU: " + str(source))
        if fixes.is_file():
            try:
                native = yaml_diagnostics(fixes, directory, source)
                if native != diagnostics:
                    errors.append("Clang-Tidy YAML/log diagnostic identity or count mismatch: " + str(source))
            except ImportError:
                errors.append("PyYAML required to validate native Clang-Tidy YAML")
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append("Cannot validate Clang-Tidy YAML: " + str(error))
            except Exception as error:
                errors.append("Cannot parse native Clang-Tidy YAML: " + str(error))
    expected_fixes = {identity + ".yaml" for identity in root_files}
    if any(path.name not in expected_fixes for path in (destination / "fixes").glob("*.yaml")):
        errors.append("Unexpected Clang-Tidy YAML outside selected TUs")
    return {"files": [item["file"] for item in summary_files], "errors": errors}


def export_evidence(output, target, result):
    destination = target / "04-clang-tidy" / "result"
    shutil.copytree(output, destination)
    payload = json.loads((destination / "clang-tidy-summary.json").read_text())
    validation = validate_evidence(destination, payload, result)
    write_json(destination / "clang-tidy-summary.json", payload)
    return validation


if __name__ == "__main__":
    raise SystemExit("Internal tool report helper; use this tool's scan.sh.")
