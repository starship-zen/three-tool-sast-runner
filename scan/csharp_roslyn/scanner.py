import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
import common
from . import reporting

NAME = "csharp_roslyn"
JOBS = 4
SUPPORTS_SOURCE = True
REQUIRES_DATABASE = False
INPUT_SUFFIXES = common.CSHARP_SUFFIXES
IGNORED_PATH_PARTS = ("bin", "obj")
REQUIRED_TOOLS = ("dotnet",)
REPORT_PRIORITY = 3
ANALYZERS = "Microsoft.CodeAnalysis.NetAnalyzers"
# The .NET SDK bundles NetAnalyzers; its build props publish the bundled
# edition under this property when no NuGet package overrides it.
ANALYZER_SDK_VERSION_PROPERTY = "_NETAnalyzersSDKAssemblyVersion"
ANALYZER_ASSETS_SOURCE = "project.assets.json"
ANALYZER_SDK_SOURCE = "dotnet-sdk"
TARGET_FRAMEWORK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_TARGET_FRAMEWORKS = 32
STATUS_ANALYZED = "analyzed"
STATUS_FAILED = "failed"
STATUS_DISCOVERED = "discovered"
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
    """Directory enumeration of `*.cs` files; never a completed-unit inventory."""
    root = project.parent
    return sorted(path.resolve() for path in root.rglob("*.cs")
                  if path.is_file() and not {"bin", "obj"}.intersection(path.relative_to(root).parts))


def msbuild(dotnet, project, arguments, output, label):
    command = [dotnet, "build", str(project), *arguments]
    receipt = common.execute(command, project.parent, output / (label + ".stdout.log"),
                             output / (label + ".stderr.log"))
    return command, receipt


def msbuild_json(receipt, label):
    text = Path(receipt["stdout"]).read_text(encoding="utf-8", errors="replace")
    start = text.find("{")
    if start < 0:
        raise ValueError(label + " 未返回 JSON 输出（dotnet 退出码 " + str(receipt["exit_code"]) + "）")
    try:
        document, _end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as error:
        raise ValueError(label + " 输出不是有效 JSON: " + str(error)) from error
    if not isinstance(document, dict):
        raise ValueError(label + " 输出不是 JSON 对象")
    return document


def project_properties(dotnet, project, output, label, names, framework=None):
    arguments = ["-getProperty:" + name for name in names] + ["-nologo", "-v:q"]
    if framework:
        arguments.append("-p:TargetFramework=" + framework)
    _command, receipt = msbuild(dotnet, project, arguments, output, label)
    if receipt["exit_code"] != 0:
        raise ValueError(label + " 失败: dotnet build 退出码 " + str(receipt["exit_code"]))
    document = msbuild_json(receipt, label)
    properties = document.get("Properties")
    if not isinstance(properties, dict):
        raise ValueError(label + " 输出缺少 Properties 对象")
    return {name: str(properties.get(name) or "").strip() for name in names}


def target_frameworks(dotnet, project, output):
    """Declared target frameworks; multi-targeting projects build each of them."""
    properties = project_properties(dotnet, project, output, "target-frameworks",
                                    ("TargetFrameworks", "TargetFramework"))
    declared = properties["TargetFrameworks"] or properties["TargetFramework"]
    frameworks = [item.strip() for item in declared.split(";") if item.strip()]
    if not frameworks:
        # A solution file declares no target framework, and this lane builds one
        # project: fail closed instead of guessing a framework.
        raise ValueError("项目未声明 TargetFramework 或 TargetFrameworks，无法获取真实编译单元: " + str(project))
    if len(frameworks) > MAX_TARGET_FRAMEWORKS:
        raise ValueError("TargetFramework 数量超出上限 " + str(MAX_TARGET_FRAMEWORKS))
    if len(set(frameworks)) != len(frameworks):
        raise ValueError("TargetFramework 声明重复: " + declared)
    for framework in frameworks:
        if not TARGET_FRAMEWORK_RE.fullmatch(framework):
            raise ValueError("TargetFramework 标识不安全，无法用于构建与产物命名: " + framework)
    return frameworks


