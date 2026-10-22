import json
import re
import sys
from pathlib import Path
import common
from . import reporting

NAME = "clang_tidy"
JOBS = 4
SUPPORTS_SOURCE = False
INPUT_SUFFIXES = common.CPP_SOURCE_SUFFIXES
REQUIRED_TOOLS = ("clang-tidy",)
REPORT_PRIORITY = 2
TIDY_CHECKS = ",".join((
    "-*", "clang-analyzer-*", "bugprone-*", "performance-*",
    "-bugprone-narrowing-conversions", "-bugprone-macro-parentheses",
    "-bugprone-branch-clone", "-bugprone-lambda-function-name",
    "-performance-unnecessary-value-param", "-performance-no-automatic-move",
    "-performance-unnecessary-copy-initialization", "-clang-diagnostic-*",
))
POLICY = {"jobs": JOBS, "checks": TIDY_CHECKS, "source_fallback": False}


def resolve_tools():
    candidates = common.llvm_candidates("clang-tidy")
    candidates.extend(str(path) for path in sorted(
        (common.ROOT / "install/clang_tidy/tmp/tooling/usr/lib").glob("llvm-*/bin/clang-tidy"), reverse=True))
    return {"clang-tidy": common.find_tool("clang-tidy", candidates)}


def header_pattern(scopes):
    patterns = []
    for scope in scopes:
        escaped = re.escape(str(scope))
        if scope.is_file():
            patterns.append(escaped + "$")
        else:
            patterns.append(escaped + "/[^/]+$")
            for child in sorted(scope.iterdir()):
                if child.is_dir() and child.name != "thirdparty" and common.in_scope(child, [scope]):
                    patterns.append(re.escape(str(child)) + "(/|$)")
    return "^(" + "|".join(patterns) + ")"


def scan(entries, scopes, database, output, binaries):
    output.mkdir(parents=True)
    logs = output / "logs"
    logs.mkdir()
    fixes = output / "fixes"
    fixes.mkdir()
    header_filter = header_pattern(scopes)

    def run_one(entry):
        identity = common.unit_id(entry)
        artifact = fixes / (identity + ".yaml")
        config = json.dumps({"Checks": TIDY_CHECKS, "WarningsAsErrors": "", "HeaderFilterRegex": header_filter})
        command = [binaries["clang-tidy"], "-p", str(database.parent), "--quiet", "--config=" + config,
                   "--export-fixes=" + str(artifact), entry["file"]]
        receipt = common.execute(command, Path(entry["directory"]), logs / (identity + ".out.log"),
                                 logs / (identity + ".err.log"))
        receipt.update({"file": entry["file"], "unit_id": identity, "artifact": str(artifact),
                        "ok": receipt["exit_code"] == 0})
        return receipt

    summary = common.collect_units(entries, output, run_one, JOBS)
    common.write_json(output / "clang-tidy-summary.json", summary)
    return summary


def report(result, group, output, entries, scopes, binaries):
    reporting.generate(output, result, group, entries, scopes, POLICY, binaries)


def export_evidence(output, target, result):
    return reporting.export_evidence(output, target, result)


def main(argv=None):
    from session import main as session_main
    return session_main((sys.modules[__name__],), argv)


if __name__ == "__main__":
    raise SystemExit("Use clang_tidy/scan.sh.")
