import json
import platform
import re
import shutil
from pathlib import Path
from datetime import datetime, timezone
import common


def artifact_layer(relative):
    parts = Path(relative).parts
    name = parts[-1]
    if "cppcheck-build" in parts:
        return "CACHE"
    if (parts[0] == "03-cppcheck" and name == "cppcheck-compile-report.xml"
            or parts[0] == "04-clang-tidy" and "fixes" in parts and name.endswith(".yaml")
            or parts[0] == "05-clang-static-analyzer" and name.endswith(".plist")):
        return "RAW"
    if (Path(name).suffix in (".html", ".css", ".md") or name == "summary.json"
            or name in ("severity.csv", "release-gate.json")):
        return "DERIVED"
    return "RECEIPT"


def git_state(root, output, label):
    receipts = {}
    values = {}
    commands = {"repo": ["rev-parse", "--show-toplevel"], "branch": ["rev-parse", "--abbrev-ref", "HEAD"],
                "commit": ["rev-parse", "HEAD"], "status": ["status", "--porcelain"],
                "diff": ["diff", "HEAD", "--binary"]}
    for name, arguments in commands.items():
        stdout = output / (label + "-git-" + name + ".txt")
        receipts[name] = common.execute(["git", "-C", str(root), *arguments], root, stdout,
                                        output / (label + "-git-" + name + ".err.txt"), timeout=30)
        values[name] = stdout.read_text(encoding="utf-8", errors="replace").strip()
    values["ok"] = all(item["exit_code"] == 0 for item in receipts.values())
    values["receipts"] = receipts
    return values


def source_hashes(files):
    return {name: common.digest(Path(name)) if Path(name).is_file() else None for name in files}


def begin(group, folder, paths, result_path, manifest):
    bundle = folder / "evidence-bundle"
    metadata = bundle / "01-metadata"
    metadata.mkdir(parents=True)
    root = common.source_root([Path(path) for path in group["scan_paths"]], group["database"])
    state = git_state(root, metadata, "before")
    group["baseline"] = {"root": str(root), "git": state, "sources": source_hashes(group["selected_files"]),
                         "captured_at": datetime.now(timezone.utc).isoformat()}
    group["evidence_bundle"] = str(bundle.relative_to(folder.parent))
    (metadata / "code-state.txt").write_text(
        "repo: {repo}\nbranch: {branch}\ncommit: {commit}\ndate: {date}\ndirty: {dirty}\n\n{status}\n".format(
            repo=state["repo"], branch=state["branch"], commit=state["commit"],
            date=group["baseline"]["captured_at"], dirty=bool(state["status"]), status=state["status"]), encoding="utf-8")
    (metadata / "compile-db-selected-tus.txt").write_text("\n".join(group["selected_files"]) + "\n", encoding="utf-8")
    common.write_json(metadata / "source-hashes.json", group["baseline"]["sources"])
    common.write_json(metadata / "analy.config", {"scan_paths": list(map(str, paths)), "result_path": str(result_path)})
    if common.CONFIG.is_file():
        shutil.copy2(common.CONFIG, metadata / "analy.config.original")
    environment = {"captured_at": group["baseline"]["captured_at"], "platform": platform.platform(),
                   "python": platform.python_version(), "versions": manifest["versions"],
                   "unavailable_tools": manifest.get("unavailable_tools", {}), "policy": manifest["policy"]}
    common.write_json(metadata / "scan-environment.json", environment)
    (metadata / "scan-environment.md").write_text("# Scan environment\n\n```json\n"
                                                 + json.dumps(environment, ensure_ascii=False, indent=2)
                                                 + "\n```\n", encoding="utf-8")
    common.write_json(metadata / "scanner-hashes.json", {str(path.relative_to(common.ROOT)): common.digest(path)
                      for path in sorted((common.ROOT / "scan").rglob("*"))
                      if path.is_file() and path.suffix in (".py", ".sh")})
    database_folder = bundle / "02-compile-db"
    database_folder.mkdir()
    if group["database"]:
        shutil.copy2(group["database"], database_folder / "compile_commands.json")
        shutil.copy2(group["selected_database"], database_folder / "compile_commands.selected.json")
    for name, version in manifest["versions"].items():
        for stream in ("stdout", "stderr"):
            shutil.copy2(version[stream], metadata / (name + ".version." + stream + ".txt"))


def check_versions(manifest):
    errors = []
    for name in ("cppcheck", "clang-tidy", "clang", "clang++"):
        receipt = manifest["versions"].get(name)
        if not receipt or receipt["exit_code"] != 0:
            errors.append("Missing tool version: " + name)
            continue
        text = Path(receipt["stdout"]).read_text(errors="replace") + Path(receipt["stderr"]).read_text(errors="replace")
        match = re.search(r"(?:Cppcheck\s+|(?:LLVM|clang)\s+version\s+)(\d+)\.(\d+)", text, re.I)
        minimum = (2, 22) if name == "cppcheck" else (14, 0)
        if not match or tuple(map(int, match.groups())) < minimum or re.search(r"\bdev\b", text, re.I):
            errors.append("Unsupported or unverified tool version: " + name + ": " + text.strip())
    return errors


