import argparse
import csv
import hashlib
import html
import json
import os
import shlex
import shutil
import subprocess
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "scan" / "analy.config"
CPP_SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".m", ".mm"}
CSHARP_SUFFIXES = {".cs"}
SOURCE_SUFFIXES = CPP_SOURCE_SUFFIXES | CSHARP_SUFFIXES
HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx"}
PROCESS_TIMEOUT = 900
ACTIVE_PROCESSES = set()
PROCESS_LOCK = threading.Lock()
CANCELLED = threading.Event()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("配置键重复: " + key)
        result[key] = value
    return result


def inputs(argv, config_path=CONFIG):
    parser = argparse.ArgumentParser(
        description="固定规则扫描；仅接受扫描路径和结果路径。",
        allow_abbrev=False,
    )
    parser.add_argument("--scan-path", action="append", nargs="+", metavar="PATH",
                        help="扫描文件或目录，可传多个路径，也可重复使用")
    parser.add_argument("--result-path", metavar="PATH", help="结果根目录")
    args = parser.parse_args(argv)
    config = {}
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
        if not isinstance(config, dict) or set(config) - {"scan_paths", "result_path"}:
            raise ValueError("配置仅允许 scan_paths 和 result_path，不支持旧 Shell 配置")
        if "scan_paths" in config and (
            not isinstance(config["scan_paths"], list) or not config["scan_paths"]
            or any(not isinstance(item, str) or not item.strip() for item in config["scan_paths"])
        ):
            raise ValueError("scan_paths 必须为非空路径字符串数组")
        if "result_path" in config and (
            not isinstance(config["result_path"], str) or not config["result_path"].strip()
        ):
            raise ValueError("result_path 必须为非空路径字符串")
    raw_paths = [item for group in args.scan_path for item in group] if args.scan_path else config.get("scan_paths")
    raw_result = args.result_path if args.result_path is not None else config.get("result_path")
    if not raw_paths or not raw_result or not raw_result.strip():
        raise ValueError("必须通过命令行或 scan/analy.config 提供扫描路径和结果路径")
    if any(not item.strip() for item in raw_paths):
        raise ValueError("扫描路径不能为空")
    source_base = Path.cwd() if args.scan_path else config_path.parent
    result_base = Path.cwd() if args.result_path is not None else config_path.parent
    paths = list(dict.fromkeys((source_base / item).resolve() for item in raw_paths))
    for path in paths:
        if not path.exists() or not (path.is_dir() or path.is_file()):
            raise ValueError("扫描路径不存在或不是普通文件/目录: " + str(path))
        if path.is_file() and path.suffix.lower() not in SOURCE_SUFFIXES | HEADER_SUFFIXES:
            raise ValueError("只支持 C/C++/C# 源文件、头文件或目录: " + str(path))
    return paths, (result_base / raw_result).resolve()


def in_scope(path, scopes):
    path = path.resolve()
    resolved_scopes = [s.resolve() for s in scopes]
    return any(
        path == scope or (
            scope.is_dir() and scope in path.parents
            and scope / "thirdparty" not in path.parents
        )
        for scope in resolved_scopes
    )


def find_database(scope):
    start = scope if scope.is_dir() else scope.parent
    for folder in (start, *start.parents):
        for relative in ("compile_commands.json", "build/compile_commands.json", "build/Debug/compile_commands.json"):
            candidate = folder / relative
            if candidate.is_file():
                return candidate.resolve()
    raise ValueError("未找到编译数据库（从扫描路径逐级向上查找）: " + str(scope))


def scanner_suffixes(scanners):
    suffixes = set()
    for scanner in scanners:
        suffixes.update(getattr(scanner, "INPUT_SUFFIXES", SOURCE_SUFFIXES))
    return suffixes


