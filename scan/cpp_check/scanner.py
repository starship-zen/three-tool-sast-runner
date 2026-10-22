import sys
import xml.etree.ElementTree as element_tree
from pathlib import Path
import common
from . import reporting
from .cache import build_cache

NAME = "cpp_check"
JOBS = 4
SUPPORTS_SOURCE = True
REQUIRED_TOOLS = ("cppcheck",)
REPORT_PRIORITY = 1
FLAGS = ["--xml", "--xml-version=2", "--enable=all", "--inconclusive"]
POLICY = {"jobs": JOBS, "flags": FLAGS, "source_fallback": True,
          "suppressions": ["--suppress=*:/usr/*", "--suppress=*:/usr/local/*", "--suppress=*:*thirdparty/*"]}


def resolve_tools():
    candidates = [str(Path.home() / ".local/bin/cppcheck")]
    return {"cppcheck": common.find_tool("cppcheck", candidates)}


def scan(entries, scopes, database, output, binaries):
    output.mkdir(parents=True)
    with build_cache(output, entries, binaries["cppcheck"], POLICY, database) as cache_receipt:
        receipt = scan_with_cache(entries, scopes, database, output, binaries)
        cache_receipt["scan_ok"] = receipt["ok"]
    receipt["cache"] = cache_receipt
    common.write_json(output / "receipt.json", receipt)
    return receipt


def scan_with_cache(entries, scopes, database, output, binaries):
    binary = binaries["cppcheck"]
    cache = output / "cppcheck-build"
    inputs = ["--project=" + str(database)] if database else [entry["file"] for entry in entries]
    flags = FLAGS if database else [flag for flag in FLAGS if flag != "--inconclusive"]
    command = [binary, *inputs, *flags,
               "--cppcheck-build-dir=" + str(cache), "-j", str(JOBS),
               "--suppress=*:/usr/*", "--suppress=*:/usr/local/*", "--suppress=*:*thirdparty/*"]
    if database is None:
        roots = {Path(entry["directory"]) for entry in entries}
        includes = {parent / "include" for root in roots for parent in (root, *root.parents)
                    if (parent / "include").is_dir() and parent != Path("/")}
        command.extend("-I" + str(path) for path in sorted(includes))
    report = output / ("cppcheck-compile-report.xml" if database else "cppcheck-report.xml")
    receipt = common.execute(command, database.parent if database else Path(entries[0]["directory"]), output / "stdout.log", report)
    receipt["mode"] = "compile" if database else "source"
    try:
        tree = element_tree.parse(report)
        if tree.getroot().tag != "results" or tree.getroot().get("version") != "2" or tree.find("errors") is None:
            raise ValueError("无效的 Cppcheck XML")
        receipt["diagnostics"] = len(tree.findall(".//errors/error"))
    except (element_tree.ParseError, ValueError) as error:
        receipt["artifact_error"] = str(error)
    receipt["selected_units"] = len(entries)
    receipt["ok"] = receipt["exit_code"] == 0 and "artifact_error" not in receipt
    common.write_json(output / "receipt.json", receipt)
    return receipt


def report(result, group, output, entries, scopes, binaries):
    if not result.get("artifact_error"):
        reporting.generate(output, result, group, entries, scopes, POLICY, binaries)


def export_evidence(output, target, result):
    return reporting.export_evidence(output, target, result)


def main(argv=None):
    from session import main as session_main
    return session_main((sys.modules[__name__],), argv)


if __name__ == "__main__":
    raise SystemExit("Use cpp_check/scan.sh.")
