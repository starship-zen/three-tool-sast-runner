import csv
import os
import sys
from pathlib import Path
import common
from . import reporting

NAME = "csharp_roslyn"
JOBS = 4
SUPPORTS_SOURCE = True
REQUIRES_DATABASE = False
INPUT_SUFFIXES = common.CSHARP_SUFFIXES
REQUIRED_TOOLS = ("dotnet",)
REPORT_PRIORITY = 3
ANALYZERS = "Microsoft.CodeAnalysis.NetAnalyzers"
FLAGS = ("-p:RunAnalyzersDuringBuild=true", "-p:EnableNETAnalyzers=true",
         "-p:AnalysisLevel=latest", "-p:AnalysisModeSecurity=AllEnabledByDefault")
POLICY = {"jobs": JOBS, "analyzers": ANALYZERS, "flags": list(FLAGS)}
PROJECT_PATTERNS = ("*.sln", "*.csproj")


def resolve_tools():
    candidates = ["/usr/bin/dotnet", "/usr/local/share/dotnet/dotnet", os.path.expanduser("~/.dotnet/dotnet")]
    return {"dotnet": common.find_tool("dotnet", candidates)}


def find_project(entries, scopes):
    located = set()
    for entry in entries:
        file_path = Path(entry["file"]).resolve()
        proj = None
        for folder in (file_path.parent, *file_path.parents):
            for pattern in PROJECT_PATTERNS:
                projects = sorted(path.resolve() for path in folder.glob(pattern) if path.is_file())
                if len(projects) == 1:
                    proj = projects[0]
                    break
                elif len(projects) > 1:
                    raise ValueError("目录中存在多个 " + pattern + " 项目文件，无法确定被测项目: " + folder.as_posix())
            if proj:
                break
        if proj:
            located.add(proj)
        else:
            raise ValueError("源文件未找到所属 .sln 或 .csproj 项目文件: " + str(file_path))
    if len(located) == 1:
        return next(iter(located))
    if len(located) > 1:
        raise ValueError("输入包含多个独立的 C# 项目文件，请通过独立扫描分别执行: " + ", ".join(str(p) for p in sorted(located)))
    raise ValueError("未找到 .sln 或 .csproj 项目文件: " + ", ".join(map(str, scopes)))


def project_sources(project):
    root = project.parent
    return sorted(path.resolve() for path in root.rglob("*.cs")
                  if path.is_file() and not {"bin", "obj"}.intersection(path.relative_to(root).parts))


def write_reconciliation(output, sources, ok):
    with (output / reporting.RECONCILIATION_NAME).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("file", "status"))
        writer.writerows((str(path), "discovered" if ok else "failed") for path in sources)


def scan(entries, scopes, database, output, binaries):
    output.mkdir(parents=True)
    project = find_project(entries, scopes)
    sarif = output / reporting.SARIF_NAME
    command = [binaries["dotnet"], "build", str(project), *FLAGS,
               "-p:ErrorLog=" + str(sarif) + ",version=2.1", "--no-incremental"]
    receipt = common.execute(command, project.parent, output / "stdout.log", output / "stderr.log")
    receipt.update({"project": str(project), "sarif": str(sarif), "policy": POLICY,
                    "database": str(database) if database else None})
    try:
        receipt["diagnostics"] = len(reporting.parse_sarif(sarif))
    except (OSError, ValueError) as error:
        receipt["artifact_error"] = str(error)
    receipt["ok"] = receipt["exit_code"] == 0 and "artifact_error" not in receipt
    write_reconciliation(output, project_sources(project), receipt["ok"])
    common.write_json(output / "receipt.json", receipt)
    return receipt


def report(result, group, output, entries, scopes, binaries):
    reporting.generate(output, result, group, entries, scopes, POLICY, binaries)


def export_evidence(output, target, result):
    return reporting.export_evidence(output, target, result)


def main(argv=None):
    from session import main as session_main
    return session_main((sys.modules[__name__],), argv)


if __name__ == "__main__":
    raise SystemExit("Use csharp_roslyn/scan.sh.")