def finish(group, folder, scanners, manifest):
    bundle = folder / "evidence-bundle"
    metadata = bundle / "01-metadata"
    baseline = group["baseline"]
    after = git_state(Path(baseline["root"]), metadata, "after")
    errors = check_versions(manifest)
    inputs = group.get("input_baseline", {})
    inputs_after = group.get("input_baseline_after", {})
    if (folder / "input-baseline").is_dir():
        try:
            shutil.copytree(folder / "input-baseline", metadata / "input-baseline")
        except (OSError, ValueError) as error:
            errors.append("Dependency evidence copy failed: " + str(error))
    errors.extend(inputs.get("errors", []))
    errors.extend(inputs_after.get("errors", []))
    if not inputs.get("ok") or not inputs_after.get("ok"):
        errors.append("Compiler dependency baseline unavailable or incomplete")
    elif any(inputs.get(key) != inputs_after.get(key) for key in ("files", "resolved_paths", "environment")):
        errors.append("Compiler dependency closure or content changed during scan")
    for digest, relative in inputs.get("snapshots", {}).items():
        snapshot = folder / "input-baseline" / "before" / relative
        try:
            if not snapshot.is_file() or common.digest(snapshot) != digest:
                errors.append("Dependency snapshot missing or changed: " + relative)
        except OSError as error:
            errors.append("Dependency snapshot unreadable: " + relative + ": " + str(error))
    for name, version in manifest["versions"].items():
        if not version.get("command"):
            errors.append("Missing tool binary identity: " + name)
            continue
        binary = Path(version["command"][0])
        if not binary.is_file() or common.digest(binary) != version.get("binary_sha256"):
            errors.append("Tool binary changed during scan: " + name)
    if not baseline["git"]["ok"] or not re.fullmatch(r"[0-9a-f]{40}", baseline["git"]["commit"]):
        errors.append("Missing full Git commit baseline")
    if (not after["ok"] or after["commit"] != baseline["git"]["commit"]
            or after["diff"] != baseline["git"]["diff"] or after["status"] != baseline["git"]["status"]):
        errors.append("Git baseline changed during scan")
    if source_hashes(group["selected_files"]) != baseline["sources"]:
        errors.append("Selected source files changed during scan")
    if not group["database"]:
        errors.append("Source fallback is not a three-tool compilation-database bundle")
    elif common.digest(Path(group["database"])) != group["database_sha256"]:
        errors.append("Compilation database changed during scan")
    roots = {baseline["git"]["repo"]}
    repository_checks = []
    for position, parent in enumerate(sorted({Path(path).parent for path in group["selected_files"]})):
        stdout = metadata / ("repository-check-" + str(position) + ".txt")
        repository = common.execute(["git", "-C", str(parent), "rev-parse", "--show-toplevel"],
                                    parent, stdout, metadata / ("repository-check-" + str(position) + ".err.txt"), timeout=30)
        repository_checks.append(repository)
        if repository["exit_code"] == 0:
            roots.add(stdout.read_text().strip())
        else:
            errors.append("Cannot identify source repository: " + str(parent))
    common.write_json(metadata / "repository-checks.json", repository_checks)
    if len(roots) != 1:
        errors.append("Multiple source repositories in one bundle")
    selected = set(group["selected_files"])
    checks = {}
    for scanner in scanners:
        result = group["results"][scanner.NAME]
        exporter = getattr(scanner, "export_evidence", None)
        if not exporter or result.get("error") or result.get("status") in ("unavailable", "cancelled"):
            errors.append("Missing evidence: " + scanner.NAME)
            continue
        try:
            checks[scanner.NAME] = exporter(folder / scanner.NAME, bundle, result)
            if set(checks[scanner.NAME]["files"]) != selected:
                errors.append("TU identity mismatch: " + scanner.NAME)
            errors.extend(checks[scanner.NAME].get("errors", []))
        except (OSError, ValueError, KeyError, TypeError, SyntaxError) as error:
            errors.append(scanner.NAME + " export: " + str(error))
    required = {"cpp_check", "clang_tidy", "clang_static_analyzer"}
    if {scanner.NAME for scanner in scanners} != required:
        errors.append("All three tools are required for certification")
    if group.get("report_error") or not all(result["ok"] for result in group["results"].values()):
        errors.append("Scan has failed execution, report errors or CSA findings")
    gate = {"ok": not errors, "errors": errors, "checks": checks, "release_ready": False,
            "pending_certification": ["downstream_schema_and_ingestion", "business_product_boundary",
                                      "all_tool_dependency_closure_and_offline_reproduction"],
            "dependency_baseline": {"ok": inputs.get("ok", False), "coverage": inputs.get("coverage", "unavailable"),
                                    "offline_reproducible": False, "limitations": inputs.get("limitations", [])},
            "downstream_ingestion": "not_verified", "product_isolation": "one_git_repository_per_bundle; product identity not certified"}
    group["release_gate"] = gate
    common.write_json(bundle / "release-gate.json", gate)
    artifacts = []
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and path != bundle / "manifest.json":
            relative = path.relative_to(bundle).as_posix()
            layer = artifact_layer(relative)
            artifacts.append({"path": relative, "sha256": common.digest(path), "size": path.stat().st_size, "layer": layer})
    common.write_json(bundle / "manifest.json", {"schema": "scanner-evidence-1", "target_contract": "three-tool-input-1.0",
                       "downstream_compatible": False, "release_ready": False, "commit": baseline["git"]["commit"],
                       "selected_files": group["selected_files"], "release_gate": gate, "artifacts": artifacts})
