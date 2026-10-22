import plistlib
import csv
import sys
from pathlib import Path
from xml.parsers.expat import ExpatError
import common
from . import reporting

NAME = "clang_static_analyzer"
JOBS = 4
SUPPORTS_SOURCE = False
REQUIRED_TOOLS = ("clang", "clang++")
REPORT_PRIORITY = 0
RELAX = ["-Wno-return-type-c-linkage", "-Wno-non-pod-varargs", "-Wno-invalid-constexpr"]
POLICY = {"jobs": JOBS, "output": ["plist", "html"], "relax": RELAX, "source_fallback": False}


def resolve_tools():
    return {name: common.find_tool(name, common.llvm_candidates(name)) for name in REQUIRED_TOOLS}


def clean_forwarded(arguments):
    output_options = {"-o", "-MF", "-MT", "-MQ", "-MJ", "-dependency-file", "-dependency-dot",
                      "-serialize-diagnostics", "--serialize-diagnostics", "-serialize-diagnostic-file", "-diagnostic-log-file"}
    standalone_outputs = {"-M", "-MM", "-MD", "-MMD", "-MP", "-MG"}
    value_options = {"-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros", "-D", "-U",
                     "-x", "-target", "-triple", "-isysroot", "-resource-dir", "-std", "-load", "-plugin", "-add-plugin"}
    result = []
    position = 0
    while position < len(arguments):
        token = arguments[position]
        if token.startswith("@"):
            raise ValueError("CSA requires expanded response files in compile_commands.json: " + token)
        if token in output_options | value_options:
            if position + 1 >= len(arguments):
                raise ValueError("Missing forwarded compiler option value: " + token)
            if token in value_options:
                result.extend(arguments[position:position + 2])
            position += 2
            continue
        if token in ("-MD", "-MMD") and position + 1 < len(arguments) and not arguments[position + 1].startswith("-"):
            position += 2
            continue
        if token in standalone_outputs or any(token.startswith(option + "=") for option in output_options) or token.startswith(("-MF", "-MT", "-MQ", "-MJ")):
            position += 1
            continue
        if token.startswith("-o") and not token.startswith(("-objc", "-object", "-offload", "-openmp", "-opt")):
            position += 1
            continue
        result.append(token)
        position += 1
    return result


def compiler_arguments(entry):
    tokens = list(entry["arguments"])
    if Path(tokens[0]).name in ("ccache", "sccache"):
        tokens.pop(0)
    tokens = tokens[1:]
    result = []
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token.startswith("@"):
            raise ValueError("CSA requires expanded response files in compile_commands.json: " + token)
        if token == "--":
            position += 1
            continue
        if token in ("-Xlinker", "-L", "-l"):
            if position + 1 >= len(tokens):
                raise ValueError("Missing compiler option value: " + token)
            position += 2
            continue
        if token.startswith(("-Wl,", "-L")) or (token.startswith("-l") and not token.startswith(("-load", "-lazy", "-lto"))):
            position += 1
            continue
        if token in ("-Xclang", "-Xpreprocessor"):
            forwarded = []
            channel = token
            while position < len(tokens) and tokens[position] == channel:
                if position + 1 >= len(tokens):
                    raise ValueError("Missing compiler option value: " + channel)
                forwarded.append(tokens[position + 1])
                position += 2
            for argument in clean_forwarded(forwarded):
                result.extend((channel, argument))
            continue
        if token.startswith("-Wp,"):
            forwarded = clean_forwarded(token[4:].split(","))
            if forwarded:
                result.append("-Wp," + ",".join(forwarded))
            position += 1
            continue
        if token in ("-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros", "-D", "-U",
                     "-x", "-target", "--target", "--sysroot", "-isysroot", "-resource-dir", "-std",
                     "-Xclang", "-Xpreprocessor", "-Xassembler", "-arch", "-B"):
            if position + 1 >= len(tokens):
                raise ValueError("Missing compiler option value: " + token)
            result.extend(tokens[position:position + 2])
            position += 2
            continue
        if token in ("-o", "-MF", "-MT", "-MQ", "-MJ", "--serialize-diagnostics", "-serialize-diagnostics"):
            if position + 1 >= len(tokens):
                raise ValueError("Missing compiler option value: " + token)
            position += 2
            continue
        if token in ("-c", "-S", "-E", "-MD", "-MMD", "-MP", "-MG", "-M", "-MM"):
            position += 1
            continue
        if (token.startswith("-o") and token != "-o" and not token.startswith(("-objc", "-object", "-offload", "-openmp", "-opt"))) or any(
                token.startswith(prefix) and token != prefix for prefix in ("-MF", "-MT", "-MQ", "-MJ")):
            position += 1
            continue
        if token.startswith(("--serialize-diagnostics=", "-serialize-diagnostics=", "-fdiagnostics-color")):
            position += 1
            continue
        if not token.startswith("-") and (Path(entry["directory"]) / token).resolve() == Path(entry["file"]):
            position += 1
            continue
        result.append(token)
        position += 1
    return result


