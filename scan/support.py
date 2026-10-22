import functools
import http.server
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from common import CONFIG, ROOT, SOURCE_SUFFIXES, HEADER_SUFFIXES, digest, environment, inputs, plan, write_json
from registry import SCANNERS, resolve_tool
from session import run


def probe(command, cwd=None):
    try:
        completed = subprocess.run(command, cwd=cwd, env=environment(), text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
        return {"command": command, "exit_code": completed.returncode, "output": completed.stdout.strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": command, "exit_code": None, "output": str(error)}


def collect(paths, output):
    groups = plan(paths, source_fallback=True)
    output.mkdir(parents=True, exist_ok=True)
    tools = {}
    for name in ("clang-tidy", "clang", "clang++", "cppcheck", "scan-build", "gcc", "g++"):
        try:
            binary = resolve_tool(name)
            tools[name] = {"path": binary, "sha256": digest(Path(binary)), "version": probe([binary, "--version"]),
                           "package": probe(["dpkg-query", "-S", str(Path(binary).resolve())])}
        except ValueError as error:
            tools[name] = {"path": None, "error": str(error)}
    helper_candidates = [shutil.which("cppcheck-htmlreport"), str(Path.home() / ".local/bin/cppcheck-htmlreport"),
                         str(ROOT / "scan/cpp_check/htmlreport/cppcheck-htmlreport")]
    helper = next((Path(path) for path in helper_candidates if path and Path(path).is_file()),
                  ROOT / "scan/cpp_check/htmlreport/cppcheck-htmlreport")
    tools["cppcheck-htmlreport"] = {"path": str(helper) if helper.is_file() else None,
                                    "sha256": digest(helper) if helper.is_file() else None}
    repositories = []
    filesystem = set()
    for scope in paths:
        folder = scope if scope.is_dir() else scope.parent
        repositories.append({"scope": str(scope), "root": probe(["git", "rev-parse", "--show-toplevel"], folder),
                             "revision": probe(["git", "rev-parse", "HEAD"], folder),
                             "branch": probe(["git", "branch", "--show-current"], folder),
                             "status": probe(["git", "status", "--porcelain"], folder)})
        filesystem.update(str(path.resolve()) for path in ([scope] if scope.is_file() else scope.rglob("*"))
                          if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES | HEADER_SUFFIXES)
    database_groups = []
    selected = []
    for database, scopes, entries in groups:
        files = [entry["file"] for entry in entries]
        selected.extend(files)
        database_groups.append({"database": str(database) if database else None,
                                "sha256": digest(database) if database else None,
                                "entry_count": len(json.loads(database.read_text())) if database else None,
                                "scan_paths": list(map(str, scopes)), "selected_files": files,
                                "selected_count": len(files), "mode": "compile" if database else "source"})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    payload = {"generated_at": stamp, "host": {"hostname": socket.gethostname(), "platform": platform.platform(),
                "architecture": platform.machine(), "kernel": platform.release(),
                "os_release": Path("/etc/os-release").read_text() if Path("/etc/os-release").exists() else None},
               "repositories": repositories, "tools": tools, "scope": {"scan_paths": list(map(str, paths)),
                "result_path": str(output), "config_path": str(CONFIG),
                "config_sha256": digest(CONFIG) if CONFIG.exists() else None,
                "filesystem_source_and_header_count": len(filesystem), "database_groups": database_groups},
               "limits": ["Environment snapshot only; no scan was started or certified.",
                          "Filesystem counts include dependencies and do not establish actual analyzed coverage."]}
    payload["scripts_sha256"] = {str(path.relative_to(ROOT)): digest(path)
                                for path in sorted((ROOT / "scan").rglob("*"))
                                if path.is_file() and path.suffix in (".sh", ".py")}
    write_json(output / ("scan-environment-" + stamp + ".json"), payload)
    (output / ("compile-db-selected-tus-" + stamp + ".txt")).write_text("\n".join(sorted(set(selected))) + "\n")
    lines = ["# Static Analysis Environment Snapshot", "", "This does not initiate or certify a scan.", "",
             "## Scan paths", "", *["- `" + str(path) + "`" for path in paths], "", "## Tools", "",
             "| Tool | Path | Version / error |", "| --- | --- | --- |"]
    for name, tool in tools.items():
        value = tool.get("version", {}).get("output", tool.get("error", "bundled report helper"))
        lines.append("| {} | {} | {} |".format(name, tool["path"], value.replace("|", "\\|").replace("\n", "<br>")))
    lines.extend(["", "## Database groups", "", "```json", json.dumps(database_groups, indent=2), "```"])
    (output / ("scan-environment-" + stamp + ".md")).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("环境快照: " + str(output))
    return 0


def serve(paths, output):
    if not output.is_dir():
        raise ValueError("结果目录不存在: " + str(output))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(output))
    with http.server.ThreadingHTTPServer(("127.0.0.1", 8000), handler) as server:
        print("报告浏览: http://127.0.0.1:8000/ (Ctrl-C 停止)", flush=True)
        server.serve_forever()
    return 0


def verify(paths, output):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="static-analy-verify-") as temporary:
        root = Path(temporary)
        source = root / "sample.c"
        source.write_text("int main(void) { int *pointer = 0; return *pointer; }\n")
        write_json(root / "compile_commands.json", [{"directory": str(root), "file": str(source),
                   "arguments": ["cc", "-c", str(source)]}])
        before = set(output.glob("*/manifest.json"))
        scan_code = run([root], output, SCANNERS)
        manifests = set(output.glob("*/manifest.json")) - before
        if len(manifests) != 1:
            raise ValueError("冒烟验证未生成唯一 manifest")
        manifest_path = manifests.pop()
        manifest = json.loads(manifest_path.read_text())
        results = manifest["groups"][0]["results"]
        csa = results["clang_static_analyzer"]
        passed = not manifest["groups"][0].get("report_error") and all(
            item.get("exit_code") == 0 for tool in ("clang_tidy", "clang_static_analyzer")
            for item in results[tool].get("files", [])) and results["cpp_check"]["exit_code"] == 0
        passed = passed and csa.get("reports", 0) > 0 and bool(csa["files"][0].get("html_reports"))
        receipt = {"ok": bool(passed), "scan_exit_code": scan_code, "expected_defect_fixture": True,
                   "manifest": str(manifest_path), "original_scan_paths": list(map(str, paths)),
                   "note": "Temporary fixture only; original scan paths were not scanned by this helper."}
        write_json(manifest_path.parent / "verification.json", receipt)
        print("冒烟验证: " + ("通过（已验证真实缺陷与 HTML 报告）" if passed else "失败"))
        return 0 if passed else 1


def main(action):
    try:
        paths, output = inputs(sys.argv[1:])
        return {"collect": collect, "serve": serve, "verify": verify}[action](paths, output)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit("Internal support helper; use scan/serve.sh, verify.sh or collect_acceptance_env.sh.")
