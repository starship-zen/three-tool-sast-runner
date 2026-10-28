#!/usr/bin/env python3
"""Run one product-scoped scan and publish its checked delivery ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import common
import handoff
import registry


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("cppcheck", "clang-tidy", "clang", "clang++")
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 1_000
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 250
MAX_ARCHIVE_DEPTH = 12
MAX_PATH_BYTES = 240
ALLOWED_SUFFIXES = {
    ".json", ".xml", ".yaml", ".yml", ".plist", ".sarif", ".html",
    ".txt", ".md", ".cfg", ".conf", ".ini", ".toml", ".log", ".csv",
    ".properties", ".config", ".cmake", ".ninja", ".csproj", ".sln",
    ".slnx", ".props", ".targets", ".editorconfig",
}


class DeliveryError(ValueError):
    def __init__(self, message: str, *, stage: str = "preflight", exit_code: int = 2,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.stage = stage
        self.exit_code = exit_code
        self.details = details or {}


def _safe_new_path(value: str | Path, label: str) -> Path:
    requested = Path(value).expanduser()
    if not requested.name or requested.name in {".", ".."}:
        raise DeliveryError(label + " must name a new path")
    parent = Path(os.path.abspath(requested.parent))
    try:
        handoff._reject_symlink_components(parent, label + " parent")
        parent = parent.resolve(strict=True)
    except (OSError, RuntimeError, handoff.HandoffError) as error:
        raise DeliveryError(label + " parent is unavailable or contains a symlink") from error
    if not parent.is_dir():
        raise DeliveryError(label + " parent must be an existing directory")
    path = parent / requested.name
    if path.exists() or path.is_symlink():
        raise DeliveryError(label + " already exists; use a fresh path")
    return path


def _source_paths(values: list[str]) -> list[Path]:
    if not values:
        raise DeliveryError("at least one explicit --scan-path is required")
    paths = []
    for value in values:
        requested = Path(value).expanduser()
        try:
            path = requested.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise DeliveryError("scan path is unavailable") from error
        if not (path.is_file() or path.is_dir()):
            raise DeliveryError("scan path must be a regular file or directory")
        if path.is_file() and path.suffix.lower() not in common.SOURCE_SUFFIXES | common.HEADER_SUFFIXES:
            raise DeliveryError("scan path file must be C/C++ source or header")
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise DeliveryError("duplicate --scan-path values are not allowed")
    return paths


def _git_value(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            env=common.environment(), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeliveryError("source repository preflight failed") from error
    if result.returncode != 0:
        raise DeliveryError("source location is not inside a readable Git repository")
    return result.stdout.strip()


def _is_inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _preflight(profile_path: Path, scan_paths: list[Path], result_root: Path,
               zip_path: Path) -> tuple[dict[str, Any], bytes, list[str], dict[str, str]]:
    try:
        handoff._reject_symlink_components(profile_path, "product profile")
        profile, profile_raw = handoff._load_json(profile_path, "product profile", handoff.MAX_PROFILE_BYTES)
        handoff._validate_profile(profile)
    except (OSError, handoff.HandoffError) as error:
        raise DeliveryError(str(error)) from error

    try:
        groups = common.plan(scan_paths, source_fallback=False)
    except (OSError, ValueError, TypeError) as error:
        raise DeliveryError("compile-database preflight failed: " + str(error)) from error
    if len(groups) != 1 or groups[0][0] is None:
        raise DeliveryError("a delivery requires exactly one compile-database group")
    selected = [entry["file"] for entry in groups[0][2]]
    if not selected:
        raise DeliveryError("compile-database preflight selected no translation units")
    if len(selected) > handoff.MAX_UNITS:
        raise DeliveryError("selected translation units exceed the delivery limit")

    source_root = common.source_root(groups[0][1], groups[0][0])
    repo_root_text = _git_value(source_root, "rev-parse", "--show-toplevel")
    repo_root = Path(repo_root_text).resolve()
    for unit in selected:
        path = Path(unit)
        if not _is_inside(path, repo_root):
            raise DeliveryError("selected translation unit is outside the source Git repository")
        current = path.parent
        while current != repo_root:
            if (current / ".git").exists():
                raise DeliveryError("selected translation units span nested Git repositories")
            current = current.parent
    try:
        handoff.validate_profile_scope(profile, repo_root, selected)
    except handoff.HandoffError as error:
        raise DeliveryError(str(error)) from error

    if _git_value(repo_root, "rev-parse", "HEAD") == "":
        raise DeliveryError("source repository has no recorded commit")
    if _git_value(repo_root, "status", "--porcelain"):
        raise DeliveryError("source repository must have a clean Git working tree before scanning")
    if _is_inside(result_root, repo_root) or _is_inside(zip_path, repo_root):
        raise DeliveryError("result and ZIP outputs must be outside the source Git checkout")
    if _is_inside(zip_path, result_root):
        raise DeliveryError("delivery ZIP must be outside the fresh run directory")

    tools: dict[str, str] = {}
    problems = []
    for name in TOOLS:
        try:
            binary = registry.resolve_tool(name)
            version = subprocess.run(
                [binary, "--version"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", timeout=30,
                env=common.environment(), check=False,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            problems.append(name + " is unavailable")
            continue
        version_text = version.stdout + "\n" + version.stderr
        try:
            handoff.stable_version_token(name, version_text)
            version_ok = version.returncode == 0
        except handoff.HandoffError:
            version_ok = False
        if not version_ok:
            problems.append(name + " has an unsupported or unverified version")
        else:
            tools[name] = version_text.strip().splitlines()[0]
    if problems:
        raise DeliveryError("tool preflight failed: " + "; ".join(problems))
    return profile, profile_raw, selected, tools


def _run_scan(scan_paths: list[Path], result_root: Path) -> int:
    command = ["bash", str(ROOT / "scan/scan_all.sh")]
    for path in scan_paths:
        command.extend(("--scan-path", str(path)))
    command.extend(("--result-path", str(result_root)))
    try:
        process = subprocess.Popen(
            command, cwd=ROOT, env=common.environment(), stdin=subprocess.DEVNULL,
            stdout=sys.stderr, stderr=sys.stderr, start_new_session=True,
        )
    except OSError as error:
        raise DeliveryError("could not start the scan runner", stage="scan", exit_code=1) from error
    try:
        with common.cancellation_signals():
            return process.wait()
    except KeyboardInterrupt as error:
        code = common.cancellation_exit_code(error)
        signum = signal.SIGTERM if code == 143 else signal.SIGINT
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
        process.wait()
        return code


def _run_directory(result_root: Path) -> tuple[Path | None, dict[str, Any] | None]:
    path = None
    for child in result_root.iterdir():
        if child.name == ".cache":
            if child.is_symlink() or not child.is_dir():
                return None, None
            continue
        if child.is_symlink() or not child.is_dir() or path is not None:
            return None, None
        path = child
    if path is None:
        return None, None
    manifest_path = path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return path, None
    try:
        manifest, _ = handoff._load_json(manifest_path, "runner execution manifest")
    except (OSError, handoff.HandoffError):
        return path, None
    try:
        recorded_path = Path(manifest.get("run_path", "")).resolve(strict=True)
        run_path = path.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return path, None
    if (manifest.get("runner_schema") != handoff.RUNNER_SCHEMA
            or manifest.get("run_id") != path.name or recorded_path != run_path):
        return path, None
    return path, manifest


def _archive_name(value: str) -> str:
    if (not value or "\x00" in value or "\\" in value
            or unicodedata.normalize("NFC", value) != value or value.startswith("/")):
        raise DeliveryError("delivery contains an unsafe ZIP member path", stage="zip", exit_code=4)
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise DeliveryError("delivery contains an unsafe ZIP member path", stage="zip", exit_code=4)
    if len(parts) > MAX_ARCHIVE_DEPTH or len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise DeliveryError("delivery ZIP member path exceeds receiver limits", stage="zip", exit_code=4)
    if PurePosixPath(value).suffix.casefold() not in ALLOWED_SUFFIXES:
        raise DeliveryError("delivery ZIP contains a file type outside receiver policy", stage="zip", exit_code=4)
    return value


def _directory_files(root: Path) -> set[str]:
    files: set[str] = set()
    casefolded: set[str] = set()
    for directory, directories, names in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        for name in directories:
            path = current / name
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise DeliveryError("exported delivery contains a symlink or special directory", stage="zip", exit_code=4)
        for name in names:
            path = current / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise DeliveryError("exported delivery contains a symlink or special file", stage="zip", exit_code=4)
            relative = _archive_name(path.relative_to(root).as_posix())
            folded = relative.casefold()
            if folded in casefolded:
                raise DeliveryError("exported delivery contains duplicate case-insensitive paths", stage="zip", exit_code=4)
            casefolded.add(folded)
            files.add(relative)
    return files


def _delivery_inventory(bundle_path: str | Path) -> dict[str, dict[str, Any]]:
    root = handoff._safe_existing_directory(bundle_path, "exported delivery")
    try:
        receipt, _ = handoff._load_json(root / "delivery-receipt.json", "delivery receipt", 16 * 1024 * 1024)
        manifest, _ = handoff._load_json(root / "manifest.json", "delivery manifest", 16 * 1024 * 1024)
    except (OSError, handoff.HandoffError) as error:
        raise DeliveryError("exported delivery is missing a readable root manifest or receipt", stage="zip", exit_code=4) from error
    if (receipt.get("schema_version") != handoff.RECEIPT_SCHEMA
            or manifest.get("schema_version") != handoff.DELIVERY_SCHEMA):
        raise DeliveryError("exported delivery schema is not the formal runner contract", stage="zip", exit_code=4)
    if (receipt.get("run_id") != manifest.get("run_id")
            or receipt.get("product_id") != manifest.get("project", {}).get("product_id")):
        raise DeliveryError("delivery receipt identity does not match its manifest", stage="zip", exit_code=4)

    records = receipt.get("files")
    if not isinstance(records, list) or not records or len(records) + 1 > MAX_ARCHIVE_MEMBERS:
        raise DeliveryError("delivery file count exceeds receiver limits", stage="zip", exit_code=4)
    inventory: dict[str, dict[str, Any]] = {}
    folded_names: set[str] = set()
    total = 0
    for record in records:
        if not isinstance(record, dict):
            raise DeliveryError("delivery receipt contains an invalid file record", stage="zip", exit_code=4)
        relative = _archive_name(record.get("path"))
        size = record.get("size")
        digest = record.get("sha256")
        folded = relative.casefold()
        if (relative == "delivery-receipt.json" or folded in folded_names
                or type(size) is not int or size < 0 or size > MAX_MEMBER_BYTES
                or not isinstance(digest, str) or not handoff.SHA_RE.fullmatch(digest)):
            raise DeliveryError("delivery receipt has an unsafe or invalid file record", stage="zip", exit_code=4)
        folded_names.add(folded)
        try:
            file_path = handoff._safe_evidence_file(root, relative, "delivery payload")
            actual_digest, actual_size = handoff._sha256_file(file_path, "delivery payload", MAX_MEMBER_BYTES)
        except handoff.HandoffError as error:
            raise DeliveryError(str(error), stage="zip", exit_code=4) from error
        if actual_digest != digest or actual_size != size:
            raise DeliveryError("delivery payload differs from its receipt: " + relative, stage="zip", exit_code=4)
        total += size
        inventory[relative] = {"sha256": digest, "size": size}
    if "manifest.json" not in inventory or receipt.get("manifest") != {
        "path": "manifest.json", **inventory.get("manifest.json", {})
    }:
        raise DeliveryError("delivery receipt does not bind the root manifest", stage="zip", exit_code=4)

    anchor_reference = manifest.get("diagnostic_anchors")
    if anchor_reference is not None:
        if (not isinstance(anchor_reference, dict) or set(anchor_reference) != {"path", "sha256"}
                or not isinstance(anchor_reference.get("sha256"), str)
                or not handoff.SHA_RE.fullmatch(anchor_reference["sha256"])):
            raise DeliveryError("diagnostic anchor reference is invalid", stage="zip", exit_code=4)
        try:
            anchor_path = _archive_name(anchor_reference.get("path"))
        except DeliveryError as error:
            raise DeliveryError("diagnostic anchor reference path is invalid", stage="zip", exit_code=4) from error
        if (anchor_path in {"manifest.json", "delivery-receipt.json"}
                or anchor_path not in inventory
                or inventory[anchor_path]["sha256"] != anchor_reference["sha256"]):
            raise DeliveryError("diagnostic anchor reference is not bound to a delivery member", stage="zip", exit_code=4)
        try:
            anchors, _ = handoff._load_json(root / anchor_path, "diagnostic anchor sidecar", handoff.MAX_NATIVE_BYTES)
        except handoff.HandoffError as error:
            raise DeliveryError("diagnostic anchor sidecar is invalid", stage="zip", exit_code=4) from error
        if (anchors.get("schema_version") != "diagnostic-anchors-1.0"
                or anchors.get("product_id") != manifest.get("project", {}).get("product_id")
                or anchors.get("source") != {
                    "repository": manifest.get("source", {}).get("repository"),
                    "commit": manifest.get("source", {}).get("commit"),
                }
                or not isinstance(anchors.get("entries"), list)):
            raise DeliveryError("diagnostic anchor sidecar identity differs from its delivery", stage="zip", exit_code=4)

    receipt_path = root / "delivery-receipt.json"
    try:
        receipt_hash, receipt_size = handoff._sha256_file(receipt_path, "delivery receipt", MAX_MEMBER_BYTES)
    except handoff.HandoffError as error:
        raise DeliveryError(str(error), stage="zip", exit_code=4) from error
    total += receipt_size
    if receipt_size > MAX_MEMBER_BYTES or total > MAX_EXPANDED_BYTES:
        raise DeliveryError("delivery exceeds receiver member or expanded-size limits", stage="zip", exit_code=4)
    inventory["delivery-receipt.json"] = {"sha256": receipt_hash, "size": receipt_size}

    if _directory_files(root) != set(inventory):
        raise DeliveryError("exported delivery contains files outside its receipt closure", stage="zip", exit_code=4)
    return inventory


def _verify_zip(path: Path, inventory: dict[str, dict[str, Any]]) -> None:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(infos) != len(inventory) or len(set(names)) != len(names) or set(names) != set(inventory):
                raise DeliveryError("ZIP members do not exactly match the receipt closure", stage="zip", exit_code=4)
            total = 0
            for info in infos:
                name = _archive_name(info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                record = inventory[name]
                if (info.is_dir() or info.flag_bits & 0x1
                        or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or file_type not in (0, stat.S_IFREG)
                        or info.file_size != record["size"] or info.file_size > MAX_MEMBER_BYTES
                        or info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO):
                    raise DeliveryError("ZIP member metadata violates receiver limits", stage="zip", exit_code=4)
                digest = hashlib.sha256()
                size = 0
                with archive.open(info, "r") as member:
                    while chunk := member.read(1024 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if size > info.file_size or size > MAX_MEMBER_BYTES or total > MAX_EXPANDED_BYTES:
                            raise DeliveryError("ZIP member expands beyond receiver limits", stage="zip", exit_code=4)
                        digest.update(chunk)
                if size != record["size"] or digest.hexdigest() != record["sha256"]:
                    raise DeliveryError("ZIP member hash or size differs from the receipt: " + name, stage="zip", exit_code=4)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, DeliveryError):
            raise
        raise DeliveryError("delivery ZIP could not be reopened and verified", stage="zip", exit_code=4) from error
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise DeliveryError("delivery ZIP exceeds the receiver upload limit of 64 MiB", stage="zip", exit_code=4)


def _publish_zip(bundle_path: str | Path, output: Path,
                 inventory: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fd, staged_name = tempfile.mkstemp(prefix="." + output.name + ".", suffix=".tmp", dir=output.parent)
    os.close(fd)
    staged = Path(staged_name)
    try:
        with zipfile.ZipFile(staged, "w", allowZip64=False) as archive:
            for relative in sorted(inventory):
                source = Path(bundle_path) / PurePosixPath(relative)
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.file_size = inventory[relative]["size"]
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                with source.open("rb") as input_stream, archive.open(info, "w") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
        os.chmod(staged, 0o600)
        _verify_zip(staged, inventory)
        handoff._rename_noreplace(staged, output)
        _verify_zip(output, inventory)
        zip_hash, zip_size = handoff._sha256_file(output, "delivery ZIP", MAX_ARCHIVE_BYTES)
        return {"path": str(output), "sha256": zip_hash, "size": zip_size,
                "member_count": len(inventory), "manifest_at_root": True}
    except DeliveryError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
        raise DeliveryError("could not create or publish a verified delivery ZIP", stage="zip", exit_code=4) from error
    finally:
        if staged.exists():
            staged.unlink()


def package_delivery(bundle_path: str | Path, zip_path: str | Path) -> dict[str, Any]:
    """Write a root-level formal delivery ZIP and verify every receipt-bound byte."""
    output = _safe_new_path(zip_path, "delivery ZIP")
    if output.suffix.casefold() != ".zip":
        raise DeliveryError("delivery output must use a .zip suffix")
    inventory = _delivery_inventory(bundle_path)
    return _publish_zip(bundle_path, output, inventory)


def _scan_failure(code: int, run_path: Path | None, manifest: dict[str, Any] | None,
                  product_id: str) -> DeliveryError:
    if code in (130, 143):
        status, message = "cancelled", "scan was cancelled; any started run evidence was preserved"
    elif code == 3:
        status, message = "evidence_incomplete", "scan ran but the three-tool evidence gate did not pass"
    else:
        status, message = "execution_failed", "scan execution failed; any started run evidence was preserved"
    details: dict[str, Any] = {"status": status, "product_id": product_id, "scan_exit_code": code}
    if run_path is not None:
        details["run_path"] = str(run_path)
    if manifest is not None:
        details["run_id"] = manifest.get("run_id")
    return DeliveryError(message, stage="scan", exit_code=code or 1, details=details)


def scan_and_deliver(profile_path: str | Path, scan_values: list[str],
                     result_path: str | Path, zip_path: str | Path) -> dict[str, Any]:
    requested_profile = Path(profile_path).expanduser()
    try:
        handoff._reject_symlink_components(requested_profile, "product profile")
        profile_file = Path(os.path.abspath(requested_profile))
    except (OSError, handoff.HandoffError) as error:
        raise DeliveryError("product profile path is unsafe or unavailable") from error
    scan_paths = _source_paths(scan_values)
    result_root = _safe_new_path(result_path, "run result root")
    output = _safe_new_path(zip_path, "delivery ZIP")
    if output.suffix.casefold() != ".zip":
        raise DeliveryError("delivery output must use a .zip suffix")
    if _is_inside(output, result_root):
        raise DeliveryError("delivery ZIP must be outside the fresh run directory")

    profile, profile_raw, selected, tool_versions = _preflight(
        profile_file, scan_paths, result_root, output)
    try:
        result_root.mkdir(mode=0o700)
    except OSError as error:
        raise DeliveryError("could not reserve the fresh run result root") from error

    scan_code = _run_scan(scan_paths, result_root)
    run_path, run_manifest = _run_directory(result_root)
    if scan_code != 0:
        raise _scan_failure(scan_code, run_path, run_manifest, profile["product_id"])
    if run_path is None or run_manifest is None:
        raise DeliveryError("runner returned success without one verifiable run directory",
                            stage="scan", exit_code=1,
                            details={"status": "execution_failed", "product_id": profile["product_id"]})

    bundle_path = output.parent / ("." + output.name + ".delivery-" + uuid.uuid4().hex)
    try:
        exported = handoff.export_run(profile_file, run_path, bundle_path)
        if exported.get("run_id") != run_manifest.get("run_id") or exported.get("product_id") != profile["product_id"]:
            raise DeliveryError("exported delivery identity changed after scan", stage="export", exit_code=4)
        manifest, _ = handoff._load_json(bundle_path / "manifest.json", "delivery manifest", 16 * 1024 * 1024)
        profile_records = [item for item in manifest.get("provenance_inputs", [])
                           if isinstance(item, dict) and item.get("purpose") == "product-scope-profile"]
        profile_hash = hashlib.sha256(profile_raw).hexdigest()
        if len(profile_records) != 1 or profile_records[0].get("sha256") != profile_hash:
            raise DeliveryError("product profile changed after preflight; no ZIP was published", stage="export", exit_code=4)
        inventory = _delivery_inventory(bundle_path)
        zip_record = _publish_zip(bundle_path, output, inventory)
    except DeliveryError as error:
        if error.stage == "preflight":
            error.stage = "export"
            error.exit_code = 4
        error.details.update({"product_id": profile["product_id"], "run_id": run_manifest["run_id"],
                              "run_path": str(run_path)})
        if bundle_path.exists() and bundle_path.is_dir() and not bundle_path.is_symlink():
            error.details["delivery_directory"] = str(bundle_path)
        raise
    except (OSError, ValueError, TypeError, KeyError, handoff.HandoffError) as error:
        details = {"product_id": profile["product_id"], "run_id": run_manifest["run_id"],
                   "run_path": str(run_path)}
        if bundle_path.exists() and bundle_path.is_dir() and not bundle_path.is_symlink():
            details["delivery_directory"] = str(bundle_path)
        raise DeliveryError("formal export failed: " + str(error), stage="export", exit_code=4,
                            details=details) from error

    try:
        shutil.rmtree(bundle_path)
    except OSError:
        pass
    return {
        "status": "delivered",
        "scan_status": "completed",
        "release_approved": False,
        "product_id": profile["product_id"],
        "profile_sha256": hashlib.sha256(profile_raw).hexdigest(),
        "run_id": run_manifest["run_id"],
        "run_path": str(run_path),
        "zip_path": zip_record["path"],
        "zip_sha256": zip_record["sha256"],
        "zip_bytes": zip_record["size"],
        "member_count": zip_record["member_count"],
        "manifest_sha256": exported["manifest_sha256"],
        "receipt_sha256": exported["receipt_sha256"],
        "selected_analysis_units": exported["counts"]["selected_analysis_units"],
        "scanner_reported_diagnostics": exported["counts"]["scanner_reported_diagnostics"],
        "tool_versions": tool_versions,
        "zip_root": ["manifest.json", "delivery-receipt.json"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and export one product-scoped C/C++ scan as a checked ZIP")
    parser.add_argument("--profile", required=True, type=Path, help="explicit operator-approved product profile JSON")
    parser.add_argument("--scan-path", required=True, action="append", nargs="+", metavar="PATH",
                        help="source file/directory to scan; repeat for additional scopes")
    parser.add_argument("--result-path", required=True, type=Path, help="fresh, nonexistent runner result root")
    parser.add_argument("--output", required=True, type=Path, help="new .zip path outside the source checkout")
    args = parser.parse_args(argv)
    scan_values = [value for group in args.scan_path for value in group]
    try:
        result = scan_and_deliver(args.profile, scan_values, args.result_path, args.output)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except DeliveryError as error:
        payload = {"status": error.details.pop("status", "not_delivered"), "stage": error.stage,
                   "error": str(error), **error.details}
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return error.exit_code
    except (OSError, ValueError, TypeError, KeyError, handoff.HandoffError) as error:
        print(json.dumps({"status": "not_delivered", "stage": "delivery", "error": str(error)},
                         sort_keys=True, separators=(",", ":")))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