def scan(entries, scopes, database, output, binaries):
    output.mkdir(parents=True)
    logs = output / "logs"
    logs.mkdir()
    reports = output / "reports"
    reports.mkdir()

    def run_one(entry):
        identity = common.unit_id(entry)
        artifact = reports / (identity + ".plist")
        compiler = binaries["clang"] if Path(entry["file"]).suffix == ".c" else binaries["clang++"]
        command = [compiler, "--analyze", *compiler_arguments(entry), *RELAX,
                   "-Xanalyzer", "-analyzer-output=plist", "-o", str(artifact), entry["file"]]
        receipt = common.execute(command, Path(entry["directory"]), logs / (identity + ".out.log"),
                                 logs / (identity + ".err.log"))
        receipt.update({"file": entry["file"], "unit_id": identity, "artifact": str(artifact),
                        "ok": receipt["exit_code"] == 0})
        try:
            with artifact.open("rb") as stream:
                diagnostics = plistlib.load(stream)["diagnostics"]
            receipt["diagnostics"] = len(diagnostics)
            html_dir = reports / identity
            html_dir.mkdir()
            html_command = command.copy()
            html_command[html_command.index("-analyzer-output=plist")] = "-analyzer-output=html"
            html_command[html_command.index("-o") + 1] = str(html_dir)
            html_receipt = common.execute(html_command, Path(entry["directory"]),
                                          logs / (identity + ".html.out.log"), logs / (identity + ".html.err.log"))
            receipt["html_execution"] = html_receipt
            receipt["html_reports"] = [str(path) for path in sorted(html_dir.glob("*.html"))]
            receipt["ok"] = receipt["ok"] and html_receipt["exit_code"] == 0
            if diagnostics and not receipt["html_reports"]:
                receipt.update({"ok": False, "artifact_error": "CSA diagnostics have no HTML report"})
        except (OSError, ValueError, KeyError, TypeError, ExpatError) as error:
            receipt.update({"ok": False, "artifact_error": str(error)})
        return receipt

    summary = common.collect_units(entries, output, run_one, JOBS)
    with (output / "tu-mapping.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("slug", "file", "diagnostics", "exit_code"))
        writer.writerows((item["unit_id"], item["file"], item.get("diagnostics", ""), item["exit_code"])
                         for item in summary["files"])
    common.write_json(output / "analyzer-status.json", summary)
    return summary


def capture_inputs(entries, output, binaries):
    from .dependencies import capture
    return capture(entries, output, binaries, compiler_arguments, RELAX, snapshot=output.name == "before")


def report(result, group, output, entries, scopes, binaries):
    reporting.generate(output, result, group, entries, scopes, POLICY, binaries)


def export_evidence(output, target, result):
    return reporting.export_evidence(output, target, result)


def main(argv=None):
    from session import main as session_main
    return session_main((sys.modules[__name__],), argv)


if __name__ == "__main__":
    raise SystemExit("Use clang_static_analyzer/scan.sh.")
