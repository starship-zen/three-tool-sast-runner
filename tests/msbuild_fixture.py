"""Fake `dotnet` used by the C# scanner tests.

The scanner drives MSBuild through `dotnet build -getProperty:`,
`dotnet build -getItem:Compile` and `dotnet build -p:ErrorLog=`. This module
writes an executable stand-in that answers those three calls from an explicit
fixture, so tests can vary the declared target frameworks, the Compile item
inventory, the emitted SARIF and the restore assets without a .NET SDK.
"""

import json
import sys
from pathlib import Path

SCRIPT = '''#!{python}
"""Synthetic `dotnet` answering the MSBuild queries the C# scanner issues."""
import json
import sys
from pathlib import Path

CONFIG = json.loads({config!r})


def option(argv, name):
    prefix = name + "="
    return next((item[len(prefix):] for item in argv if item.startswith(prefix)), "")


def write_assets():
    path = Path(CONFIG["assets_file"])
    document = {{"version": 3, "targets": {{}}, "libraries": {{}}}}
    version = CONFIG.get("assets_analyzer_version")
    if version:
        key = "Microsoft.CodeAnalysis.NetAnalyzers/" + version
        document["libraries"][key] = {{"type": "package", "serviceable": True}}
        document["targets"]["net8.0"] = {{key: {{}}}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def main(argv):
    if argv[:1] == ["--version"]:
        print(CONFIG.get("sdk_version", "8.0.100"))
        return 0
    if argv[:1] != ["build"]:
        sys.stderr.write("fake dotnet only emulates `build`\\n")
        return 2
    if CONFIG.get("write_assets", True):
        write_assets()
    properties = [item.split(":", 1)[1] for item in argv if item.startswith("-getProperty:")]
    items = [item.split(":", 1)[1] for item in argv if item.startswith("-getItem:")]
    framework = option(argv, "-p:TargetFramework")
    error_log = option(argv, "-p:ErrorLog")
    if properties:
        values = dict(CONFIG["properties"])
        values.setdefault("ProjectAssetsFile", CONFIG["assets_file"])
        if len(properties) == 1:
            print(values.get(properties[0], ""))
        else:
            print(json.dumps({{"Properties": {{name: values.get(name, "") for name in properties}}}}))
        return 0
    if items:
        if items != ["Compile"] or CONFIG.get("fail_item_query"):
            sys.stderr.write("error MSB4064: synthetic Compile item query failed\\n")
            return 1
        declared = CONFIG["compile_items"].get(framework)
        if declared is None:
            sys.stderr.write("error NETSDK1045: synthetic target framework is unsupported\\n")
            return 1
        print(json.dumps({{"Items": {{"Compile": [{{"Identity": path, "FullPath": path}}
                                               for path in declared]}}}}))
        return 0
    if error_log:
        exit_code = CONFIG.get("build_exit_code", 0)
        if exit_code:
            sys.stderr.write("error CS0001: synthetic build failure\\n")
            return exit_code
        # MSBuild receives the comma percent-encoded so it survives `-p:` value
        # splitting; the real compiler decodes it back to `,version=2.1`.
        path, _, version = error_log.replace("%2C", ",").partition(",")
        if version != "version=2.1":
            sys.stderr.write("error CS8022: missing SARIF log version\\n")
            return 1
        results = CONFIG["results"].get(framework)
        if results is None:
            sys.stderr.write("error NETSDK1045: synthetic target framework is unsupported\\n")
            return 1
        Path(path).write_text(json.dumps({{
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [{{
                "tool": {{"driver": {{"name": "Microsoft (R) Visual C# Compiler",
                                     "version": "4.11.0-3.24510.3 (cfe3bd0a)",
                                     "semanticVersion": "4.11.0",
                                     "dottedQuadFileVersion": "4.11.0.0"}}}},
                "results": results,
            }}],
        }}))
        return 0
    sys.stderr.write("fake dotnet received an unsupported command line\\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

DEFAULT_PROPERTIES = {"TargetFrameworks": "net8.0", "TargetFramework": "",
                      "_NETAnalyzersSDKAssemblyVersion": "8.0.0"}


def write_fake_dotnet(folder, project, compile_items, results=None, properties=None,
                      assets_analyzer_version=None, write_assets=True,
                      fail_item_query=False, build_exit_code=0, sdk_version="8.0.100"):
    """Write an executable `dotnet` fixture and return its path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    config = {
        "properties": dict(properties or DEFAULT_PROPERTIES),
        "compile_items": {name: list(paths) for name, paths in compile_items.items()},
        "results": dict(results or {name: [] for name in compile_items}),
        "assets_file": str(Path(project).resolve().parent / "obj" / "project.assets.json"),
        "assets_analyzer_version": assets_analyzer_version,
        "write_assets": write_assets,
        "fail_item_query": fail_item_query,
        "build_exit_code": build_exit_code,
        "sdk_version": sdk_version,
    }
    tool = folder / "fake-dotnet"
    tool.write_text(SCRIPT.format(python=sys.executable, config=json.dumps(config)))
    tool.chmod(0o755)
    return str(tool)
