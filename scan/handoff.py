#!/usr/bin/env python3
"""Export completed runner evidence as a portable, product-scoped delivery.

The delivery is scanner evidence, not a report manifest. It contains no
reporter checkout paths, standards or rule mappings. A receiving report system
is responsible for binding its locally approved taxonomy assets.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import errno
import fnmatch
import hashlib
import io
import json
import os
import plistlib
import re
import shutil
import stat
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any


RUNNER_SCHEMA = "three-tool-sast-runner-execution-1.0"
PROFILE_SCHEMA = "three-tool-product-profile-1.0"
DELIVERY_SCHEMA = "three-tool-scan-delivery-1.0"
RECEIPT_SCHEMA = "three-tool-scan-delivery-receipt-1.0"
TOOLS = ("cppcheck", "clang-tidy", "clang-static-analyzer")
RUNNER_NAMES = {
    "cppcheck": "cpp_check",
    "clang-tidy": "clang_tidy",
    "clang-static-analyzer": "clang_static_analyzer",
}
FORMATS = {
    "cppcheck": "cppcheck-xml",
    "clang-tidy": "clang-tidy-yaml",
    "clang-static-analyzer": "csa-plist",
}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"(?<![A-Za-z0-9])(?:v)?(\d+(?:\.\d+){1,3})(?![\d.])")
PRERELEASE_RE = re.compile(
    r"(?:\bdev\b|\b(?:alpha|beta|pre|rc)\b|\.dev\d*|-(?:alpha|beta|pre|rc)(?:[.-]?\d*)?)",
    re.I,
)
MAX_PROFILE_BYTES = 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_VERSION_TEXT_BYTES = 1024 * 1024
MAX_NATIVE_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_FILES = 200_000
MAX_UNITS = 100_000


class HandoffError(ValueError):
    """Input evidence cannot safely establish a complete scan delivery."""


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HandoffError("JSON input contains a duplicate key")
        result[key] = value
    return result


def _reject_symlink_components(value: Path, label: str) -> None:
    path = Path(os.path.abspath(value))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as error:
            raise HandoffError(label + " path is unavailable") from error
        if stat.S_ISLNK(info.st_mode):
            raise HandoffError(label + " path contains a symlink")


def _open_regular_file(path: Path, label: str, max_bytes: int):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HandoffError(label + " is unavailable or is a symlink") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HandoffError(label + " is not a regular file")
        if info.st_size < 0 or info.st_size > max_bytes:
            raise HandoffError(label + " exceeds the maximum supported file size")
        return os.fdopen(descriptor, "rb"), info
    except BaseException:
        os.close(descriptor)
        raise


def _read_file(path: Path, label: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    stream, before = _open_regular_file(path, label, max_bytes)
    with stream:
        raw = stream.read(max_bytes + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > max_bytes or len(raw) != after.st_size or before.st_ino != after.st_ino:
        raise HandoffError(label + " changed while being read or exceeds the maximum supported file size")
    return raw


def _sha256_file(path: Path, label: str, max_bytes: int = MAX_FILE_BYTES) -> tuple[str, int]:
    digest = hashlib.sha256()
    stream, before = _open_regular_file(path, label, max_bytes)
    total = 0
    with stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(chunk)
            if total > max_bytes:
                raise HandoffError(label + " exceeds the maximum supported file size")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if total != after.st_size or before.st_ino != after.st_ino:
        raise HandoffError(label + " changed while being hashed")
    return digest.hexdigest(), total


def _load_json(path: Path, label: str, max_bytes: int = MAX_JSON_BYTES) -> tuple[dict[str, Any], bytes]:
    raw = _read_file(path, label, max_bytes)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=lambda _: (_ for _ in ()).throw(HandoffError("invalid JSON number")),
        )
    except HandoffError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise HandoffError(label + " is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise HandoffError(label + " must contain a JSON object")
    return value, raw


def _safe_existing_directory(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    _reject_symlink_components(path, label)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise HandoffError(label + " is unavailable") from error
    if not resolved.is_dir():
        raise HandoffError(label + " is not a directory")
    return resolved


def _safe_new_output(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.name or path.name in {".", ".."}:
        raise HandoffError("output must name a new directory")
    requested_parent = Path(os.path.abspath(path.parent))
    _reject_symlink_components(requested_parent, "output parent")
    try:
        parent = requested_parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise HandoffError("output parent is unavailable") from error
    if not parent.is_dir():
        raise HandoffError("output parent must be an existing directory")
    output = parent / path.name
    if output.exists() or output.is_symlink():
        raise HandoffError("output already exists; refusing to overwrite")
    return output


def _safe_run_file(root: Path, raw_path: Any, label: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HandoffError(label + " path is missing")
    raw_candidate = Path(raw_path)
    if ".." in raw_candidate.parts:
        raise HandoffError(label + " path contains a parent traversal")
    candidate = raw_candidate if raw_candidate.is_absolute() else root / raw_candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise HandoffError(label + " escapes the runner run") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise HandoffError(label + " path contains a symlink")
    stream, _ = _open_regular_file(candidate, label, MAX_FILE_BYTES)
    stream.close()
    return candidate


def _safe_evidence_file(root: Path, relative_path: Any, label: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path:
        raise HandoffError(label + " must be a non-empty POSIX-relative path")
    if relative_path.startswith("/") or any(part in {"", ".", ".."} for part in relative_path.split("/")):
        raise HandoffError(label + " escapes the runner evidence bundle")
    current = root
    parts = PurePosixPath(relative_path).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError as error:
            raise HandoffError(label + " is unavailable") from error
        if stat.S_ISLNK(info.st_mode):
            raise HandoffError(label + " path contains a symlink")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise HandoffError(label + " path parent is not a directory")
        if index == len(parts) - 1 and not stat.S_ISREG(info.st_mode):
            raise HandoffError(label + " is not a regular file")
    return current


def _validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version", "product_id", "display_name", "repository", "component_id",
        "target_id", "configuration", "included_paths", "excluded_paths", "languages",
        "unsupported_languages",
    }
    if set(profile) != required:
        raise HandoffError("product profile must contain exactly the documented delivery fields")
    if profile.get("schema_version") != PROFILE_SCHEMA:
        raise HandoffError("unsupported product profile schema")
    for field in ("product_id", "component_id", "target_id", "configuration"):
        value = profile.get(field)
        if not isinstance(value, str) or not ID_RE.fullmatch(value):
            raise HandoffError("product profile " + field + " must be a stable safe identifier")
    for field in ("display_name", "repository"):
        value = profile.get(field)
        if not isinstance(value, str) or not value.strip():
            raise HandoffError("product profile " + field + " must be non-empty text")
    if re.search(r"https?://[^/@\s]+@", profile["repository"], re.I):
        raise HandoffError("repository label must not contain URL user information")
    for field in ("included_paths", "excluded_paths"):
        values = profile[field]
        if not isinstance(values, list) or (field == "included_paths" and not values):
            raise HandoffError("product profile " + field + " must be an explicit array")
        if any(
            not isinstance(item, str) or not item.strip() or "\\" in item
            or PurePosixPath(item).is_absolute() or ".." in PurePosixPath(item).parts
            for item in values
        ):
            raise HandoffError("product profile " + field + " contains an unsafe relative glob")
        if len(values) != len(set(values)):
            raise HandoffError("product profile " + field + " contains duplicate patterns")
    for field in ("languages", "unsupported_languages"):
        values = profile[field]
        if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
            raise HandoffError("product profile " + field + " must be an explicit string array")
        if len(values) != len(set(values)):
            raise HandoffError("product profile " + field + " contains duplicate values")
    if not profile["languages"] or any(item not in {"C", "C++"} for item in profile["languages"]):
        raise HandoffError("this delivery lane supports explicit C and/or C++ scope only")
    return profile


def _version_record(run_root: Path, entry: Any, name: str) -> tuple[dict[str, Any], bytes, bytes, str, str]:
    if (not isinstance(entry, dict) or type(entry.get("exit_code")) is not int or entry["exit_code"] != 0
            or entry.get("cancelled") is not False or entry.get("timed_out") is not False):
        raise HandoffError("missing successful captured version command for " + name)
    command = entry.get("command")
    if not isinstance(command, list) or len(command) < 2 or command[-1] != "--version":
        raise HandoffError("version command identity is incomplete for " + name)
    binary_hash = entry.get("binary_sha256")
    if not isinstance(binary_hash, str) or not SHA_RE.fullmatch(binary_hash):
        raise HandoffError("version command lacks a captured executable SHA-256 for " + name)
    stdout_path = _safe_run_file(run_root, entry.get("stdout"), name + " version stdout")
    stderr_path = _safe_run_file(run_root, entry.get("stderr"), name + " version stderr")
    stdout = _read_file(stdout_path, name + " version stdout", MAX_VERSION_TEXT_BYTES)
    stderr = _read_file(stderr_path, name + " version stderr", MAX_VERSION_TEXT_BYTES)
    text = (stdout + b"\n" + stderr).decode("utf-8", errors="replace").strip()
    if not text or PRERELEASE_RE.search(text):
        raise HandoffError("missing, prerelease, or development tool version evidence for " + name)
    if name == "cppcheck":
        match = re.search(r"\bCppcheck\s+(\d+(?:\.\d+){1,3})\b", text, re.I)
    else:
        match = VERSION_RE.search(text)
    if not match:
        raise HandoffError("cannot parse a stable numeric release token for " + name)
    token = match.group(1)
    parts = tuple(int(part) for part in token.split("."))
    minimum = (2, 22) if name == "cppcheck" else (14, 0)
    width = max(len(parts), len(minimum))
    if parts + (0,) * (width - len(parts)) < minimum + (0,) * (width - len(minimum)):
        raise HandoffError("tool version is below the runner evidence gate for " + name)
    return entry, stdout, stderr, text, token


def _copy_source(stage: Path, source: Path, relative: str, expected_sha256: str,
                 max_bytes: int = MAX_FILE_BYTES) -> dict[str, Any]:
    destination = stage / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    source_stream, before = _open_regular_file(source, "copy source " + source.name, max_bytes)
    try:
        with source_stream, destination.open("xb") as output:
            for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                total += len(chunk)
                if total > max_bytes:
                    raise HandoffError("copy source exceeds the maximum supported file size: " + source.name)
                digest.update(chunk)
                output.write(chunk)
            after = os.fstat(source_stream.fileno())
        if total != after.st_size or before.st_ino != after.st_ino:
            raise HandoffError("copy source changed while exporting: " + source.name)
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    actual = digest.hexdigest()
    if actual != expected_sha256:
        destination.unlink()
        raise HandoffError("copy source SHA-256 changed while exporting: " + source.name)
    return {"path": relative, "sha256": actual, "size": total}


def _write_json(stage: Path, relative: str, value: Any) -> dict[str, Any]:
    raw = _json_bytes(value)
    destination = stage / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(raw)
    return {"path": relative, "sha256": _sha256_bytes(raw), "size": len(raw)}


def _copy_bytes(stage: Path, relative: str, raw: bytes) -> dict[str, Any]:
    destination = stage / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(raw)
    return {"path": relative, "sha256": _sha256_bytes(raw), "size": len(raw)}


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux" and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(source), os.fsencode(destination), 0x00000004)
    else:
        raise HandoffError("atomic no-replace directory publication is unsupported on this platform")
    if result == 0:
        return
    error_code = ctypes.get_errno()
    if error_code in {errno.EEXIST, errno.ENOTEMPTY}:
        raise HandoffError("output appeared during export; refusing to overwrite")
    raise OSError(error_code, os.strerror(error_code), str(destination))


def _provenance(path: str, digest: str, purpose: str, **identity: Any) -> dict[str, Any]:
    return {"path": path, "sha256": digest, "purpose": purpose, **identity}


def _validate_run(run_root: Path, profile: dict[str, Any]) -> dict[str, Any]:
    run_manifest, run_manifest_raw = _load_json(run_root / "manifest.json", "runner execution manifest")
    run_id = run_manifest.get("run_id")
    if run_manifest.get("runner_schema") != RUNNER_SCHEMA:
        raise HandoffError("runner execution manifest lacks the canonical exporter marker")
    if (not isinstance(run_id, str) or not ID_RE.fullmatch(run_id) or run_root.name != run_id
            or Path(str(run_manifest.get("run_path", ""))).name != run_id):
        raise HandoffError("runner execution run_id does not match its output directory identity")
    if (run_manifest.get("ok") is not True or type(run_manifest.get("exit_code")) is not int
            or run_manifest["exit_code"] != 0):
        raise HandoffError("failed or incomplete scanner execution cannot be exported as a complete delivery")
    if run_manifest.get("cancelled") or run_manifest.get("error"):
        raise HandoffError("cancelled or errored runner execution cannot be exported")
    if run_manifest.get("evidence_gate_required") is not True or run_manifest.get("evidence_checks_ok") is not True:
        raise HandoffError("complete delivery requires the three-tool evidence gate to pass")
    if run_manifest.get("downstream_compatible") is not False or run_manifest.get("release_ready") is not False:
        raise HandoffError("runner evidence must not claim consumer compatibility or release readiness")

    groups = run_manifest.get("groups")
    if not isinstance(groups, list) or len(groups) != 1:
        raise HandoffError("one product delivery requires exactly one compile-database group")
    group = groups[0]
    if not isinstance(group, dict) or group.get("mode") != "compile" or not group.get("database"):
        raise HandoffError("this delivery lane requires a compilation-database run")
    if group.get("report_error") or group.get("evidence_error"):
        raise HandoffError("runner has a report or evidence error")

    selected = group.get("selected_files")
    if (not isinstance(selected, list) or not selected or len(selected) > MAX_UNITS
            or any(not isinstance(item, str) or not Path(item).is_absolute() for item in selected)
            or len(selected) != len(set(selected))):
        raise HandoffError("runner selected-file inventory is missing, non-absolute or duplicated")
    baseline = group.get("baseline")
    if not isinstance(baseline, dict) or not isinstance(baseline.get("git"), dict):
        raise HandoffError("run lacks a captured Git baseline")
    git = baseline["git"]
    commit = git.get("commit")
    repo_root = git.get("repo")
    if not git.get("ok") or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise HandoffError("run lacks a valid captured Git commit")
    if str(git.get("status", "")).strip() or str(git.get("diff", "")).strip():
        raise HandoffError("product delivery requires a clean captured Git baseline")
    source_root = baseline.get("root")
    if (not isinstance(repo_root, str) or not Path(repo_root).is_absolute()
            or ".." in Path(repo_root).parts or not isinstance(source_root, str)
            or not Path(source_root).is_absolute() or ".." in Path(source_root).parts):
        raise HandoffError("captured source root or Git repository root is missing or invalid")
    try:
        source_scope = Path(source_root).relative_to(Path(repo_root))
    except ValueError as error:
        raise HandoffError("captured source root is outside the Git repository") from error
    if ".." in source_scope.parts:
        raise HandoffError("captured source root is outside the Git repository")

    relative_units: dict[str, str] = {}
    for unit in selected:
        try:
            relative = Path(unit).relative_to(Path(repo_root)).as_posix()
        except ValueError as error:
            raise HandoffError("a selected translation unit is outside the captured Git repository") from error
        if not relative or ".." in Path(relative).parts:
            raise HandoffError("selected source path cannot be represented relative to the captured Git root")
        if not any(fnmatch.fnmatchcase(relative, pattern) for pattern in profile["included_paths"]):
            raise HandoffError("selected source falls outside the explicit product-profile include scope: " + relative)
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in profile["excluded_paths"]):
            raise HandoffError("selected source also matches an explicit product-profile exclusion: " + relative)
        suffix = Path(relative).suffix.lower()
        language = "C" if suffix == ".c" else "C++" if suffix in {".cc", ".cpp", ".cxx", ".c++"} else None
        if language is None or language not in profile["languages"]:
            raise HandoffError("selected source extension is outside the explicit C/C++ profile: " + relative)
        relative_units[unit] = relative
    source_hashes = baseline.get("sources")
    if not isinstance(source_hashes, dict) or set(source_hashes) != set(selected):
        raise HandoffError("captured source-hash inventory does not exactly match selected units")
    if any(not isinstance(source_hashes[unit], str) or not SHA_RE.fullmatch(source_hashes[unit]) for unit in selected):
        raise HandoffError("captured source hashes are incomplete")

    if group.get("evidence_bundle") != "group-1/evidence-bundle":
        raise HandoffError("runner evidence bundle path does not match the one-group run layout")
    evidence_root = run_root / "group-1/evidence-bundle"
    _reject_symlink_components(evidence_root, "runner evidence bundle")
    evidence_manifest, evidence_manifest_raw = _load_json(evidence_root / "manifest.json", "runner evidence manifest")
    if (evidence_manifest.get("schema") != "scanner-evidence-1"
            or evidence_manifest.get("target_contract") != "three-tool-input-1.0"
            or evidence_manifest.get("downstream_compatible") is not False
            or evidence_manifest.get("release_ready") is not False):
        raise HandoffError("runner evidence manifest is not a native scanner-evidence-1 execution bundle")
    if evidence_manifest.get("commit") != commit or evidence_manifest.get("selected_files") != selected:
        raise HandoffError("evidence manifest contradicts the captured source baseline")
    listed = evidence_manifest.get("artifacts")
    if not isinstance(listed, list) or not listed or len(listed) > MAX_FILES:
        raise HandoffError("runner evidence manifest has no valid artifact inventory")
    evidence_files: dict[str, Path] = {}
    evidence_hashes: dict[str, str] = {}
    evidence_sizes: dict[str, int] = {}
    total = 0
    for item in listed:
        if not isinstance(item, dict) or not SHA_RE.fullmatch(str(item.get("sha256", ""))):
            raise HandoffError("runner evidence manifest has an invalid artifact record")
        relative = item.get("path")
        path = _safe_evidence_file(evidence_root, relative, "runner evidence artifact")
        digest, size = _sha256_file(path, "runner evidence artifact " + relative)
        if type(item.get("size")) is not int or item["size"] != size:
            raise HandoffError("runner evidence artifact size differs from its captured inventory: " + relative)
        if digest != item["sha256"]:
            raise HandoffError("runner evidence artifact SHA-256 mismatch: " + relative)
        if relative in evidence_files:
            raise HandoffError("runner evidence manifest repeats an artifact path")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise HandoffError("runner evidence bundle exceeds the maximum supported total size")
        evidence_files[relative] = path
        evidence_hashes[relative] = digest
        evidence_sizes[relative] = size

    gate_path = evidence_files.get("release-gate.json")
    if gate_path is None:
        raise HandoffError("captured release gate is missing")
    gate, gate_raw = _load_json(gate_path, "runner evidence release gate")
    if (gate.get("ok") is not True or gate.get("errors") != [] or gate.get("release_ready") is not False
            or group.get("release_gate") != gate or evidence_manifest.get("release_gate") != gate):
        raise HandoffError("runner evidence gate is failed, inconsistent or claims release readiness")
    checks = gate.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(RUNNER_NAMES.values()):
        raise HandoffError("green evidence gate does not contain checks for all three scanners")
    for name, check in checks.items():
        if not isinstance(check, dict) or check.get("errors") != [] or set(check.get("files", [])) != set(selected):
            raise HandoffError("green evidence gate does not reconcile selected translation units: " + name)
    if evidence_hashes.get("release-gate.json") != _sha256_bytes(gate_raw):
        raise HandoffError("release gate bytes differ from the captured evidence inventory")

    source_receipt_path = evidence_files.get("01-metadata/source-hashes.json")
    if source_receipt_path is None:
        raise HandoffError("captured source-hash receipt is missing")
    captured_source_hashes, _ = _load_json(source_receipt_path, "source-hash receipt")
    if captured_source_hashes != source_hashes:
        raise HandoffError("captured source hash receipt differs from the execution manifest")
    compile_db = evidence_files.get("02-compile-db/compile_commands.json")
    selected_db = evidence_files.get("02-compile-db/compile_commands.selected.json")
    if compile_db is None or selected_db is None:
        raise HandoffError("captured original and selected compilation databases are required")
    if evidence_hashes["02-compile-db/compile_commands.json"] != group.get("database_sha256"):
        raise HandoffError("captured original compilation database hash differs from the execution manifest")
    if evidence_hashes["02-compile-db/compile_commands.selected.json"] != group.get("selected_database_sha256"):
        raise HandoffError("captured selected compilation database hash differs from the execution manifest")

    results = group.get("results")
    if not isinstance(results, dict) or set(results) != set(RUNNER_NAMES.values()):
        raise HandoffError("all three scanner execution receipts are required")
    for name, result in results.items():
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise HandoffError("scanner execution did not complete successfully: " + name)

    version_records: dict[str, dict[str, Any]] = {}
    version_raw: dict[str, tuple[bytes, bytes, str, str]] = {}
    version_tokens: dict[str, str] = {}
    versions = run_manifest.get("versions")
    if not isinstance(versions, dict):
        raise HandoffError("runner has no captured scanner version receipts")
    for name in ("cppcheck", "clang-tidy", "clang", "clang++"):
        record, stdout, stderr, text, token = _version_record(run_root, versions.get(name), name)
        version_records[name] = record
        version_raw[name] = (stdout, stderr, text, token)
        version_tokens[name] = token
    if version_tokens["clang"] != version_tokens["clang++"] or version_tokens["clang"] != version_tokens["clang-tidy"]:
        raise HandoffError("clang, clang++ and clang-tidy captured release tokens must agree")
    version_tokens["clang-static-analyzer"] = version_tokens["clang"]

    units_by_tool: dict[str, dict[str, dict[str, Any]]] = {}
    for tool, runner_name in RUNNER_NAMES.items():
        result = results[runner_name]
        if tool == "cppcheck":
            if (result.get("mode") != "compile" or not _is_count(result.get("selected_units"))
                    or result["selected_units"] != len(selected)):
                raise HandoffError("Cppcheck did not complete the selected compile-database scope")
            units_by_tool[tool] = {unit: {} for unit in selected}
            continue
        files = result.get("files")
        if not isinstance(files, list) or len(files) != len(selected):
            raise HandoffError("scanner per-TU receipt count differs from selected units: " + tool)
        by_file: dict[str, dict[str, Any]] = {}
        for item in files:
            if (not isinstance(item, dict) or item.get("file") not in selected or item.get("file") in by_file
                    or item.get("ok") is not True or type(item.get("exit_code")) is not int
                    or item["exit_code"] != 0):
                raise HandoffError("scanner per-TU execution receipt is missing, duplicated or failed: " + tool)
            if tool == "clang-static-analyzer" and not _is_count(item.get("diagnostics")):
                raise HandoffError("CSA per-TU diagnostic count is invalid")
            by_file[item["file"]] = item
        if set(by_file) != set(selected):
            raise HandoffError("scanner per-TU inventory differs from selected translation units: " + tool)
        units_by_tool[tool] = by_file
    if (not _is_count(results["clang_static_analyzer"].get("total"))
            or results["clang_static_analyzer"]["total"] != len(selected)
            or not _is_count(results["clang_static_analyzer"].get("analyzed"))
            or results["clang_static_analyzer"]["analyzed"] != len(selected)
            or results["clang_static_analyzer"].get("failed") not in (None, [])):
        raise HandoffError("CSA must complete every selected translation unit, including units with findings")
    if (not _is_count(results["clang_tidy"].get("total"))
            or results["clang_tidy"]["total"] != len(selected)):
        raise HandoffError("clang-tidy selected-file count differs from the compile database")

    cpp_xml_rel = "03-cppcheck/result/cppcheck-compile-report.xml"
    cpp_xml_path = evidence_files.get(cpp_xml_rel)
    cpp_xml_inventory = {name for name in evidence_files if name.startswith("03-cppcheck/result/") and name.endswith(".xml")}
    if cpp_xml_path is None or cpp_xml_inventory != {cpp_xml_rel}:
        raise HandoffError("expected exactly one Cppcheck native XML report")
    cpp_xml_raw = _read_file(cpp_xml_path, "Cppcheck native XML", MAX_NATIVE_BYTES)
    try:
        xml_root = ET.fromstring(cpp_xml_raw)
    except ET.ParseError as error:
        raise HandoffError("Cppcheck native XML cannot be parsed") from error
    if xml_root.tag != "results" or xml_root.get("version") != "2" or xml_root.find("errors") is None:
        raise HandoffError("Cppcheck output must be valid native XML v2")
    cpp_versions = {node.get("version") for node in xml_root.findall(".//cppcheck") if node.get("version")}
    if cpp_versions != {version_tokens["cppcheck"]}:
        raise HandoffError("Cppcheck XML release token conflicts with captured version")
    cpp_diagnostics = len(xml_root.findall("./errors/error"))

    cpp_receipt_path = evidence_files.get("03-cppcheck/cppcheck-artifacts/receipt.json")
    cpp_summary_path = evidence_files.get("03-cppcheck/cppcheck-artifacts/summary.json")
    cpp_inputs_path = evidence_files.get("03-cppcheck/cppcheck-artifacts/input-files.csv")
    if cpp_receipt_path is None or cpp_summary_path is None or cpp_inputs_path is None:
        raise HandoffError("Cppcheck execution receipt, summary and selected-input CSV are required")
    cpp_receipt, _ = _load_json(cpp_receipt_path, "Cppcheck execution receipt")
    cpp_summary, _ = _load_json(cpp_summary_path, "Cppcheck summary")
    for key, expected in (("ok", True), ("exit_code", 0), ("mode", "compile"), ("selected_units", len(selected))):
        receipt_value = cpp_receipt.get(key)
        result_value = results["cpp_check"].get(key)
        if key == "ok":
            valid = receipt_value is True and result_value is True
        elif key == "exit_code":
            valid = (type(receipt_value) is int and receipt_value == expected
                     and type(result_value) is int and result_value == expected)
        elif key == "selected_units":
            valid = (_is_count(receipt_value) and receipt_value == expected
                     and _is_count(result_value) and result_value == expected)
        else:
            valid = receipt_value == expected and result_value == expected
        if not valid:
            raise HandoffError("Cppcheck receipt does not establish successful compile-database completion")
    if (not _is_count(cpp_receipt.get("diagnostics")) or cpp_receipt["diagnostics"] != cpp_diagnostics
            or not _is_count(cpp_summary.get("diagnostics")) or cpp_summary["diagnostics"] != cpp_diagnostics):
        raise HandoffError("Cppcheck native XML count differs from typed scanner receipts")
    input_raw = _read_file(cpp_inputs_path, "Cppcheck selected-input CSV", MAX_JSON_BYTES)
    try:
        with io.StringIO(input_raw.decode("utf-8"), newline="") as stream:
            rows = list(csv.DictReader(stream))
    except (UnicodeDecodeError, csv.Error) as error:
        raise HandoffError("Cppcheck selected-input CSV is invalid") from error
    if not rows or list(rows[0]) != ["file"] or len(rows) != len(selected) or {row.get("file") for row in rows} != set(selected):
        raise HandoffError("Cppcheck selected-input CSV does not exactly match selected units")

    mapping_path = evidence_files.get("05-clang-static-analyzer/result/tu-mapping.csv")
    if mapping_path is None:
        raise HandoffError("CSA TU-to-plist mapping receipt is missing")
    try:
        with io.StringIO(_read_file(mapping_path, "CSA TU-to-plist mapping receipt", MAX_JSON_BYTES).decode("utf-8"), newline="") as stream:
            mapping_reader = csv.DictReader(stream)
            if mapping_reader.fieldnames != ["slug", "file", "diagnostics", "exit_code"]:
                raise HandoffError("CSA TU-to-plist mapping columns are invalid")
            mapping_rows = list(mapping_reader)
    except (UnicodeDecodeError, csv.Error) as error:
        raise HandoffError("CSA TU-to-plist mapping CSV is invalid") from error
    if len(mapping_rows) != len(selected):
        raise HandoffError("CSA mapping receipt does not contain one row per selected TU")
    csa_folder = "05-clang-static-analyzer/result/csa-native/"
    csa_entries: dict[str, Path] = {}
    csa_versions: set[str] = set()
    csa_count = 0
    for row in mapping_rows:
        unit = row.get("file")
        slug = row.get("slug")
        if unit not in selected or slug != hashlib.sha256(unit.encode()).hexdigest()[:20] or row.get("exit_code") != "0":
            raise HandoffError("CSA mapping receipt identity or status is invalid")
        unit_receipt = units_by_tool["clang-static-analyzer"].get(unit)
        if (unit_receipt is None or not _is_count(unit_receipt.get("diagnostics"))
                or row.get("diagnostics") != str(unit_receipt["diagnostics"])):
            raise HandoffError("CSA mapping receipt findings differ from per-TU execution receipts")
        relative = csa_folder + slug + ".plist"
        path = evidence_files.get(relative)
        if path is None:
            raise HandoffError("CSA lacks its native plist for selected TU " + unit)
        try:
            plist = plistlib.loads(_read_file(path, "CSA native plist", MAX_NATIVE_BYTES))
        except (plistlib.InvalidFileException, ValueError, OverflowError) as error:
            raise HandoffError("CSA native plist cannot be parsed for " + unit) from error
        if not isinstance(plist, dict) or not isinstance(plist.get("diagnostics"), list):
            raise HandoffError("CSA native plist lacks a diagnostics array for " + unit)
        native_version = plist.get("clang_version")
        if not isinstance(native_version, str) or not native_version.strip():
            raise HandoffError("CSA native plist lacks its original clang_version string")
        native_match = VERSION_RE.search(native_version)
        if not native_match or native_match.group(1) != version_tokens["clang"]:
            raise HandoffError("CSA plist release token conflicts with captured clang version")
        if len(plist["diagnostics"]) != int(row["diagnostics"]):
            raise HandoffError("CSA native plist findings differ from per-TU execution receipts")
        csa_count += len(plist["diagnostics"])
        csa_versions.add(native_version)
        csa_entries[unit] = path
    expected_plists = {csa_folder + hashlib.sha256(unit.encode()).hexdigest()[:20] + ".plist" for unit in selected}
    listed_plists = {name for name in evidence_files if name.startswith(csa_folder) and name.endswith(".plist")}
    if listed_plists != expected_plists:
        raise HandoffError("CSA native plist inventory does not exactly match selected translation units")
    csa_status_path = evidence_files.get("05-clang-static-analyzer/result/compile-db/analyzer-status.json")
    if csa_status_path is None:
        raise HandoffError("CSA analyzer-status execution receipt is missing")
    csa_status, _ = _load_json(csa_status_path, "CSA analyzer-status receipt")
    expected_csa_count = sum(item.get("diagnostics", 0) for item in results["clang_static_analyzer"].get("files", []))
    if (not _is_count(csa_status.get("total")) or csa_status["total"] != len(selected)
            or not _is_count(csa_status.get("analyzed")) or csa_status["analyzed"] != len(selected)
            or csa_status.get("failed") not in (None, []) or not _is_count(csa_status.get("reports"))
            or csa_status["reports"] != expected_csa_count
            or csa_count != expected_csa_count):
        raise HandoffError("CSA analyzer-status receipt does not reconcile completed TUs and findings")
    csa_status_files = csa_status.get("files")
    if not isinstance(csa_status_files, list) or len(csa_status_files) != len(selected):
        raise HandoffError("CSA analyzer-status per-TU receipt count is inconsistent")
    csa_status_by_unit = {item.get("file"): item for item in csa_status_files if isinstance(item, dict)}
    if set(csa_status_by_unit) != set(selected):
        raise HandoffError("CSA analyzer-status TU identities differ from selected scope")
    for unit in selected:
        status_item = csa_status_by_unit[unit]
        execution_item = units_by_tool["clang-static-analyzer"][unit]
        if (status_item.get("unit_id") != execution_item.get("unit_id")
                or status_item.get("ok") is not True
                or type(status_item.get("exit_code")) is not int or status_item["exit_code"] != 0
                or not _is_count(status_item.get("diagnostics"))
                or status_item["diagnostics"] != execution_item.get("diagnostics")):
            raise HandoffError("CSA status per-TU receipt conflicts with execution manifest")

    tidy_summary_path = evidence_files.get("04-clang-tidy/result/clang-tidy-summary.json")
    if tidy_summary_path is None:
        raise HandoffError("clang-tidy execution summary is missing")
    tidy_summary, _ = _load_json(tidy_summary_path, "clang-tidy execution summary")
    summary_rows = tidy_summary.get("files")
    if (not _is_count(tidy_summary.get("selected_file_count"))
            or tidy_summary["selected_file_count"] != len(selected) or not isinstance(summary_rows, list)
            or len(summary_rows) != len(selected)):
        raise HandoffError("clang-tidy summary count differs from selected units")
    summary_by_unit: dict[str, dict[str, Any]] = {}
    for item in summary_rows:
        if not isinstance(item, dict):
            raise HandoffError("clang-tidy summary TU inventory differs from selected units")
        unit = item.get("file")
        if not isinstance(unit, str) or unit not in selected or unit in summary_by_unit:
            raise HandoffError("clang-tidy summary TU inventory differs from selected units")
        summary_by_unit[unit] = item
    if set(summary_by_unit) != set(selected):
        raise HandoffError("clang-tidy summary TU inventory differs from selected units")
    for unit, row in summary_by_unit.items():
        if row.get("status") != "ok" or type(row.get("unique_diagnostics")) is not int or row["unique_diagnostics"] < 0:
            raise HandoffError("clang-tidy summary contains an invalid per-TU status or count")
        receipt = units_by_tool["clang-tidy"][unit]
        if receipt.get("unit_id") != hashlib.sha256(unit.encode()).hexdigest()[:20]:
            raise HandoffError("clang-tidy unit ID does not bind to its selected-file identity")
    tidy_warnings = tidy_summary.get("warnings")
    if not isinstance(tidy_warnings, dict):
        raise HandoffError("clang-tidy warning summary is missing")
    raw_total = tidy_warnings.get("raw_total")
    unique_total = tidy_warnings.get("unique_total")
    by_check = tidy_warnings.get("by_check")
    if (not _is_count(raw_total) or not _is_count(unique_total) or unique_total > raw_total
            or not isinstance(by_check, list)):
        raise HandoffError("clang-tidy warning summary has invalid aggregate counts")
    checks: set[str] = set()
    by_check_raw = 0
    by_check_unique = 0
    for item in by_check:
        if (not isinstance(item, dict) or not isinstance(item.get("check"), str)
                or not item["check"].strip() or item["check"] in checks
                or not _is_count(item.get("raw")) or not _is_count(item.get("unique"))
                or item["unique"] > item["raw"]):
            raise HandoffError("clang-tidy by-check summary contains an invalid row")
        checks.add(item["check"])
        by_check_raw += item["raw"]
        by_check_unique += item["unique"]
    if by_check_raw != raw_total or by_check_unique != unique_total:
        raise HandoffError("clang-tidy warning totals do not reconcile with by-check counts")
    tidy_prefix = "04-clang-tidy/result/fixes/"
    tidy_files = {path for path in evidence_files if path.startswith(tidy_prefix) and path.endswith(".yaml")}
    expected_tidy: dict[str, str] = {}
    for unit in selected:
        relative = tidy_prefix + units_by_tool["clang-tidy"][unit]["unit_id"] + ".yaml"
        if relative in evidence_files:
            expected_tidy[unit] = relative
        elif summary_by_unit[unit]["unique_diagnostics"] != 0:
            raise HandoffError("clang-tidy reports findings but lacks native YAML for " + unit)
    if tidy_files != set(expected_tidy.values()):
        raise HandoffError("clang-tidy evidence contains an unexpected or unselected YAML artifact")

    return {
        "run_id": run_id, "run_manifest": run_manifest, "run_manifest_raw": run_manifest_raw,
        "group": group, "selected": selected, "relative_units": relative_units,
        "source_hashes": source_hashes, "commit": commit, "repository_root": repo_root,
        "evidence_root": evidence_root, "evidence_manifest_raw": evidence_manifest_raw,
        "evidence_files": evidence_files, "evidence_hashes": evidence_hashes,
        "evidence_sizes": evidence_sizes, "gate": gate, "gate_raw": gate_raw,
        "compile_db": compile_db, "selected_db": selected_db, "results": results,
        "versions": version_records, "version_raw": version_raw, "version_tokens": version_tokens,
        "cpp_xml": cpp_xml_path, "cpp_diagnostics": cpp_diagnostics,
        "csa_entries": csa_entries, "csa_native_versions": sorted(csa_versions),
        "csa_diagnostics": csa_count, "tidy_summary": tidy_summary,
        "tidy_summary_path": tidy_summary_path, "tidy_by_unit": summary_by_unit,
        "tidy_yaml": expected_tidy,
    }


def export_run(profile_path: str | Path, run_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    run_root = _safe_existing_directory(run_path, "runner run")
    output = _safe_new_output(output_path)
    try:
        output.relative_to(run_root)
        raise HandoffError("output must not be inside the runner run directory")
    except ValueError:
        pass
    profile_requested = Path(profile_path).expanduser()
    _reject_symlink_components(profile_requested, "product profile")
    profile_file = Path(os.path.abspath(profile_requested))
    profile, profile_raw = _load_json(profile_file, "product profile", MAX_PROFILE_BYTES)
    _validate_profile(profile)
    validated = _validate_run(run_root, profile)

    stage = Path(tempfile.mkdtemp(prefix="." + output.name + ".delivery-", dir=output.parent))
    payload_files: dict[str, dict[str, Any]] = {}
    provenance: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    profile_path = "provenance/product-profile.json"
    profile_file_record = _copy_bytes(stage, profile_path, profile_raw)
    payload_files[profile_path] = profile_file_record
    provenance.append(_provenance(profile_path, profile_file_record["sha256"], "product-scope-profile"))

    def copy_evidence(source_relative: str, destination: str, purpose: str,
                      **identity: Any) -> dict[str, Any]:
        prior = payload_files.get(destination)
        if prior is not None:
            return prior
        source = validated["evidence_files"].get(source_relative)
        if source is None:
            raise HandoffError("required evidence file is missing: " + source_relative)
        copied = _copy_source(stage, source, destination, validated["evidence_hashes"][source_relative])
        payload_files[destination] = copied
        provenance.append(_provenance(destination, copied["sha256"], purpose, source_path=source_relative, **identity))
        return copied

    try:
        profile_value = profile
        product_id = profile_value["product_id"]
        component = profile_value["component_id"]
        target_id = profile_value["target_id"]
        configuration = profile_value["configuration"]
        units = validated["selected"]
        versions = validated["version_tokens"]

        # Preserve native scanner bytes verbatim and identify each output by
        # its declared scanner, product component, target and source unit.
        cpp_record = copy_evidence("03-cppcheck/result/cppcheck-compile-report.xml", "native/cppcheck.xml", "native-scanner-output")
        artifacts.append({
            "path": cpp_record["path"], "sha256": cpp_record["sha256"], "tool": "cppcheck",
            "format": FORMATS["cppcheck"], "tool_version": versions["cppcheck"],
            "component": component, "configuration": configuration, "target": target_id,
        })
        for unit, source_relative in validated["tidy_yaml"].items():
            record = copy_evidence(source_relative, "native/clang-tidy/" + hashlib.sha256(unit.encode()).hexdigest()[:20] + ".yaml",
                                   "native-scanner-output", tool="clang-tidy", analysis_unit=unit)
            artifacts.append({
                "path": record["path"], "sha256": record["sha256"], "tool": "clang-tidy",
                "format": FORMATS["clang-tidy"], "tool_version": versions["clang-tidy"],
                "component": component, "configuration": configuration, "target": target_id,
                "analysis_unit": unit,
            })
        for unit in units:
            source_relative = "05-clang-static-analyzer/result/csa-native/" + hashlib.sha256(unit.encode()).hexdigest()[:20] + ".plist"
            record = copy_evidence(source_relative, "native/clang-static-analyzer/" + hashlib.sha256(unit.encode()).hexdigest()[:20] + ".plist",
                                   "native-scanner-output", tool="clang-static-analyzer", analysis_unit=unit)
            artifacts.append({
                "path": record["path"], "sha256": record["sha256"], "tool": "clang-static-analyzer",
                "format": FORMATS["clang-static-analyzer"], "tool_version": versions["clang-static-analyzer"],
                "component": component, "configuration": configuration, "target": target_id,
                "analysis_unit": unit,
            })

        # Preserve execution receipts, source/build closure, all captured tool
        # banners and hashes as separate, immutable provenance files.
        root_manifest_path = "provenance/runner/manifest.json"
        root_record = _copy_bytes(stage, root_manifest_path, validated["run_manifest_raw"])
        payload_files[root_manifest_path] = root_record
        provenance.append(_provenance(root_manifest_path, root_record["sha256"], "runner-execution-receipt"))
        evidence_manifest_path = "provenance/runner/evidence-manifest.json"
        evidence_record = _copy_bytes(stage, evidence_manifest_path, validated["evidence_manifest_raw"])
        payload_files[evidence_manifest_path] = evidence_record
        provenance.append(_provenance(evidence_manifest_path, evidence_record["sha256"], "scanner-evidence-inventory"))
        gate_record = copy_evidence("release-gate.json", "provenance/runner/release-gate.json", "scanner-evidence-gate")
        full_db = copy_evidence("02-compile-db/compile_commands.json", "provenance/build/compile_commands.json", "build-configuration")
        selected_db = copy_evidence("02-compile-db/compile_commands.selected.json", "provenance/build/compile_commands.selected.json", "selected-build-configuration")
        source_hash_record = copy_evidence("01-metadata/source-hashes.json", "provenance/source-hashes.json", "source-hash-inventory")
        # These typed receipts are copied without interpretation or rewriting.
        for source_relative in sorted(validated["evidence_files"]):
            suffix = PurePosixPath(source_relative).suffix.lower()
            if suffix not in {".json", ".csv", ".txt"}:
                continue
            if not source_relative.startswith(("01-metadata/", "03-cppcheck/", "04-clang-tidy/", "05-clang-static-analyzer/")):
                continue
            if source_relative in {
                "01-metadata/source-hashes.json", "02-compile-db/compile_commands.json",
                "02-compile-db/compile_commands.selected.json", "03-cppcheck/result/cppcheck-compile-report.xml",
            }:
                continue
            copy_evidence(source_relative, "provenance/runner-evidence/" + source_relative, "scanner-typed-receipt")

        binary_identities: dict[str, Any] = {}
        for name, record in validated["versions"].items():
            stdout, stderr, _text, token = validated["version_raw"][name]
            for suffix, raw in (("stdout", stdout), ("stderr", stderr)):
                relative = "provenance/tool-versions/" + name.replace("+", "plus") + "." + suffix + ".txt"
                copied = _copy_bytes(stage, relative, raw)
                payload_files[relative] = copied
                provenance.append(_provenance(relative, copied["sha256"], "tool-version-banner", tool=name))
            binary_identities[name] = {
                "tool_version": token,
                "binary_sha256": record["binary_sha256"],
                "command_basename": Path(record["command"][0]).name,
                "banner_sha256": _sha256_bytes(stdout + b"\n" + stderr),
            }
        binary_record = _write_json(stage, "provenance/tool-versions/binary-identities.json", binary_identities)
        payload_files[binary_record["path"]] = binary_record
        provenance.append(_provenance(binary_record["path"], binary_record["sha256"], "tool-binary-identities"))
        native_version_record = _write_json(
            stage, "provenance/tool-versions/csa-native-version-strings.json",
            {"clang_version": validated["csa_native_versions"]},
        )
        payload_files[native_version_record["path"]] = native_version_record
        provenance.append(_provenance(native_version_record["path"], native_version_record["sha256"], "native-tool-version-metadata"))

        zero_yaml_units = [unit for unit in units if unit not in validated["tidy_yaml"]]
        if zero_yaml_units:
            summary_record = next(
                item for item in provenance
                if item.get("source_path") == "04-clang-tidy/result/clang-tidy-summary.json"
            )
            provenance.append({
                **summary_record,
                "evidence_type": "clang-tidy-runner-zero-result-receipt",
                "tool": "clang-tidy",
                "tool_version": versions["clang-tidy"],
                "component": component,
                "configuration": configuration,
                "target": target_id,
                "analysis_units": zero_yaml_units,
                "basis": "runner per-file receipt reports status ok and unique_diagnostics zero; native YAML is absent",
            })

        policy = validated["run_manifest"].get("policy", {}).get("tools", {})
        policy_names = {"cppcheck": "cpp_check", "clang-tidy": "clang_tidy", "clang-static-analyzer": "clang_static_analyzer"}
        rule_hashes: dict[str, str] = {}
        for tool in TOOLS:
            runner_policy = policy.get(policy_names[tool]) if isinstance(policy, dict) else None
            if not isinstance(runner_policy, dict):
                raise HandoffError("captured scanner policy is missing: " + tool)
            binary_ids = (
                {name: validated["versions"][name]["binary_sha256"] for name in ("clang", "clang++")}
                if tool == "clang-static-analyzer"
                else {"clang-tidy" if tool == "clang-tidy" else "cppcheck": validated["versions"]["clang-tidy" if tool == "clang-tidy" else "cppcheck"]["binary_sha256"]}
            )
            rules = {
                "schema_version": "three-tool-runner-rules-config-1.0",
                "tool": tool,
                "tool_version": versions[tool],
                "binary_sha256": binary_ids,
                "runner_policy": runner_policy,
            }
            rules_path = "provenance/rules/" + tool + ".json"
            rules_record = _write_json(stage, rules_path, rules)
            payload_files[rules_path] = rules_record
            provenance.append(_provenance(rules_path, rules_record["sha256"], "rules-configuration",
                                          tool=tool, tool_version=versions[tool], component=component,
                                          configuration=configuration))
            rule_hashes[tool] = rules_record["sha256"]

        run_results = validated["results"]
        tidy_by_unit = {item["file"]: item for item in run_results["clang_tidy"]["files"]}
        csa_by_unit = {item["file"]: item for item in run_results["clang_static_analyzer"]["files"]}
        tool_runs: list[dict[str, Any]] = []
        descriptions = {
            "cppcheck": "cppcheck compile-database analysis; exact arguments and runner policy are preserved in the execution receipt",
            "clang-tidy": "clang-tidy per-translation-unit analysis; exact commands and runner policy are preserved in execution receipts",
            "clang-static-analyzer": "Clang Static Analyzer per-translation-unit analysis; exact commands and runner policy are preserved in execution receipts",
        }
        for tool in TOOLS:
            if tool == "cppcheck":
                receipts = [run_results["cpp_check"]]
            elif tool == "clang-tidy":
                receipts = [tidy_by_unit[unit] for unit in units]
            else:
                receipts = [csa_by_unit[unit] for unit in units]
            started = min((item.get("started") for item in receipts if item.get("started")), default=None)
            completed = max((item.get("finished") for item in receipts if item.get("finished")), default=None)
            if not isinstance(started, str) or not isinstance(completed, str):
                raise HandoffError("scanner execution timestamps are incomplete: " + tool)
            target = {
                "target": target_id,
                "attempted": True,
                "analyzer_completed": True,
                "exit_status": 0,
                "outcome": "success",
                "analysis_units": units,
                "units_attempted": len(units),
                "units_completed": len(units),
                "started_at": started,
                "completed_at": completed,
            }
            tool_runs.append({
                "tool": tool,
                "tool_version": versions[tool],
                "component": component,
                "configuration": configuration,
                "command": descriptions[tool],
                "rules_config_sha256": rule_hashes[tool],
                "started_at": started,
                "completed_at": completed,
                "targets": [target],
            })

        source = {
            "repository": profile_value["repository"],
            "commit": validated["commit"],
            "build_configuration_sha256": full_db["sha256"],
            "scope": {
                "included_components": [component],
                "included_paths": list(profile_value["included_paths"]),
                "excluded_paths": list(profile_value["excluded_paths"]),
                "languages": list(profile_value["languages"]),
                "unsupported_languages": list(profile_value["unsupported_languages"]),
            },
        }
        component_record = {
            "component_id": component,
            "target_id": target_id,
            "configuration": configuration,
            "analysis_units": units,
            "relative_source_paths": validated["relative_units"],
        }
        counts = {
            "selected_analysis_units": len(units),
            "artifact_count_by_tool": {
                tool: sum(item["tool"] == tool for item in artifacts) for tool in TOOLS
            },
            "tidy_units_without_native_yaml": len(units) - len(validated["tidy_yaml"]),
            "scanner_reported_diagnostics": {
                "cppcheck_native_xml": validated["cpp_diagnostics"],
                "clang_tidy_log_summary_unique": validated["tidy_summary"].get("warnings", {}).get("unique_total"),
                "clang_static_analyzer_native_plists": validated["csa_diagnostics"],
            },
        }
        manifest = {
            "schema_version": DELIVERY_SCHEMA,
            "run_id": validated["run_id"],
            "project": {"product_id": product_id, "display_name": profile_value["display_name"]},
            "source": source,
            "components": [component_record],
            "tool_runs": tool_runs,
            "artifacts": artifacts,
            "provenance_inputs": provenance,
            "counts": counts,
            "delivery_status": "complete-scan-evidence; no release approval implied",
        }
        manifest_record = _write_json(stage, "manifest.json", manifest)
        payload_files[manifest_record["path"]] = manifest_record
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "run_id": validated["run_id"],
            "product_id": product_id,
            "manifest": manifest_record,
            "files": [payload_files[path] for path in sorted(payload_files)],
        }
        receipt_record = _write_json(stage, "delivery-receipt.json", receipt)
        _rename_noreplace(stage, output)
        return {
            "status": "delivered",
            "schema_version": RECEIPT_SCHEMA,
            "run_id": validated["run_id"],
            "product_id": product_id,
            "output": str(output),
            "manifest_sha256": manifest_record["sha256"],
            "file_count": len(payload_files),
            "receipt_sha256": receipt_record["sha256"],
            "counts": counts,
        }
    except BaseException:
        if stage.exists() and stage.is_dir() and not stage.is_symlink():
            shutil.rmtree(stage)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export completed scanner evidence as a portable product delivery")
    subparsers = parser.add_subparsers(dest="action", required=True)
    export = subparsers.add_parser("export", help="export one evidence-gated product group")
    export.add_argument("--profile", required=True, type=Path, help="operator-approved product scope profile")
    export.add_argument("--run", required=True, type=Path, help="top-level runner result directory")
    export.add_argument("--output", required=True, type=Path, help="new delivery directory")
    args = parser.parse_args(argv)
    try:
        receipt = export_run(args.profile, args.run, args.output)
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        return 0
    except (HandoffError, OSError, ValueError, KeyError, TypeError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
