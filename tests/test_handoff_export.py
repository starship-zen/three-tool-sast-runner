from __future__ import annotations

import csv
import copy
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import warnings
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import common
import deliver
import handoff


STAMP = "2026-10-22T10:00:00+00:00"
STAMP_END = "2026-10-22T10:01:00+00:00"
COMMIT = "a" * 40
COMPONENT = "fixture-component"
TARGET = "fixture-target"
CONFIGURATION = "fixture-config-v1"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, value: object) -> bytes:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _rebind_evidence_artifact(root: Path, relative_path: str) -> None:
    evidence_root = root / "group-1/evidence-bundle"
    path = evidence_root / relative_path
    manifest_path = evidence_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    item = next(item for item in manifest["artifacts"] if item["path"] == relative_path)
    raw = path.read_bytes()
    item["sha256"] = _sha(raw)
    item["size"] = len(raw)
    _write_json(manifest_path, manifest)


class HandoffExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runner-handoff-export-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.run_root = self.folder / "uninitialized-run"
        self.profile_path = self.folder / "profile.json"
        self.output = self.folder / "bundle"

    def make_profile(self, *, include=("src/**",)):
        profile = {
            "schema_version": handoff.PROFILE_SCHEMA,
            "product_id": "fixture-product",
            "display_name": "Synthetic exporter fixture",
            "repository": "https://example.invalid/org/repository",
            "component_id": COMPONENT,
            "target_id": TARGET,
            "configuration": CONFIGURATION,
            "included_paths": list(include),
            "excluded_paths": ["generated/**"],
            "languages": ["C++"],
            "unsupported_languages": [],
        }
        _write_json(self.profile_path, profile)
        return profile

    def make_run(self, *, unit_count=2, positive_tidy_units=(0,), csa_findings=(0,),
                 source_subdirectory="", profile_include=("src/**",),
                 repository_root: Path | None = None, diagnostic_header: str | None = None,
                 diagnostic_header_bytes: bytes | None = None):
        self.make_profile(include=profile_include)
        root = self.folder / ("20261022T100000Z-" + uuid.uuid4().hex[:12])
        self.run_root = root
        group_dir = root / "group-1"
        evidence_root = group_dir / "evidence-bundle"
        evidence_root.mkdir(parents=True)
        repo_root = str(repository_root.resolve()) if repository_root is not None else "/synthetic/source-root"
        source_root = repo_root + ("/" + source_subdirectory if source_subdirectory else "")
        if source_subdirectory:
            source_files = ["smoke.cpp", "tinyxml2/tinyxml2.cpp"]
            units = [source_root + "/" + source_files[index] for index in range(unit_count)]
        else:
            units = [source_root + "/src/unit-" + str(index) + ".cpp" for index in range(unit_count)]
        commit_value = COMMIT
        if repository_root is not None:
            repository_root.mkdir(parents=True, exist_ok=True)
            initialized = subprocess.run(
                ["git", "-C", str(repository_root), "init", "--quiet"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, check=False,
            )
            if initialized.returncode != 0:
                raise AssertionError(initialized.stderr.decode("utf-8", "replace"))
            for index, unit in enumerate(units):
                source = Path(unit)
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("int fixture_" + str(index) + "() { return " + str(index) + "; }\n", encoding="utf-8")
            if diagnostic_header is not None:
                header_path = Path(repo_root) / diagnostic_header
                header_path.parent.mkdir(parents=True, exist_ok=True)
                header_path.write_bytes(
                    diagnostic_header_bytes if diagnostic_header_bytes is not None
                    else b"// synthetic tracked diagnostic header\n"
                )
            added = subprocess.run(
                ["git", "-C", str(repository_root), "add", "--all"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, check=False,
            )
            if added.returncode != 0:
                raise AssertionError(added.stderr.decode("utf-8", "replace"))
            git_env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Synthetic Test",
                "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
                "GIT_COMMITTER_NAME": "Synthetic Test",
                "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
            }
            committed = subprocess.run(
                ["git", "-C", str(repository_root), "commit", "--quiet", "-m", "synthetic runner fixture"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, check=False, env=git_env,
            )
            if committed.returncode != 0:
                raise AssertionError(committed.stderr.decode("utf-8", "replace"))
            identified = subprocess.run(
                ["git", "-C", str(repository_root), "rev-parse", "--verify", "HEAD"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, check=False,
            )
            if identified.returncode != 0:
                raise AssertionError(identified.stderr.decode("utf-8", "replace"))
            commit_value = identified.stdout.decode("ascii").strip()
        slugs = {unit: hashlib.sha256(unit.encode()).hexdigest()[:20] for unit in units}
        source_hashes = {unit: hashlib.sha256(("source-" + str(index)).encode()).hexdigest()
                         for index, unit in enumerate(units)}

        version_specs = {
            "cppcheck": ("Cppcheck 2.22.0", "2.22.0"),
            "clang-tidy": ("Ubuntu LLVM version 18.1.3", "18.1.3"),
            "clang": ("Ubuntu clang version 18.1.3 (1ubuntu1)", "18.1.3"),
            "clang++": ("Ubuntu clang version 18.1.3 (1ubuntu1)", "18.1.3"),
        }
        versions = {}
        for name, (banner, _) in version_specs.items():
            stdout = root / (name.replace("+", "plus") + ".version.txt")
            stderr = root / (name.replace("+", "plus") + ".version.err.txt")
            stdout.parent.mkdir(parents=True, exist_ok=True)
            stdout.write_text(banner + "\n")
            stderr.write_text("")
            versions[name] = {
                "command": ["/tools/" + name, "--version"],
                "stdout": str(stdout), "stderr": str(stderr), "exit_code": 0,
                "timed_out": False, "cancelled": False, "binary_sha256": "b" * 64,
            }

        compile_db_raw = _write_json(root / "compile_commands.full.json", [
            {"directory": source_root, "file": unit, "arguments": ["c++", "-c", unit, "-o", "unit.o"]}
            for unit in units
        ])
        selected_db_raw = _write_json(evidence_root / "02-compile-db/compile_commands.selected.json", [
            {"directory": source_root, "file": unit, "arguments": ["c++", "-c", unit, "-o", "unit.o"]}
            for unit in units
        ])
        (evidence_root / "02-compile-db/compile_commands.json").write_bytes(compile_db_raw)
        _write_json(evidence_root / "01-metadata/source-hashes.json", source_hashes)
        _write_json(evidence_root / "01-metadata/scan-environment.json", {"synthetic": True})
        _write_json(evidence_root / "01-metadata/scanner-hashes.json", {"scan/runner.py": "c" * 64})

        cpp_error = ""
        if diagnostic_header is not None:
            native_header = str(Path(repo_root) / diagnostic_header)
            cpp_error = (
                '<error id="uninitvar" severity="warning" msg="synthetic header warning" cwe="457">'
                '<location file="' + native_header + '" line="1" column="1"/></error>'
            )
        cpp_xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<results version="2">'
                   '<cppcheck version="2.22.0"/><errors>' + cpp_error + '</errors></results>\n').encode()
        cpp_diagnostics = 1 if diagnostic_header is not None else 0
        (evidence_root / "03-cppcheck/result/cppcheck-compile-report.xml").parent.mkdir(parents=True, exist_ok=True)
        (evidence_root / "03-cppcheck/result/cppcheck-compile-report.xml").write_bytes(cpp_xml)
        _write_json(evidence_root / "03-cppcheck/cppcheck-artifacts/receipt.json",
                    {"ok": True, "exit_code": 0, "mode": "compile", "selected_units": unit_count,
                     "diagnostics": cpp_diagnostics})
        _write_json(evidence_root / "03-cppcheck/cppcheck-artifacts/summary.json", {"diagnostics": cpp_diagnostics})
        with (evidence_root / "03-cppcheck/cppcheck-artifacts/input-files.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("file",))
            writer.writerows((unit,) for unit in units)

        tidy_files = []
        tidy_warnings = {"raw_total": 0, "unique_total": 0, "by_check": []}
        tidy_summary_files = []
        for index, unit in enumerate(units):
            item = {"file": unit, "unit_id": slugs[unit], "ok": True, "exit_code": 0,
                    "started": STAMP, "finished": STAMP_END, "command": ["clang-tidy", unit]}
            tidy_files.append(item)
            if index in positive_tidy_units:
                yaml_raw = ("MainSourceFile: " + unit + "\n"
                            "Diagnostics:\n"
                            "  - DiagnosticName: bugprone-example-check\n"
                            "    Level: Warning\n"
                            "    DiagnosticMessage:\n"
                            "      Message: synthetic warning\n"
                            "      FilePath: " + unit + "\n"
                            "      FileOffset: 4\n"
                            "    Notes: []\n"
                            "    Replacements: []\n").encode()
                (evidence_root / "04-clang-tidy/result/fixes" / (slugs[unit] + ".yaml")).parent.mkdir(parents=True, exist_ok=True)
                (evidence_root / "04-clang-tidy/result/fixes" / (slugs[unit] + ".yaml")).write_bytes(yaml_raw)
                logs = evidence_root / "04-clang-tidy/result/logs"
                logs.mkdir(parents=True, exist_ok=True)
                (logs / (slugs[unit] + ".out.log")).write_bytes(b"")
                (logs / (slugs[unit] + ".err.log")).write_text(
                    unit + ":7:5: warning: synthetic warning [bugprone-example-check]\n", encoding="utf-8")
                (logs / (slugs[unit] + ".log")).write_text("derived combined log must not be exported as native", encoding="utf-8")
                tidy_warnings["raw_total"] += 1
                tidy_warnings["unique_total"] += 1
                tidy_warnings["by_check"].append({"check": "bugprone-example-check", "raw": 1, "unique": 1})
                count = 1
            else:
                count = 0
            tidy_summary_files.append({"file": unit, "status": "ok", "unique_diagnostics": count})
        _write_json(evidence_root / "04-clang-tidy/result/clang-tidy-summary.json", {
            "selected_file_count": unit_count, "files": tidy_summary_files, "warnings": tidy_warnings,
            "generated_at": STAMP_END,
        })

        csa_files = []
        mapping_path = evidence_root / "05-clang-static-analyzer/result/tu-mapping.csv"
        mapping_path.parent.mkdir(parents=True, exist_ok=True)
        with mapping_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("slug", "file", "diagnostics", "exit_code"))
            for index, unit in enumerate(units):
                diagnostic_count = 1 if index in csa_findings else 0
                plist_path = evidence_root / "05-clang-static-analyzer/result/csa-native" / (slugs[unit] + ".plist")
                plist_path.parent.mkdir(parents=True, exist_ok=True)
                native = {
                    "files": [unit],
                    "clang_version": "Ubuntu clang version 18.1.3 (1ubuntu1)",
                    "diagnostics": ([{"check_name": "core.NullDereference", "description": "synthetic CSA finding",
                                      "location": {"file": 0, "line": 1, "col": 1}}] if diagnostic_count else []),
                }
                plist_path.write_bytes(plistlib.dumps(native))
                writer.writerow((slugs[unit], unit, diagnostic_count, 0))
                csa_files.append({"file": unit, "unit_id": slugs[unit], "ok": True, "exit_code": 0,
                                  "started": STAMP, "finished": STAMP_END, "diagnostics": diagnostic_count,
                                  "artifact": str(plist_path)})
        _write_json(evidence_root / "05-clang-static-analyzer/result/compile-db/analyzer-status.json",
                    {"total": unit_count, "analyzed": unit_count, "failed": [],
                     "reports": sum(1 for index in range(unit_count) if index in csa_findings),
                     "files": csa_files})

        check_files = list(units)
        gate = {
            "ok": True, "errors": [], "release_ready": False,
            "checks": {
                "cpp_check": {"files": check_files, "errors": []},
                "clang_tidy": {"files": check_files, "errors": []},
                "clang_static_analyzer": {"files": check_files, "errors": []},
            },
            "pending_certification": ["downstream_schema_and_ingestion"],
        }
        _write_json(evidence_root / "release-gate.json", gate)

        database_hash = _sha(compile_db_raw)
        selected_database_hash = _sha(selected_db_raw)
        group = {
            "database": source_root + "/build/compile_commands.json", "database_sha256": database_hash,
            "mode": "compile", "selected_database": str(root / "group-1/compile_commands.json"),
            "selected_database_sha256": selected_database_hash, "evidence_bundle": "group-1/evidence-bundle",
            "scan_paths": [source_root], "selected_files": units,
            "baseline": {"root": source_root,
                         "git": {"repo": repo_root, "commit": commit_value, "status": "", "diff": "", "ok": True},
                         "sources": source_hashes, "captured_at": STAMP},
            "results": {
                "cpp_check": {"ok": True, "mode": "compile", "selected_units": unit_count, "exit_code": 0,
                              "diagnostics": cpp_diagnostics,
                              "started": STAMP, "finished": STAMP_END, "command": ["cppcheck", "--xml"]},
                "clang_tidy": {"ok": True, "total": unit_count, "files": tidy_files},
                "clang_static_analyzer": {"ok": True, "total": unit_count, "analyzed": unit_count,
                                           "failed": [], "status": "issues" if any(csa_findings) else "ok",
                                           "files": csa_files},
            },
            "release_gate": gate,
        }
        _write_json(root / "group-1/compile_commands.json", json.loads(selected_db_raw))
        common.write_json(evidence_root / "02-compile-db/compile_commands.json", json.loads(compile_db_raw))
        if repository_root is not None:
            input_files = list(units)
            if diagnostic_header is not None:
                input_files.append(str(Path(repo_root) / diagnostic_header))
            dependency_files = {}
            resolved_paths = {}
            snapshots = {}
            snapshot_root = evidence_root / "01-metadata/input-baseline/before"
            for name in input_files:
                source_path = Path(name)
                raw_source = source_path.read_bytes()
                digest = _sha(raw_source)
                dependency_files[str(source_path)] = digest
                resolved_paths[str(source_path)] = str(source_path.resolve())
                relative_snapshot = "content/" + digest
                snapshot_path = snapshot_root / relative_snapshot
                snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                snapshot_path.write_bytes(raw_source)
                snapshots[digest] = relative_snapshot
            before = {
                "ok": True, "errors": [], "files": dependency_files,
                "resolved_paths": resolved_paths, "environment": {}, "snapshots": snapshots,
            }
            after = {
                "ok": True, "errors": [], "files": dependency_files,
                "resolved_paths": resolved_paths, "environment": {}, "snapshots": {},
            }
            group["input_baseline"] = before
            group["input_baseline_after"] = after
            _write_json(snapshot_root / "baseline.json", before)
        evidence_artifacts = []
        for path in sorted(item for item in evidence_root.rglob("*") if item.is_file() and item.name != "manifest.json"):
            relative = path.relative_to(evidence_root).as_posix()
            evidence_artifacts.append({"path": relative, "sha256": _sha(path.read_bytes()),
                                       "size": path.stat().st_size, "layer": common.__dict__.get("_unused", "RECEIPT")})
        for item in evidence_artifacts:
            item["layer"] = __import__("evidence").artifact_layer(item["path"])
        evidence_manifest = {
            "schema": "scanner-evidence-1", "target_contract": "three-tool-input-1.0",
            "downstream_compatible": False, "release_ready": False, "commit": commit_value,
            "selected_files": units, "release_gate": gate, "artifacts": evidence_artifacts,
        }
        _write_json(evidence_root / "manifest.json", evidence_manifest)

        root_manifest = {
            "runner_schema": handoff.RUNNER_SCHEMA, "run_id": root.name, "started_at": STAMP,
            "completed_at": STAMP_END, "run_path": str(root), "ok": True, "exit_code": 0,
            "cancelled": False, "evidence_gate_required": True, "evidence_checks_ok": True,
            "downstream_compatible": False, "release_ready": False,
            "tools": ["cpp_check", "clang_tidy", "clang_static_analyzer"],
            "versions": versions,
            "policy": {"version": "tool-owned-scan-1", "tools": {
                "cpp_check": {"flags": ["--xml", "--xml-version=2", "--enable=all", "--inconclusive"]},
                "clang_tidy": {"checks": "-*,bugprone-*", "jobs": 4},
                "clang_static_analyzer": {"jobs": 4, "relax": []},
            }},
            "groups": [group],
        }
        _write_json(root / "manifest.json", root_manifest)
        # Rewrite the nested inventory after all evidence files (including the
        # two compile database copies) have been placed.
        evidence_artifacts = []
        for path in sorted(item for item in evidence_root.rglob("*") if item.is_file() and item.name != "manifest.json"):
            relative = path.relative_to(evidence_root).as_posix()
            evidence_artifacts.append({"path": relative, "sha256": _sha(path.read_bytes()),
                                       "size": path.stat().st_size, "layer": __import__("evidence").artifact_layer(relative)})
        evidence_manifest["artifacts"] = evidence_artifacts
        _write_json(evidence_root / "manifest.json", evidence_manifest)
        return root

    def test_mixed_zero_and_positive_units_preserve_bytes_and_positive_csa_is_complete(self):
        root = self.make_run(unit_count=2, positive_tidy_units=(0,), csa_findings=(0,))
        result = handoff.export_run(self.profile_path, root, self.output)
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], "three-tool-scan-delivery-1.0")
        self.assertNotIn("assets", manifest)
        self.assertNotIn("mapping_policy", manifest)
        self.assertNotIn("producer_contract", manifest)
        self.assertEqual(manifest["project"]["product_id"], "fixture-product")
        self.assertEqual(manifest["components"][0]["component_id"], COMPONENT)
        self.assertEqual(manifest["components"][0]["target_id"], TARGET)
        selected_units = manifest["components"][0]["analysis_units"]
        self.assertTrue(all(run["targets"][0]["analysis_units"] == selected_units for run in manifest["tool_runs"]))
        self.assertTrue(all(artifact.get("analysis_unit") in selected_units
                            for artifact in manifest["artifacts"] if artifact["tool"] != "cppcheck"))
        self.assertEqual(manifest["tool_runs"][0]["tool_version"], "2.22.0")
        self.assertEqual({run["tool_version"] for run in manifest["tool_runs"] if run["tool"] != "cppcheck"}, {"18.1.3"})
        tidy_receipt = next(item for item in manifest["provenance_inputs"]
                            if item.get("evidence_type") == "clang-tidy-runner-zero-result-receipt")
        summary = json.loads((self.output / tidy_receipt["path"]).read_text())
        self.assertEqual([item["unique_diagnostics"] for item in summary["files"]], [1, 0])
        self.assertEqual(tidy_receipt["analysis_units"], [manifest["components"][0]["analysis_units"][1]])
        csa = next(item for item in manifest["artifacts"] if item["tool"] == "clang-static-analyzer" and item["analysis_unit"].endswith("unit-0.cpp"))
        self.assertEqual(csa["sha256"], _sha((self.output / csa["path"]).read_bytes()))
        self.assertEqual(csa["tool_version"], "18.1.3")
        original = root / "group-1/evidence-bundle/05-clang-static-analyzer/result/csa-native" / (hashlib.sha256(csa["analysis_unit"].encode()).hexdigest()[:20] + ".plist")
        self.assertEqual((self.output / csa["path"]).read_bytes(), original.read_bytes())
        self.assertEqual((self.output / "provenance/runner/release-gate.json").is_file(), True)
        self.assertEqual(result["manifest_sha256"], _sha((self.output / "manifest.json").read_bytes()))
        self.assertEqual(manifest["counts"]["selected_analysis_units"], 2)
        self.assertEqual(manifest["counts"]["artifact_count_by_tool"], {
            "cppcheck": 1, "clang-tidy": 1, "clang-static-analyzer": 2,
        })
        tidy = next(item for item in manifest["artifacts"] if item["tool"] == "clang-tidy")
        self.assertEqual({item["stream"] for item in tidy["diagnostic_logs"]}, {"stdout", "stderr"})
        for log in tidy["diagnostic_logs"]:
            self.assertEqual(log["sha256"], _sha((self.output / log["path"]).read_bytes()))
            self.assertTrue(any(item["path"] == log["path"] and item["purpose"] == "native-diagnostic-log"
                                for item in manifest["provenance_inputs"]))
        self.assertFalse(any(item["path"].endswith("/" + hashlib.sha256(tidy["analysis_unit"].encode()).hexdigest()[:20] + ".log")
                             for item in manifest["provenance_inputs"]))
        delivery_receipt = json.loads((self.output / "delivery-receipt.json").read_text())
        self.assertEqual(delivery_receipt["manifest"]["sha256"], result["manifest_sha256"])
        for item in delivery_receipt["files"]:
            copied = self.output / item["path"]
            self.assertEqual(copied.stat().st_size, item["size"])
            self.assertEqual(_sha(copied.read_bytes()), item["sha256"])

    def test_diagnostic_anchor_sidecar_uses_pre_scan_snapshots_and_includes_headers(self):
        repository = self.folder / "anchor-repository"
        header_relative = "src/vendor/diagnostic.hpp"
        root = self.make_run(unit_count=1, positive_tidy_units=(0,), csa_findings=(0,),
                             repository_root=repository, diagnostic_header=header_relative)
        handoff.export_run(self.profile_path, root, self.output)

        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        reference = manifest["diagnostic_anchors"]
        self.assertEqual(set(reference), {"path", "sha256"})
        sidecar_path = self.output / reference["path"]
        sidecar_raw = sidecar_path.read_bytes()
        self.assertEqual(reference["sha256"], _sha(sidecar_raw))
        self.assertNotIn(b"synthetic tracked diagnostic header", sidecar_raw)
        sidecar = json.loads(sidecar_raw)
        self.assertEqual(sidecar["schema_version"], "diagnostic-anchors-1.0")
        self.assertEqual(sidecar["product_id"], manifest["project"]["product_id"])
        self.assertEqual(sidecar["source"], {"repository": manifest["source"]["repository"],
                                              "commit": manifest["source"]["commit"]})
        self.assertEqual(len(sidecar["entries"]), 3)
        self.assertTrue(all(item["anchor"]["status"] == "available" for item in sidecar["entries"]))

        by_locator = {item["locator"]: item for item in sidecar["entries"]}
        cpp = by_locator["errors.error[0]"]
        header_path = repository / header_relative
        self.assertEqual(cpp["primary_location"], {
            "file": str(header_path), "line": 1, "column": 1, "offset": None,
        })
        self.assertEqual(cpp["anchor"]["repository_path"], header_relative)
        header_bytes = header_path.read_bytes()
        self.assertEqual(cpp["anchor"]["file_sha256"], _sha(header_bytes))
        expected_context = _sha(len(header_bytes).to_bytes(8, "big") + header_bytes)
        self.assertEqual(cpp["anchor"]["context_sha256"], expected_context)
        self.assertEqual(cpp["anchor"]["context_algorithm"], "line-window-3-v1")

        tidy = by_locator["Diagnostics[0]"]
        self.assertEqual(tidy["primary_location"], {
            "file": str(repository / "src/unit-0.cpp"), "line": None, "column": None, "offset": 4,
        })
        csa = by_locator["diagnostics[0]"]
        self.assertEqual(csa["primary_location"], {
            "file": str(repository / "src/unit-0.cpp"), "line": 1, "column": 1, "offset": None,
        })

        receipt = json.loads((self.output / "delivery-receipt.json").read_text(encoding="utf-8"))
        self.assertTrue(any(item["path"] == reference["path"] and item["sha256"] == reference["sha256"]
                            for item in receipt["files"]))
        self.assertFalse(any("/content/" in item["path"] for item in receipt["files"]))

    def test_many_diagnostics_in_one_large_header_read_its_snapshot_once(self):
        repository = self.folder / "many-header-repository"
        header_relative = "src/vendor/shared-diagnostic.hpp"
        header_bytes = b"int shared_diagnostic;\n" + b"// padding line\n" * 120_000
        run = self.make_run(
            unit_count=1, positive_tidy_units=(), csa_findings=(),
            repository_root=repository, diagnostic_header=header_relative,
            diagnostic_header_bytes=header_bytes,
        )
        profile = handoff._load_json(self.profile_path, "fixture profile")[0]
        validated = handoff._validate_run(run, profile)
        cpp_path = validated["cpp_xml"]
        xml_root = ET.fromstring(cpp_path.read_bytes())
        errors = xml_root.find("./errors")
        self.assertIsNotNone(errors)
        first_error = errors.find("error")
        self.assertIsNotNone(first_error)
        diagnostic_count = 128
        for _ in range(diagnostic_count - 1):
            errors.append(copy.deepcopy(first_error))
        cpp_path.write_bytes(ET.tostring(xml_root, encoding="utf-8"))
        artifact_digest = _sha(cpp_path.read_bytes())
        validated["cpp_diagnostics"] = diagnostic_count
        snapshot_reads = []
        tracked_checks = []
        original_read_file = handoff._read_file
        original_tracked_check = handoff._git_path_is_tracked

        def count_snapshot_reads(path, label, max_bytes=handoff.MAX_FILE_BYTES):
            if label == "pre-scan source snapshot":
                snapshot_reads.append(Path(path))
            return original_read_file(path, label, max_bytes)

        def count_tracked_checks(repository_root, relative_path):
            tracked_checks.append(relative_path)
            return original_tracked_check(repository_root, relative_path)

        with patch.object(handoff, "_read_file", side_effect=count_snapshot_reads), \
             patch.object(handoff, "_git_path_is_tracked", side_effect=count_tracked_checks):
            sidecar = handoff._native_anchor_rows(
                validated,
                [{"tool": "cppcheck", "source": cpp_path, "sha256": artifact_digest}],
                "fixture-product", profile["repository"],
            )

        self.assertEqual(len(sidecar["entries"]), diagnostic_count)
        self.assertEqual(len(snapshot_reads), 1)
        self.assertEqual(tracked_checks, [header_relative])
        self.assertEqual(snapshot_reads[0].name, _sha(header_bytes))
        expected = hashlib.sha256()
        for line in header_bytes.splitlines(keepends=True)[:4]:
            expected.update(len(line).to_bytes(8, "big"))
            expected.update(line)
        expected_digest = expected.hexdigest()
        self.assertTrue(all(entry["anchor"]["status"] == "available" for entry in sidecar["entries"]))
        self.assertTrue(all(entry["anchor"]["context_sha256"] == expected_digest
                            for entry in sidecar["entries"]))

    def test_batched_line_windows_and_offsets_match_the_original_byte_protocol(self):
        for raw in (b"", b"one\ntwo\nthree\n", b"one\r\ntwo\nthree", b"one\rtwo\r", b"last line"):
            lines = []
            start = 0
            for index, byte in enumerate(raw):
                if byte == 10:
                    lines.append(raw[start:index + 1])
                    start = index + 1
            if start < len(raw) or not lines:
                lines.append(raw[start:])

            target_lines = list(range(-1, len(lines) + 3))
            actual = handoff._line_window_sha256_many(raw, target_lines)
            expected = {}
            for line_number in target_lines:
                if line_number < 1:
                    continue
                if line_number > len(lines):
                    expected[line_number] = None
                    continue
                digest = hashlib.sha256()
                for line in lines[max(0, line_number - 4):line_number + 3]:
                    digest.update(len(line).to_bytes(8, "big"))
                    digest.update(line)
                expected[line_number] = digest.hexdigest()
            self.assertEqual(actual, expected)

            offsets = list(range(len(raw) + 2))
            self.assertEqual(
                handoff._source_lines_for_offsets(raw, offsets),
                {offset: handoff._source_line_for_offset(raw, offset) for offset in offsets},
            )

    def test_diagnostic_anchor_sidecar_is_present_for_a_zero_finding_delivery(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        handoff.export_run(self.profile_path, root, self.output)
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        sidecar = json.loads((self.output / manifest["diagnostic_anchors"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(sidecar["entries"], [])

    def test_csa_anchor_parsing_matches_adapter_when_plist_files_array_is_omitted(self):
        repository = self.folder / "csa-no-files-repository"
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=(0,), repository_root=repository)
        unit = str(repository / "src/unit-0.cpp")
        plist_path = root / "group-1/evidence-bundle/05-clang-static-analyzer/result/csa-native" / (
            hashlib.sha256(unit.encode()).hexdigest()[:20] + ".plist"
        )
        plist = plistlib.loads(plist_path.read_bytes())
        plist.pop("files", None)
        plist["diagnostics"][0]["location"]["file"] = unit
        plist_path.write_bytes(plistlib.dumps(plist))
        relative = plist_path.relative_to(root / "group-1/evidence-bundle").as_posix()
        _rebind_evidence_artifact(root, relative)

        handoff.export_run(self.profile_path, root, self.output)
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        sidecar = json.loads((self.output / manifest["diagnostic_anchors"]["path"]).read_text(encoding="utf-8"))
        csa = next(item for item in sidecar["entries"] if item["locator"] == "diagnostics[0]")
        self.assertEqual(csa["primary_location"], {
            "file": unit, "line": 1, "column": 1, "offset": None,
        })
        self.assertEqual(csa["anchor"]["status"], "available")
        self.assertEqual(csa["anchor"]["repository_path"], "src/unit-0.cpp")

    def test_diagnostic_anchor_sidecar_marks_relative_cppcheck_path_unavailable(self):
        repository = self.folder / "relative-cppcheck-repository"
        header_relative = "src/vendor/diagnostic.hpp"
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=(),
                             repository_root=repository, diagnostic_header=header_relative)
        xml_path = root / "group-1/evidence-bundle/03-cppcheck/result/cppcheck-compile-report.xml"
        xml = xml_path.read_text(encoding="utf-8").replace(str(repository / header_relative), header_relative)
        xml_path.write_text(xml, encoding="utf-8")
        _rebind_evidence_artifact(root, "03-cppcheck/result/cppcheck-compile-report.xml")
        handoff.export_run(self.profile_path, root, self.output)
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        sidecar = json.loads((self.output / manifest["diagnostic_anchors"]["path"]).read_text(encoding="utf-8"))
        cpp = next(item for item in sidecar["entries"] if item["locator"] == "errors.error[0]")
        self.assertEqual(cpp["primary_location"]["file"], header_relative)
        self.assertEqual(cpp["anchor"], {"status": "unavailable", "reason": "relative-path-without-captured-base"})

    def test_diagnostic_anchor_sidecar_marks_changed_dependency_inventory_unavailable(self):
        repository = self.folder / "changed-dependency-repository"
        root = self.make_run(unit_count=1, positive_tidy_units=(0,), csa_findings=(), repository_root=repository)
        run_manifest_path = root / "manifest.json"
        run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
        after = run_manifest["groups"][0]["input_baseline_after"]
        first_path = next(iter(after["files"]))
        after["files"][first_path] = "f" * 64
        _write_json(run_manifest_path, run_manifest)
        handoff.export_run(self.profile_path, root, self.output)
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        sidecar = json.loads((self.output / manifest["diagnostic_anchors"]["path"]).read_text(encoding="utf-8"))
        self.assertTrue(sidecar["entries"])
        self.assertTrue(all(item["anchor"] == {"status": "unavailable", "reason": "postscan-inputs-changed"}
                            for item in sidecar["entries"]))

    def test_diagnostic_anchor_duplicate_artifact_locator_is_never_assigned_an_anchor(self):
        repository = self.folder / "duplicate-anchor-repository"
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=(),
                             repository_root=repository, diagnostic_header="src/vendor/diagnostic.hpp")
        profile = handoff._load_json(self.profile_path, "fixture profile")[0]
        validated = handoff._validate_run(root, profile)
        cpp_path = validated["cpp_xml"]
        digest = _sha(cpp_path.read_bytes())
        validated["cpp_diagnostics"] = 2
        sidecar = handoff._native_anchor_rows(validated, [
            {"tool": "cppcheck", "source": cpp_path, "sha256": digest},
            {"tool": "cppcheck", "source": cpp_path, "sha256": digest},
        ], "fixture-product", profile["repository"])
        self.assertEqual(len(sidecar["entries"]), 2)
        self.assertEqual(sidecar["entries"][0]["artifact_sha256"], sidecar["entries"][1]["artifact_sha256"])
        self.assertEqual(sidecar["entries"][0]["locator"], sidecar["entries"][1]["locator"])
        self.assertEqual(sidecar["entries"][0]["primary_location"], sidecar["entries"][1]["primary_location"])
        self.assertTrue(all(item["anchor"] == {"status": "unavailable", "reason": "duplicate-artifact-digest"}
                            for item in sidecar["entries"]))

    def test_diagnostic_anchor_snapshot_tampering_only_disables_anchor(self):
        repository = self.folder / "tampered-anchor-repository"
        root = self.make_run(unit_count=1, positive_tidy_units=(0,), csa_findings=(), repository_root=repository)
        evidence_root = root / "group-1/evidence-bundle"
        baseline = json.loads((evidence_root / "01-metadata/input-baseline/before/baseline.json").read_text())
        snapshot_digest = next(iter(baseline["snapshots"]))
        snapshot_relative = "01-metadata/input-baseline/before/content/" + snapshot_digest
        snapshot_path = evidence_root / snapshot_relative
        snapshot_path.write_bytes(b"tampered source snapshot\n")
        _rebind_evidence_artifact(root, snapshot_relative)
        handoff.export_run(self.profile_path, root, self.output)
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        sidecar = json.loads((self.output / manifest["diagnostic_anchors"]["path"]).read_text(encoding="utf-8"))
        self.assertTrue(sidecar["entries"])
        self.assertTrue(all(item["anchor"] == {"status": "unavailable", "reason": "snapshot-hash-mismatch"}
                            for item in sidecar["entries"]))

    def test_checked_zip_rejects_unbound_diagnostic_anchor_reference(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        handoff.export_run(self.profile_path, root, self.output)
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["diagnostic_anchors"]["sha256"] = "f" * 64
        _write_json(manifest_path, manifest)
        receipt_path = self.output / "delivery-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        manifest_record = next(item for item in receipt["files"] if item["path"] == "manifest.json")
        manifest_record["sha256"] = _sha(manifest_path.read_bytes())
        manifest_record["size"] = manifest_path.stat().st_size
        receipt["manifest"] = {"path": "manifest.json", **manifest_record}
        _write_json(receipt_path, receipt)
        with self.assertRaisesRegex(deliver.DeliveryError, "diagnostic anchor reference"):
            deliver.package_delivery(self.output, self.folder / "unbound-anchors.zip")

    def test_all_zero_tidy_target_has_typed_zero_receipt_but_no_yaml_artifact(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        handoff.export_run(self.profile_path, root, self.output)
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertFalse(any(item["tool"] == "clang-tidy" for item in manifest["artifacts"]))
        receipt = next(item for item in manifest["provenance_inputs"]
                       if item.get("evidence_type") == "clang-tidy-runner-zero-result-receipt")
        summary = json.loads((self.output / receipt["path"]).read_text())
        self.assertEqual(summary["warnings"], {"raw_total": 0, "unique_total": 0, "by_check": []})
        self.assertEqual(summary["files"][0]["unique_diagnostics"], 0)
        self.assertEqual(receipt["analysis_units"], manifest["components"][0]["analysis_units"])
        self.assertEqual(manifest["counts"]["tidy_units_without_native_yaml"], 1)

    def test_failed_scan_and_evidence_gate_are_not_exportable(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["exit_code"] = 3
        manifest["evidence_checks_ok"] = False
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "failed or incomplete|evidence gate"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_ambiguous_multiple_group_input_is_rejected(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["groups"].append(manifest["groups"][0])
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "exactly one"):
            handoff.export_run(self.profile_path, root, self.output)

    def test_profile_scope_mismatch_is_rejected_without_product_inference(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        profile = json.loads(self.profile_path.read_text())
        profile["included_paths"] = ["other/**"]
        _write_json(self.profile_path, profile)
        with self.assertRaisesRegex(handoff.HandoffError, "outside the explicit product-profile"):
            handoff.export_run(self.profile_path, root, self.output)

    def test_explicit_profile_ownership_is_carried_without_inference(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        profile = json.loads(self.profile_path.read_text())
        declared = [
            {"path": "src", "ownership": "first-party"},
            {"path": "src/vendor", "ownership": "vendored"},
        ]
        profile["path_ownership"] = declared
        _write_json(self.profile_path, profile)

        handoff.export_run(self.profile_path, root, self.output)

        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["source"]["scope"]["path_ownership"], declared)
        self.assertNotIn("source_path_identities", manifest["source"])

    def test_path_ownership_profile_rejects_globs_traversal_and_duplicates(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        base_profile = json.loads(self.profile_path.read_text())
        for label, declarations, message in (
            ("glob", [{"path": "vendor/**", "ownership": "vendored"}], "literal relative prefix"),
            ("traversal", [{"path": "../vendor", "ownership": "vendored"}], "literal relative prefix"),
            ("neutral-label-namespace", [
                {"path": "diagnostic-files", "ownership": "vendored"},
            ], "neutral diagnostic-label namespace"),
            ("duplicate", [
                {"path": "vendor", "ownership": "vendored"},
                {"path": "vendor", "ownership": "system"},
            ], "duplicate paths"),
        ):
            with self.subTest(case=label):
                profile = {**base_profile, "path_ownership": declarations}
                _write_json(self.profile_path, profile)
                with self.assertRaisesRegex(handoff.HandoffError, message):
                    handoff.export_run(self.profile_path, root, self.output)
                self.assertFalse(self.output.exists())

    def test_source_path_identity_requires_in_repo_file_and_matching_clean_commit(self):
        repository = self.folder / "source-repository"
        header = repository / "vendor" / "shared.hpp"
        header.parent.mkdir(parents=True)
        header.write_bytes(b"synthetic header bytes\n")
        outside = self.folder / "outside.hpp"
        outside.write_bytes(b"outside bytes\n")
        validated = {"repository_root": str(repository), "commit": "a" * 40}
        native_paths = [(str(header), None), (str(outside), None)]
        with (
            patch.object(handoff, "_git_checkout_matches_commit", return_value=True),
            patch.object(handoff, "_git_path_is_tracked", return_value=True),
            patch.object(handoff, "_primary_native_locations", return_value=native_paths),
        ):
            identities = handoff._source_path_identities(validated)
        self.assertEqual(identities, [{
            "native_path": str(header),
            "relative_path": "vendor/shared.hpp",
            "sha256": _sha(header.read_bytes()),
        }])
        with patch.object(handoff, "_git_checkout_matches_commit", return_value=False):
            self.assertEqual(handoff._source_path_identities(validated), [])

    def test_export_captures_primary_header_identity_from_clean_tracked_checkout(self):
        repository = self.folder / "tracked-source"
        header_relative = "vendor/shared.hpp"
        root = self.make_run(
            unit_count=1,
            positive_tidy_units=(),
            csa_findings=(),
            profile_include=("src/**",),
            repository_root=repository,
            diagnostic_header=header_relative,
        )

        handoff.export_run(self.profile_path, root, self.output)

        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        identities = manifest["source"].get("source_path_identities")
        self.assertIsInstance(identities, list)
        header = next(item for item in identities if item["relative_path"] == header_relative)
        header_path = repository / header_relative
        self.assertEqual(header["native_path"], str(header_path))
        self.assertEqual(header["sha256"], _sha(header_path.read_bytes()))
        self.assertEqual(manifest["source"]["commit"], subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=True,
        ).stdout.decode("ascii").strip())

    def test_git_tracking_lookup_handles_literal_glob_characters(self):
        repository = self.folder / "git-tracked-paths"
        repository.mkdir()
        initialized = subprocess.run(
            ["git", "-C", str(repository), "init", "--quiet"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr.decode("utf-8", "replace"))
        ordinary = repository / "README.md"
        literal_glob = repository / "vendor" / "[literal]*.hpp"
        literal_glob.parent.mkdir()
        ordinary.write_text("tracked\n", encoding="utf-8")
        literal_glob.write_text("tracked literal filename\n", encoding="utf-8")
        added = subprocess.run(
            ["git", "-C", str(repository), "add", "--all"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False,
        )
        self.assertEqual(added.returncode, 0, added.stderr.decode("utf-8", "replace"))

        self.assertTrue(handoff._git_path_is_tracked(repository, "README.md"))
        self.assertTrue(handoff._git_path_is_tracked(repository, "vendor/[literal]*.hpp"))
        self.assertFalse(handoff._git_path_is_tracked(repository, "vendor/literal-anything.hpp"))

    def test_identity_baseline_requires_clean_tracked_and_untracked_worktree(self):
        repository = self.folder / "clean-baseline"
        repository.mkdir()
        initialized = subprocess.run(
            ["git", "-C", str(repository), "init", "--quiet"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr.decode("utf-8", "replace"))
        (repository / "tracked.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repository), "add", "--all"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=True,
        )
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Synthetic Test",
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic Test",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        }
        subprocess.run(
            ["git", "-C", str(repository), "commit", "--quiet", "-m", "clean baseline"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=True, env=git_env,
        )
        head = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=True,
        ).stdout.decode("ascii").strip()
        self.assertTrue(handoff._git_checkout_matches_commit(repository, head))
        (repository / "untracked.hpp").write_text("// untracked\n", encoding="utf-8")
        self.assertFalse(handoff._git_checkout_matches_commit(repository, head))

    def test_subdirectory_scan_root_exports_repo_relative_paths_and_preserves_raw_receipts(self):
        root = self.make_run(
            source_subdirectory="examples/cpp",
            profile_include=("examples/cpp/**",),
        )
        evidence_root = root / "group-1/evidence-bundle"
        summary_relative = "04-clang-tidy/result/clang-tidy-summary.json"
        summary_path = evidence_root / summary_relative
        source_summary_raw = summary_path.read_bytes()
        source_summary_hash = _sha(source_summary_raw)
        source_hashes_raw = (evidence_root / "01-metadata/source-hashes.json").read_bytes()

        handoff.export_run(self.profile_path, root, self.output)

        manifest = json.loads((self.output / "manifest.json").read_text())
        units = manifest["components"][0]["analysis_units"]
        relative_units = manifest["components"][0]["relative_source_paths"]
        self.assertEqual(relative_units, {
            units[0]: "examples/cpp/smoke.cpp",
            units[1]: "examples/cpp/tinyxml2/tinyxml2.cpp",
        })
        source_summary = next(
            item for item in manifest["provenance_inputs"]
            if item.get("source_path") == summary_relative and item.get("evidence_type") is None
        )
        self.assertEqual(source_summary["sha256"], source_summary_hash)
        self.assertEqual((self.output / source_summary["path"]).read_bytes(), source_summary_raw)
        zero_receipt = next(
            item for item in manifest["provenance_inputs"]
            if item.get("evidence_type") == "clang-tidy-runner-zero-result-receipt"
        )
        self.assertEqual(zero_receipt["path"], source_summary["path"])
        self.assertEqual(zero_receipt["sha256"], source_summary_hash)
        self.assertEqual(zero_receipt["source_path"], summary_relative)
        self.assertNotIn("identity_normalization", zero_receipt)
        summary = json.loads((self.output / zero_receipt["path"]).read_text())
        self.assertEqual({item["file"] for item in summary["files"]}, set(units))
        source_hash_receipt = next(
            item for item in manifest["provenance_inputs"]
            if item.get("source_path") == "01-metadata/source-hashes.json"
        )
        self.assertEqual((self.output / source_hash_receipt["path"]).read_bytes(), source_hashes_raw)

    def test_captured_source_root_outside_repository_is_rejected(self):
        root = self.make_run(
            source_subdirectory="examples/cpp",
            profile_include=("examples/cpp/**",),
        )
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["groups"][0]["baseline"]["root"] = "/synthetic/outside/examples/cpp"
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "source root is outside the Git repository"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_captured_source_root_parent_traversal_is_rejected(self):
        root = self.make_run(
            source_subdirectory="examples/cpp",
            profile_include=("examples/cpp/**",),
        )
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["groups"][0]["baseline"]["root"] = "/synthetic/source-root/examples/../outside"
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "missing or invalid"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_selected_translation_unit_outside_repository_is_rejected(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["groups"][0]["selected_files"][0] = "/synthetic/outside/unit.cpp"
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "selected translation unit is outside"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_selected_translation_unit_parent_traversal_is_rejected(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["groups"][0]["selected_files"][0] = "/synthetic/source-root/../outside/unit.cpp"
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "cannot be represented relative"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_scope_relative_clang_tidy_evidence_is_rejected(self):
        root = self.make_run(
            source_subdirectory="examples/cpp",
            profile_include=("examples/cpp/**",),
        )
        relative = "04-clang-tidy/result/clang-tidy-summary.json"
        summary_path = root / "group-1/evidence-bundle" / relative
        summary = json.loads(summary_path.read_text())
        summary["files"][0]["file"] = "smoke.cpp"
        summary["files"][1]["file"] = "tinyxml2/tinyxml2.cpp"
        _write_json(summary_path, summary)
        _rebind_evidence_artifact(root, relative)
        with self.assertRaisesRegex(handoff.HandoffError, "summary TU inventory differs"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_native_tampering_is_rejected_and_profiles_cannot_bind_reporter_assets(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        xml = root / "group-1/evidence-bundle/03-cppcheck/result/cppcheck-compile-report.xml"
        xml.write_bytes(xml.read_bytes().replace(b"2.22.0", b"2.21.0"))
        with self.assertRaisesRegex(handoff.HandoffError, "SHA-256 mismatch"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        profile = json.loads(self.profile_path.read_text())
        profile["mappings"] = {"path": "mapping.json", "sha256": "0" * 64}
        _write_json(self.profile_path, profile)
        with self.assertRaisesRegex(handoff.HandoffError, "exactly the documented delivery fields"):
            handoff.export_run(self.profile_path, root, self.output)

    def test_prerelease_version_and_native_version_conflict_are_rejected(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        stdout = root / "cppcheck.version.txt"
        stdout.write_text("Cppcheck 2.22.0-dev\n")
        with self.assertRaisesRegex(handoff.HandoffError, "prerelease|development"):
            handoff.export_run(self.profile_path, root, self.output)

        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        manifest = json.loads((root / "group-1/evidence-bundle/manifest.json").read_text())
        csa_artifact = next(item for item in manifest["artifacts"] if item["path"].endswith(".plist"))
        csa_path = root / "group-1/evidence-bundle" / csa_artifact["path"]
        native = plistlib.loads(csa_path.read_bytes())
        native["clang_version"] = "Ubuntu clang version 17.0.6 (1ubuntu1)"
        csa_path.write_bytes(plistlib.dumps(native))
        # The old evidence inventory must be re-bound to the changed plist so
        # the exporter reaches and tests the native/tool version comparison.
        csa_artifact["sha256"] = _sha(csa_path.read_bytes())
        csa_artifact["size"] = csa_path.stat().st_size
        _write_json(root / "group-1/evidence-bundle/manifest.json", manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "release token conflicts"):
            handoff.export_run(self.profile_path, root, self.output)

    def test_tidy_warning_aggregate_counts_are_typed_and_reconcile_by_check(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        relative = "04-clang-tidy/result/clang-tidy-summary.json"
        summary_path = root / "group-1/evidence-bundle" / relative
        summary = json.loads(summary_path.read_text())
        summary["warnings"]["unique_total"] = True
        _write_json(summary_path, summary)
        _rebind_evidence_artifact(root, relative)
        with self.assertRaisesRegex(handoff.HandoffError, "invalid aggregate counts"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        summary_path = root / "group-1/evidence-bundle" / relative
        summary = json.loads(summary_path.read_text())
        summary["warnings"]["raw_total"] = 1
        _write_json(summary_path, summary)
        _rebind_evidence_artifact(root, relative)
        with self.assertRaisesRegex(handoff.HandoffError, "do not reconcile with by-check"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_csa_per_unit_receipt_rejects_boolean_status_and_count_values(self):
        for key, value in (("ok", 1), ("exit_code", False), ("diagnostics", True)):
            with self.subTest(key=key):
                root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=(0,))
                relative = "05-clang-static-analyzer/result/compile-db/analyzer-status.json"
                status_path = root / "group-1/evidence-bundle" / relative
                status = json.loads(status_path.read_text())
                status["files"][0][key] = value
                _write_json(status_path, status)
                _rebind_evidence_artifact(root, relative)
                with self.assertRaisesRegex(handoff.HandoffError, "per-TU receipt conflicts"):
                    handoff.export_run(self.profile_path, root, self.output)
                self.assertFalse(self.output.exists())

    def test_existing_output_is_refused_without_mutation(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("do not overwrite")
        with self.assertRaisesRegex(handoff.HandoffError, "refusing to overwrite"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertEqual(sentinel.read_text(), "do not overwrite")

    def test_delivery_is_relocatable_and_export_needs_no_original_run_after_publication(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=(0,))
        result = handoff.export_run(self.profile_path, root, self.output)
        moved = self.folder / "different-machine" / "delivery"
        moved.parent.mkdir()
        shutil.copytree(self.output, moved)
        shutil.rmtree(root)
        manifest = json.loads((moved / "manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], "three-tool-scan-delivery-1.0")
        self.assertEqual(manifest["project"]["product_id"], "fixture-product")
        self.assertEqual(manifest["artifacts"][0]["path"], "native/cppcheck.xml")
        self.assertTrue(all(not Path(item["path"]).is_absolute() for item in manifest["artifacts"]))
        receipt = json.loads((moved / "delivery-receipt.json").read_text())
        self.assertEqual(receipt["manifest"]["sha256"], result["manifest_sha256"])
        self.assertTrue(all((moved / item["path"]).is_file() for item in receipt["files"]))

    def test_raw_evidence_symlink_is_rejected_even_when_target_stays_inside_run(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        evidence_root = root / "group-1/evidence-bundle"
        xml = evidence_root / "03-cppcheck/result/cppcheck-compile-report.xml"
        target = xml.with_name("cppcheck-native-target.xml")
        xml.rename(target)
        xml.symlink_to(target.name)
        with self.assertRaisesRegex(handoff.HandoffError, "symlink"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_raw_version_path_symlink_is_rejected_before_resolving_target(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        target = root / "cppcheck.version.txt"
        link = root / "version-link.txt"
        link.symlink_to(target.name)
        manifest["versions"]["cppcheck"]["stdout"] = str(link)
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "symlink"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_checked_zip_has_formal_root_and_keeps_positive_findings_as_complete(self):
        root = self.make_run(unit_count=2, positive_tidy_units=(0,), csa_findings=(0,))
        handoff.export_run(self.profile_path, root, self.output)
        archive_path = self.folder / "portable-delivery.zip"

        packed = deliver.package_delivery(self.output, archive_path)

        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            manifest_raw = archive.read("manifest.json")
            manifest = json.loads(manifest_raw)
            receipt = json.loads(archive.read("delivery-receipt.json"))
        self.assertEqual(packed["manifest_at_root"], True)
        self.assertIn("manifest.json", names)
        self.assertIn("delivery-receipt.json", names)
        self.assertFalse(any(name.startswith("bundle/") for name in names))
        self.assertEqual(manifest["schema_version"], "three-tool-scan-delivery-1.0")
        self.assertEqual(receipt["schema_version"], "three-tool-scan-delivery-receipt-1.0")
        self.assertEqual(manifest["counts"]["scanner_reported_diagnostics"]["clang_tidy_log_summary_unique"], 1)
        self.assertEqual(manifest["counts"]["scanner_reported_diagnostics"]["clang_static_analyzer_native_plists"], 1)
        self.assertEqual(receipt["manifest"]["sha256"], _sha(manifest_raw))

    def test_checked_zip_accepts_a_complete_zero_diagnostic_delivery(self):
        root = self.make_run(unit_count=2, positive_tidy_units=(), csa_findings=())
        handoff.export_run(self.profile_path, root, self.output)
        archive_path = self.folder / "zero-delivery.zip"

        deliver.package_delivery(self.output, archive_path)

        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        counts = manifest["counts"]["scanner_reported_diagnostics"]
        self.assertEqual(counts, {"cppcheck_native_xml": 0, "clang_tidy_log_summary_unique": 0,
                                  "clang_static_analyzer_native_plists": 0})
        self.assertEqual(manifest["counts"]["tidy_units_without_native_yaml"], 2)
        self.assertFalse(any(item["tool"] == "clang-tidy" for item in manifest["artifacts"]))

    def test_checked_zip_rejects_tampered_scope_and_unbound_files(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        handoff.export_run(self.profile_path, root, self.output)
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source"]["scope"]["included_paths"] = ["other/**"]
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(deliver.DeliveryError, "differs from its receipt"):
            deliver.package_delivery(self.output, self.folder / "tampered-scope.zip")

        handoff.export_run(self.profile_path, root, self.folder / "clean-delivery")
        (self.folder / "clean-delivery" / "extra.txt").write_text("unbound")
        with self.assertRaisesRegex(deliver.DeliveryError, "outside its receipt closure"):
            deliver.package_delivery(self.folder / "clean-delivery", self.folder / "unbound.zip")

    def test_checked_zip_refuses_existing_destination_and_detects_archive_tampering(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        handoff.export_run(self.profile_path, root, self.output)
        archive_path = self.folder / "immutable.zip"
        archive_path.write_bytes(b"keep-existing")
        with self.assertRaisesRegex(deliver.DeliveryError, "already exists"):
            deliver.package_delivery(self.output, archive_path)
        self.assertEqual(archive_path.read_bytes(), b"keep-existing")

        archive_path.unlink()
        deliver.package_delivery(self.output, archive_path)
        inventory = deliver._delivery_inventory(self.output)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
            with zipfile.ZipFile(archive_path, "a") as archive:
                archive.writestr("native/cppcheck.xml", b"tampered")
        with self.assertRaisesRegex(deliver.DeliveryError, "exactly match"):
            deliver._verify_zip(archive_path, inventory)

    def test_zip_over_upload_limit_is_not_published_and_delivery_directory_is_preserved(self):
        root = self.make_run(unit_count=1, positive_tidy_units=(), csa_findings=())
        handoff.export_run(self.profile_path, root, self.output)
        archive_path = self.folder / "oversize-delivery.zip"

        with patch.object(deliver, "MAX_ARCHIVE_BYTES", 1):
            with self.assertRaisesRegex(deliver.DeliveryError, "upload limit of 64 MiB"):
                deliver.package_delivery(self.output, archive_path)

        self.assertTrue(self.output.is_dir())
        self.assertFalse(archive_path.exists())
        self.assertFalse(list(self.folder.glob(".oversize-delivery.zip.*.tmp")))

    def test_regular_file_read_bound_is_enforced(self):
        path = self.folder / "bounded.txt"
        path.write_bytes(b"0123456789abcdefg")
        with self.assertRaisesRegex(handoff.HandoffError, "maximum supported file size"):
            handoff._read_file(path, "bounded test input", max_bytes=16)


if __name__ == "__main__":
    unittest.main()