def compile_items(dotnet, project, framework, output):
    """The authoritative per-framework compilation-unit inventory.

    `-getItem:Compile` reports the evaluated MSBuild Compile items, so files that
    the project excludes or that a target only generates at build time are not
    reported as analyzed source. There is no directory-enumeration fallback.
    """
    label = "compile-items." + framework
    _command, receipt = msbuild(dotnet, project, ["-getItem:Compile", "-p:TargetFramework=" + framework,
                                                  "-nologo", "-v:q"], output, label)
    if receipt["exit_code"] != 0:
        raise ValueError("无法读取 " + framework + " 的 Compile 编译单元: dotnet build 退出码 "
                         + str(receipt["exit_code"]))
    document = msbuild_json(receipt, label)
    items = document.get("Items")
    declared = items.get("Compile") if isinstance(items, dict) else None
    if not isinstance(declared, list) or not declared:
        raise ValueError("TargetFramework " + framework + " 未声明任何 Compile 编译单元")
    paths = set()
    for item in declared:
        path = item.get("FullPath") if isinstance(item, dict) else None
        if not isinstance(path, str) or not path or not Path(path).is_absolute():
            raise ValueError("TargetFramework " + framework + " 的 Compile 编译单元缺少绝对路径")
        paths.add(str(Path(path).resolve()))
    return sorted(paths)


def asset_analyzer_versions(path):
    """Microsoft.CodeAnalysis.NetAnalyzers versions recorded by restore."""
    if not path.is_file():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("无法解析还原产物 " + str(path) + ": " + str(error)) from error
    versions = set()
    for section in ("libraries", "targets"):
        block = document.get(section)
        if not isinstance(block, dict):
            continue
        for key, packages in block.items():
            if section == "targets":
                if not isinstance(packages, dict):
                    continue
                keys = list(packages)
            else:
                keys = [key]
            for entry in keys:
                name, _separator, version = entry.partition("/")
                if name.lower() == ANALYZERS.lower() and version:
                    versions.add(version)
    return sorted(versions)


def analyzer_identity(dotnet, project, frameworks, output):
    """Bind the NetAnalyzers identity, not the C# compiler that emits the SARIF."""
    limitations = []
    entries = {}
    assets_files = set()
    for framework in frameworks:
        properties = project_properties(dotnet, project, output, "analyzer-version." + framework,
                                        (ANALYZER_SDK_VERSION_PROPERTY, "ProjectAssetsFile"),
                                        framework=framework)
        if properties["ProjectAssetsFile"]:
            assets_files.add(properties["ProjectAssetsFile"])
        entries[framework] = {"sdk_version": properties[ANALYZER_SDK_VERSION_PROPERTY] or None}
    packages = set()
    missing_assets = {path for path in assets_files if not Path(path).is_file()}
    if missing_assets or not assets_files:
        limitations.append("未找到项目还原产物 project.assets.json，无法确认 NetAnalyzers 包版本")
    for path in sorted(assets_files - missing_assets):
        try:
            packages.update(asset_analyzer_versions(Path(path)))
        except (OSError, ValueError) as error:
            limitations.append(str(error))
    if len(packages) > 1:
        limitations.append("还原产物解析到多个 Microsoft.CodeAnalysis.NetAnalyzers 版本: "
                           + ", ".join(sorted(packages)))
    for framework, entry in entries.items():
        if len(packages) == 1:
            entry.update(version=next(iter(packages)), source=ANALYZER_ASSETS_SOURCE)
        elif entry["sdk_version"]:
            entry.update(version=entry["sdk_version"], source=ANALYZER_SDK_SOURCE)
        else:
            entry.update(version=None, source=None)
    resolved = {entry["version"] for entry in entries.values() if entry["version"]}
    sources = {entry["source"] for entry in entries.values() if entry["source"]}
    if len(resolved) == 1 and len(sources) == 1:
        version, source = next(iter(resolved)), next(iter(sources))
    else:
        version, source = None, None
        if len(resolved) > 1:
            limitations.append("不同 TargetFramework 解析到不同的分析器版本: " + ", ".join(sorted(resolved)))
        if not resolved and not limitations:
            limitations.append("无法确定 Microsoft.CodeAnalysis.NetAnalyzers 版本")
    return {"name": ANALYZERS, "version": version, "source": source,
            "assets_files": sorted(assets_files), "package_versions": sorted(packages),
            "by_target_framework": entries, "limitations": limitations}


