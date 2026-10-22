import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
import common
from reporting import run_index
import evidence


def capture_inputs(scanner, entries, folder, binaries, summary=None):
    try:
        baseline = scanner.capture_inputs(entries, folder, binaries)
        if not isinstance(baseline, dict):
            raise ValueError("Invalid dependency baseline")
        if summary is not None:
            common.write_json(summary, baseline)
        return baseline
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        return {"ok": False, "files": {}, "resolved_paths": {}, "snapshots": {},
                "errors": ["Dependency capture failed: " + str(error)], "phase": folder.name}


def render_reports(scanners, group, folder, entries, scopes, binaries):
    errors = []
    for scanner in sorted(scanners, key=lambda item: item.REPORT_PRIORITY):
        result = group["results"][scanner.NAME]
        if result.get("status") == "unavailable" or result.get("error"):
            continue
        output = folder / scanner.NAME
        try:
            scanner.report(result, group, output, entries, scopes, binaries)
            if (output / "index.html").exists():
                (output / "index.md").write_text("# " + scanner.NAME + " reports\n\n- [HTML report index](index.html)\n",
                                                encoding="utf-8")
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            result["report_error"] = str(error)
            errors.append(scanner.NAME + ": " + str(error))
    if errors:
        raise ValueError("; ".join(errors))


def run(paths, result_path, scanners):
    common.CANCELLED.clear()
    groups = common.plan(paths, source_fallback=True)
    binaries = {}
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    run_path = result_path / run_id
    run_path.mkdir(parents=True)
    manifest = {"runner_schema": "three-tool-sast-runner-execution-1.0", "run_id": run_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "policy": {"version": "tool-owned-scan-1", "tools": {scanner.NAME: scanner.POLICY for scanner in scanners}},
                "scan_paths": list(map(str, paths)), "result_path": str(result_path), "run_path": str(run_path),
                "tools": [scanner.NAME for scanner in scanners], "versions": {}, "groups": [], "ok": False}
    common.write_json(run_path / "manifest.json", manifest)
    try:
        return complete_run(paths, result_path, scanners, groups, binaries, run_path, manifest)
    except KeyboardInterrupt:
        common.cancel_processes()
        manifest["cancelled"] = True
        manifest["ok"] = False
        common.write_json(run_path / "manifest.json", manifest)
        raise
    except (OSError, ValueError, TypeError) as error:
        manifest["error"] = str(error)
        common.write_json(run_path / "manifest.json", manifest)
        raise


def complete_run(paths, result_path, scanners, groups, binaries, run_path, manifest):
    unavailable = {}
    for scanner in scanners:
        if scanner.SUPPORTS_SOURCE or any(database for database, _, _ in groups):
            try:
                binaries.update(scanner.resolve_tools())
            except (OSError, ValueError) as error:
                unavailable[scanner.NAME] = str(error)
    manifest["unavailable_tools"] = unavailable
    for name, binary in binaries.items():
        version = common.execute([binary, "--version"], common.ROOT, run_path / (name + ".version.txt"),
                                 run_path / (name + ".version.err.txt"))
        version["binary_sha256"] = common.digest(Path(binary))
        manifest["versions"][name] = version
    for position, (database, scopes, entries) in enumerate(groups, 1):
        group_path = run_path / ("group-" + str(position))
        group_path.mkdir()
        selected_database = group_path / "compile_commands.json" if database else None
        if selected_database:
            common.write_json(selected_database, entries)
        group = {"database": str(database) if database else None,
                 "database_sha256": common.digest(database) if database else None,
                 "mode": "compile" if database else "source",
                 "selected_database": str(selected_database) if selected_database else None,
                 "selected_database_sha256": common.digest(selected_database) if selected_database else None,
                 "tool_versions": manifest["versions"],
                 "scan_paths": list(map(str, scopes)), "selected_files": [entry["file"] for entry in entries], "results": {}}
        manifest["groups"].append(group)
        evidence.begin(group, group_path, paths, result_path, manifest)
        collectors = [scanner for scanner in scanners if hasattr(scanner, "capture_inputs")
                      and scanner.NAME not in unavailable and database is not None]
        for scanner in collectors:
            group["input_baseline"] = capture_inputs(scanner, entries, group_path / "input-baseline" / "before",
                                                     binaries, group_path / "input-baseline.json")
        for scanner in scanners:
            print("扫描 " + scanner.NAME + ": " + str(len(entries)) + " 个编译单元", flush=True)
            try:
                if scanner.NAME in unavailable:
                    receipt = {"ok": False, "status": "unavailable", "error": unavailable[scanner.NAME]}
                elif database is None and not scanner.SUPPORTS_SOURCE:
                    receipt = {"ok": False, "status": "unavailable", "error": "缺少编译数据库；此工具不支持源码回退"}
                else:
                    receipt = scanner.scan(entries, scopes, selected_database, group_path / scanner.NAME, binaries)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                receipt = {"ok": False, "error": str(error)}
            except KeyboardInterrupt:
                common.cancel_processes()
                group["results"][scanner.NAME] = {"ok": False, "status": "cancelled", "exit_code": 130}
                manifest["cancelled"] = True
                common.write_json(run_path / "manifest.json", manifest)
                raise
            group["results"][scanner.NAME] = receipt
            common.write_json(run_path / "manifest.json", manifest)
        try:
            render_reports(scanners, group, group_path, entries, scopes, binaries)
        except KeyboardInterrupt:
            common.cancel_processes()
            manifest["cancelled"] = True
            group["report_error"] = "Report generation cancelled"
            common.write_json(run_path / "manifest.json", manifest)
            raise
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            group["report_error"] = str(error)
            common.write_json(group_path / "report-error.json", {"ok": False, "error": str(error)})
        common.write_json(run_path / "manifest.json", manifest)
        try:
            for scanner in collectors:
                group["input_baseline_after"] = capture_inputs(
                    scanner, entries, group_path / "input-baseline" / "after", binaries)
            evidence.finish(group, group_path, scanners, manifest)
        except (OSError, ValueError, KeyError, TypeError) as error:
            group["evidence_error"] = str(error)
            group["release_gate"] = {"ok": False, "errors": [str(error)]}
    manifest["ok"] = all(not group.get("report_error") and not group.get("evidence_error") and all(result["ok"] for result in group["results"].values())
                         for group in manifest["groups"])
    manifest["evidence_checks_ok"] = all(group.get("release_gate", {}).get("ok", False) for group in manifest["groups"])
    manifest["downstream_compatible"] = False
    required = {"cpp_check", "clang_tidy", "clang_static_analyzer"}
    manifest["evidence_gate_required"] = {scanner.NAME for scanner in scanners} == required
    manifest["release_ready"] = False
    manifest["exit_code"] = (1 if not manifest["ok"] else 3 if manifest["evidence_gate_required"]
                             and not manifest["evidence_checks_ok"] else 0)
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    common.write_json(run_path / "manifest.json", manifest)
    run_index(manifest, run_path)
    print("结果目录: " + str(run_path))
    if manifest["exit_code"] == 3:
        print("扫描执行成功，但证据门禁未通过；详见 evidence-bundle/release-gate.json。", file=sys.stderr)
    return manifest["exit_code"]


def main(scanners, argv=None):
    try:
        paths, result_path = common.inputs(sys.argv[1:] if argv is None else argv)
        return run(paths, result_path, scanners)
    except KeyboardInterrupt:
        common.cancel_processes()
        print("扫描已取消，子进程组已终止。", file=sys.stderr)
        return 130
    except (OSError, ValueError, TypeError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit("Internal session helpers; use a scan.sh entry.")