def plan(paths, source_fallback=False, suffixes=SOURCE_SUFFIXES, ignore_database=False):
    databases = {}
    for path in paths:
        try:
            if ignore_database:
                raise ValueError("ignore database")
            database = find_database(path)
        except ValueError:
            if not source_fallback:
                raise
            database = None
        databases.setdefault(database, []).append(path)
    groups = []
    for database, scopes in databases.items():
        if database is None:
            sources = set()
            for scope in scopes:
                candidates = [scope] if scope.is_file() else scope.rglob("*")
                sources.update(path.resolve() for path in candidates
                               if path.is_file() and path.suffix.lower() in suffixes
                               and in_scope(path, scopes))
            if not sources:
                raise ValueError("源码扫描范围未匹配任何源文件")
            groups.append((None, scopes, [{"file": str(path), "directory": str(path.parent),
                                          "arguments": []} for path in sorted(sources)]))
            continue
        entries = json.loads(database.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("编译数据库必须为数组: " + str(database))
        selected = []
        contexts = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
                raise ValueError("编译数据库包含无效记录: " + str(database))
            directory = Path(entry.get("directory") or database.parent)
            directory = (database.parent / directory).resolve()
            source = (directory / entry["file"]).resolve()
            if source.suffix.lower() not in suffixes or not in_scope(source, scopes):
                continue
            if not source.is_file():
                raise ValueError("编译数据库中的源文件不存在: " + str(source))
            arguments = entry.get("arguments")
            if arguments is None:
                arguments = shlex.split(entry.get("command", ""))
            if not isinstance(arguments, list) or not arguments or any(not isinstance(item, str) for item in arguments):
                raise ValueError("编译记录缺少有效 command/arguments: " + str(source))
            selected_entry = {"directory": str(directory), "file": str(source), "arguments": arguments}
            previous = contexts.get(source)
            if previous is not None:
                if previous["directory"] != selected_entry["directory"] or previous["arguments"] != arguments:
                    raise ValueError("同一源码存在冲突的编译上下文: " + str(source))
                continue
            contexts[source] = selected_entry
            selected.append(selected_entry)
        if not selected:
            raise ValueError("扫描范围未匹配任何编译单元: " + ", ".join(map(str, scopes)))
        groups.append((database, scopes, sorted(selected, key=lambda entry: entry["file"])))
    return groups


def environment():
    allowed = {"HOME", "PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def stop_process(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def cancel_processes():
    CANCELLED.set()
    with PROCESS_LOCK:
        for process in list(ACTIVE_PROCESSES):
            stop_process(process)


def execute(command, cwd, stdout, stderr, timeout=None, env=None):
    started = datetime.now(timezone.utc).isoformat()
    timed_out = False
    with stdout.open("w", encoding="utf-8") as out_stream, stderr.open("w", encoding="utf-8") as err_stream:
        try:
            with PROCESS_LOCK:
                if CANCELLED.is_set():
                    return {"command": command, "cwd": str(cwd), "exit_code": 130, "cancelled": True,
                            "stdout": str(stdout), "stderr": str(stderr), "started": started,
                            "finished": datetime.now(timezone.utc).isoformat()}
                process = subprocess.Popen(command, cwd=cwd, env=environment() if env is None else env,
                                           stdin=subprocess.DEVNULL, stdout=out_stream, stderr=err_stream,
                                           start_new_session=True)
                ACTIVE_PROCESSES.add(process)
            try:
                code = process.wait(timeout=PROCESS_TIMEOUT if timeout is None else timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_process(process)
                code = 124
                err_stream.write("\nProcess timeout; process group terminated.\n")
            except KeyboardInterrupt:
                cancel_processes()
                raise
            finally:
                with PROCESS_LOCK:
                    ACTIVE_PROCESSES.discard(process)
        except OSError as error:
            err_stream.write(str(error))
            code = 127
    return {"command": command, "cwd": str(cwd), "exit_code": code, "timed_out": timed_out,
            "cancelled": CANCELLED.is_set(), "started": started,
            "finished": datetime.now(timezone.utc).isoformat(), "stdout": str(stdout), "stderr": str(stderr)}


def unit_id(entry):
    return hashlib.sha256(entry["file"].encode()).hexdigest()[:20]




def link(path, label=None):
    return '<a href="{}">{}</a>'.format(quote(str(path), safe="/"), html.escape(str(label or path)))


def page(path, title, body):
    path.write_text('<!doctype html><html><head><meta charset="utf-8"><title>'
                    + html.escape(title) + '</title><style>body{font:16px sans-serif;max-width:1200px;'
                    'margin:2em auto;padding:1em}table{border-collapse:collapse}td,th{padding:.6em;'
                    'border:1px solid #bbb}pre{white-space:pre-wrap}</style></head><body><h1>'
                    + html.escape(title) + '</h1>' + body + '</body></html>\n', encoding="utf-8")


def source_root(scopes, database):
    if database:
        folder = Path(database).parent
        if folder.name == "Debug":
            folder = folder.parent
        if folder.name == "build":
            folder = folder.parent
        return folder
    return Path(os.path.commonpath([str(path if path.is_dir() else path.parent) for path in scopes]))


def find_tool(name, candidates=()):
    for candidate in (name, *candidates):
        resolved = shutil.which(candidate)
        if resolved:
            return str(Path(resolved).absolute())
    raise ValueError("缺少工具 " + name + "，请运行 install/ 中对应安装脚本")


def llvm_candidates(name):
    return [name + "-" + str(version) for version in range(21, 9, -1)] + [
        str(path) for path in sorted(Path("/usr/lib").glob("llvm-*/bin/" + name), reverse=True)]


def collect_units(entries, output, worker, jobs):
    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=jobs)
    futures = [pool.submit(worker, entry) for entry in entries]
    interrupted = False
    try:
        receipts = [future.result() for future in as_completed(futures)]
        receipts.sort(key=lambda item: item["file"])
    except BaseException as error:
        interrupted = isinstance(error, KeyboardInterrupt)
        cancel_processes()
        for future in futures:
            future.cancel()
        raise
    finally:
        pool.shutdown(wait=True)
        if not interrupted:
            CANCELLED.clear()
    with (output / "tu-mapping.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("unit_id", "file", "artifact", "status"))
        writer.writerows((item["unit_id"], item["file"], item["artifact"], "ok" if item["ok"] else "failed")
                         for item in receipts)
    return {"ok": all(item["ok"] for item in receipts), "total": len(entries),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "analyzed": sum(item["ok"] for item in receipts), "files": receipts}


if __name__ == "__main__":
    raise SystemExit("Internal shared helpers; use a scan.sh entry.")