def build_framework(dotnet, project, framework, output):
    """One isolated build per target framework with its own SARIF error log."""
    build = {"target_framework": framework}
    try:
        items = compile_items(dotnet, project, framework, output)
    except (OSError, ValueError) as error:
        build.update(units_attempted=0, units_completed=0, diagnostics=0, ok=False, error=str(error))
        return build, []
    sarif = output / reporting.sarif_name(framework)
    # MSBuild splits `-p:` values on commas, so the documented
    # `ErrorLog=<file>,version=2.1` form would reach the compiler without the
    # version and emit legacy SARIF 1.0. The comma must be percent-encoded for
    # MSBuild to forward `version=2.1` to csc; verified with .NET SDK 8.0.404.
    command, receipt = msbuild(dotnet, project, [*FLAGS, "-p:TargetFramework=" + framework,
                                                 "-p:ErrorLog=" + str(sarif) + "%2Cversion=2.1",
                                                 "--no-incremental"], output, "build." + framework)
    build.update(receipt)
    build.update({"sarif": str(sarif), "compile_items": items, "units_attempted": len(items),
                  "units_completed": 0, "diagnostics": 0})
    runs = []
    try:
        runs = reporting.read_runs(sarif)
        build["diagnostics"] = len(reporting.parse_sarif(sarif))
        build["sarif_sha256"] = common.digest(sarif)
    except (OSError, ValueError) as error:
        build["error"] = str(error)
    build["ok"] = receipt["exit_code"] == 0 and "error" not in build
    if build["ok"]:
        build["units_completed"] = len(items)
    else:
        build.setdefault("error", "dotnet build 退出码 " + str(receipt["exit_code"]))
    return build, [(framework, run) for run in runs]


def annotate_run(run, framework):
    annotated = dict(run)
    properties = run.get("properties")
    properties = dict(properties) if isinstance(properties, dict) else {}
    properties["targetFramework"] = framework
    annotated["properties"] = properties
    return annotated


def write_reconciliation(output, builds, discovered):
    rows = []
    for build in builds:
        status = STATUS_ANALYZED if build["ok"] else STATUS_FAILED
        rows.extend((build["target_framework"], path, status) for path in build.get("compile_items", []))
    rows.extend(("", str(path), STATUS_DISCOVERED) for path in discovered)
    with (output / reporting.RECONCILIATION_NAME).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("tfm", "file", "status"))
        writer.writerows(sorted(rows))


def scan(entries, scopes, database, output, binaries):
    output.mkdir(parents=True)
    project = find_project(entries, scopes)
    dotnet = binaries["dotnet"]
    started = datetime.now(timezone.utc).isoformat()
    errors = []
    frameworks = []
    try:
        frameworks = target_frameworks(dotnet, project, output)
    except (OSError, ValueError) as error:
        errors.append(str(error))
    builds = []
    merged = []
    for framework in frameworks:
        build, runs = build_framework(dotnet, project, framework, output)
        builds.append(build)
        if build.get("error"):
            errors.append(framework + ": " + build["error"])
        merged.extend(annotate_run(run, name) for name, run in runs)
    sarif = output / reporting.SARIF_NAME
    if merged:
        reporting.write_report(sarif, merged)
    analyzers = analyzer_identity(dotnet, project, frameworks, output) if frameworks else {
        "name": ANALYZERS, "version": None, "source": None, "assets_files": [],
        "package_versions": [], "by_target_framework": {},
        "limitations": ["未执行任何 TargetFramework 构建，无法确定分析器版本"]}
    compiled = {path for build in builds for path in build.get("compile_items", [])}
    discovered = [path for path in project_sources(project) if str(path) not in compiled]
    if discovered:
        listing = ", ".join(str(path) for path in discovered[:10])
        errors.append("以下源文件不是项目任何 TargetFramework 的 Compile 编译单元，未被分析: " + listing
                      + (" 等 " + str(len(discovered)) + " 个" if len(discovered) > 10 else ""))
    completed = sum(build["units_completed"] for build in builds)
    ok = bool(builds) and not discovered and all(build["ok"] for build in builds)
    receipt = {
        "cwd": str(project.parent),
        "project": str(project),
        "policy": POLICY,
        "database": str(database) if database else None,
        "selected_files": [entry["file"] for entry in entries],
        "target_frameworks": frameworks,
        "analyzers": analyzers,
        "builds": builds,
        "sarif": str(sarif) if merged else None,
        "diagnostics": sum(build["diagnostics"] for build in builds),
        "units_attempted": sum(build["units_attempted"] for build in builds),
        "units_completed": completed,
        "discovered": len(discovered),
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
        "exit_code": 0 if ok else next((build.get("exit_code") for build in builds if build.get("exit_code")), 1),
        "ok": ok,
    }
    if errors:
        receipt["error"] = "; ".join(errors)
    write_reconciliation(output, builds, discovered)
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
