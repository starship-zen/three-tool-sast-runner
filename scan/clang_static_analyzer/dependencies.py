from pathlib import Path
import shutil
import common


def parse_dependencies(text, directory):
    text = text.replace("\\\n", "").replace("$$", "$")
    if not text.startswith("scanner-dependencies:"):
        raise ValueError("Invalid dependency target")
    names = []
    token = ""
    escaped = False
    for character in text.split(":", 1)[1]:
        if escaped:
            token += character
            escaped = False
        elif character == "\\":
            escaped = True
        elif character.isspace():
            if token:
                names.append(token)
                token = ""
        else:
            token += character
    if escaped:
        raise ValueError("Incomplete dependency escape")
    if token:
        names.append(token)
    if not names:
        raise ValueError("Empty compiler dependency list")
    return [str(Path(directory) / name) for name in names]


def capture(entries, output, binaries, arguments, relax, snapshot=True):
    output.mkdir(parents=True)
    files = set()
    receipts = []
    errors = []
    for entry in entries:
        identity = common.unit_id(entry)
        dependency_file = output / (identity + ".d")
        compiler = binaries["clang"] if Path(entry["file"]).suffix == ".c" else binaries["clang++"]
        try:
            command = [compiler, *arguments(entry), *relax, "-M", "-MT", "scanner-dependencies",
                       "-MF", str(dependency_file), entry["file"]]
            receipt = common.execute(command, Path(entry["directory"]), output / (identity + ".out.log"),
                                     output / (identity + ".err.log"))
            receipt["file"] = entry["file"]
            receipts.append(receipt)
            if receipt["exit_code"] != 0:
                raise ValueError("Dependency preprocessing failed: " + entry["file"])
            files.update(parse_dependencies(dependency_file.read_text(), entry["directory"]))
            files.add(entry["file"])
            for parent in (Path(entry["file"]).parent, *Path(entry["file"]).parents):
                for name in (".clang-tidy", ".clang-format", ".clangd"):
                    candidate = parent / name
                    if candidate.is_file():
                        files.add(str(candidate))
        except (OSError, ValueError) as error:
            errors.append(str(error))
    hashes = {}
    resolved = {}
    snapshots = {}
    for name in sorted(files):
        try:
            hashes[name] = common.digest(Path(name))
            resolved[name] = str(Path(name).resolve(strict=True))
            if snapshot:
                relative = "content/" + hashes[name]
                target = output / relative
                target.parent.mkdir(exist_ok=True)
                if not target.exists():
                    shutil.copyfile(name, target)
                if common.digest(target) != hashes[name]:
                    raise ValueError("Dependency changed while snapshotting: " + name)
                snapshots[hashes[name]] = relative
        except (OSError, ValueError) as error:
            errors.append(str(error))
    result = {"ok": not errors, "coverage": "clang_compile_context_including_system_headers",
              "offline_reproducible": False, "immutable_inputs": False,
              "limitations": ["Only discovered dependency contents are snapshotted, not the complete build/SDK/toolchain",
                              "Cppcheck alternate preprocessor configurations are not certified",
                              "Modules, compiler plugins, runtime shared libraries and complete SDK contents are not snapshotted",
                              "Before/after checks cannot detect transient edits restored during a scan"],
              "files": hashes, "resolved_paths": resolved, "environment": common.environment(),
              "snapshots": snapshots,
              "receipts": receipts, "errors": errors}
    common.write_json(output / "baseline.json", result)
    return result
