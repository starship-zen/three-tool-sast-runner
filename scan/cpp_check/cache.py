import fcntl
import hashlib
import json
import shutil
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
import common


def fingerprints(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlink in Cppcheck cache: " + str(path))
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = common.digest(path)
    return result


def inputs_unchanged(baseline):
    try:
        return (baseline["environment"] == common.environment()
                and all(common.digest(Path(name)) == digest
                        and str(Path(name).resolve(strict=True)) == baseline["resolved_paths"][name]
                        for name, digest in baseline["files"].items()))
    except (OSError, KeyError):
        return False


def runtime_files(binary):
    prefixes = {Path("/usr"), Path("/usr/local"), Path.home() / ".local",
                Path(binary).resolve().parent.parent}
    roots = {prefix / "share" / name for prefix in prefixes for name in ("cppcheck", "Cppcheck")}
    roots.add(Path(binary).resolve().parent / "cfg")
    return {str(root): {str(path.relative_to(root)): common.digest(path)
                        for path in sorted(root.rglob("*")) if path.is_file()}
            for root in sorted(roots) if root.is_dir()}


@contextmanager
def build_cache(output, entries, binary, policy, database):
    private = output / "cppcheck-build"
    private.mkdir()
    receipt = {"status": "disabled", "reason": "Verified dependency baseline unavailable", "published": False}
    receipt["scope"] = "Native build cache only; Cppcheck always executes, diagnostic results are not skipped"
    stack = ExitStack()
    try:
        try:
            stack.enter_context(shared_cache(output, entries, binary, policy, database, receipt))
        except (OSError, ValueError) as error:
            receipt.update(status="disabled", reason="Shared cache preparation failed: " + str(error), published=False)
            receipt.setdefault("warnings", []).append(receipt["reason"])
            shutil.rmtree(private)
            private.mkdir()
        try:
            yield receipt
        except BaseException:
            receipt["scan_ok"] = False
            raise
    finally:
        try:
            stack.close()
        except (OSError, ValueError) as error:
            receipt["publication_error"] = str(error)
            receipt.setdefault("warnings", []).append("Shared cache finalization failed: " + str(error))
        try:
            common.write_json(output / "cache-receipt.json", receipt)
        except OSError as error:
            receipt.setdefault("warnings", []).append("Cache receipt write failed: " + str(error))


@contextmanager
def shared_cache(output, entries, binary, policy, database, receipt):
    private = output / "cppcheck-build"
    baseline_path = output.parent / "input-baseline.json"
    try:
        baseline = json.loads(baseline_path.read_text()) if baseline_path.is_file() else {}
        if not isinstance(baseline, dict):
            baseline = {}
    except (OSError, ValueError) as error:
        baseline = {}
        receipt["reason"] = "Cannot read dependency baseline: " + str(error)
    if database is None or not baseline.get("ok") or not baseline.get("files") or not inputs_unchanged(baseline):
        try:
            yield receipt
        finally:
            common.write_json(output / "cache-receipt.json", receipt)
        return
    payload = {"schema": "cppcheck-cache-1", "binary": common.digest(Path(binary)), "policy": policy,
               "runtime_files": runtime_files(binary),
               "implementation": {str(path): common.digest(path) for path in
                                  [Path(__file__), Path(__file__).with_name("scanner.py"), Path(common.__file__)]},
               "entries": entries, "files": baseline["files"], "resolved_paths": baseline["resolved_paths"],
               "environment": baseline["environment"]}
    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    common.write_json(output / "cache-key-inputs.json", payload)
    root = output.parents[2] / ".cache" / "cppcheck"
    root.mkdir(parents=True, exist_ok=True)
    shared = root / key
    with (root / (key + ".lock")).open("a") as lock:
        deadline = time.monotonic() + 30
        while True:
            if common.CANCELLED.is_set():
                raise KeyboardInterrupt
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    receipt["reason"] = "Cache lock timeout; using private cache"
                    try:
                        yield receipt
                    finally:
                        common.write_json(output / "cache-receipt.json", receipt)
                    return
                time.sleep(0.1)
        content = shared / "content"
        receipt.update({"key": key, "path": str(shared), "status": "cold", "reason": None})
        try:
            if shared.exists():
                try:
                    expected = json.loads((shared / "hashes.json").read_text())
                    if not content.is_dir() or content.is_symlink() or fingerprints(content) != expected:
                        raise ValueError("Cppcheck cache integrity mismatch")
                    shutil.copytree(content, private, dirs_exist_ok=True)
                    receipt["status"] = "hit"
                except (OSError, ValueError) as error:
                    receipt.update(status="rebuilt", reason=str(error))
                    shutil.rmtree(private)
                    private.mkdir()
            yield receipt
            if (receipt.get("scan_ok") and inputs_unchanged(baseline)
                    and common.digest(Path(binary)) == payload["binary"]
                    and runtime_files(binary) == payload["runtime_files"]):
                temporary = root / (key + ".pending")
                if temporary.exists():
                    shutil.rmtree(temporary)
                shutil.copytree(private, temporary / "content")
                common.write_json(temporary / "hashes.json", fingerprints(temporary / "content"))
                if shared.exists():
                    shutil.rmtree(shared)
                temporary.rename(shared)
                receipt["published"] = True
            else:
                receipt["publication_skipped"] = "Scan failed or inputs/tool/runtime files changed"
        finally:
            common.write_json(output / "cache-receipt.json", receipt)
