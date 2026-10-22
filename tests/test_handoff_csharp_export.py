from __future__ import annotations

import csv
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scan"))
import evidence
import handoff
from csharp_roslyn import scanner as csharp_scanner


STAMP = "2026-10-22T10:00:00+00:00"
STAMP_END = "2026-10-22T10:01:00+00:00"
COMMIT = "b" * 40
DOTNET = "/tools/dotnet"
POLICY = {"jobs": 4, "analyzers": "Microsoft.CodeAnalysis.NetAnalyzers",
          "flags": list(csharp_scanner.FLAGS)}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, value: object) -> bytes:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _rebind_evidence_artifact(root: Path, relative_path: str) -> None:
    evidence_root = root / "group-1/evidence-bundle"
    manifest_path = evidence_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    item = next(item for item in manifest["artifacts"] if item["path"] == relative_path)
    raw = (evidence_root / relative_path).read_bytes()
    item["sha256"] = _sha(raw)
    item["size"] = len(raw)
    _write_json(manifest_path, manifest)


class CSharpHandoffExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runner-csharp-handoff-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.repo = self.folder / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src/App.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            "<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n")
        for index in range(2):
            (self.repo / "src" / ("Unit-" + str(index) + ".cs")).write_text(
                "namespace Sample { public static class Unit" + str(index) + " { } }\n")
        self.profile_path = self.folder / "profile.json"
        self.output = self.folder / "csharp-delivery"
        self.run_root: Path | None = None

    def make_profile(self, *, languages=("C#",), include=("src/**",)):
        profile = {
            "schema_version": handoff.PROFILE_SCHEMA,
            "product_id": "guardclauses-example",
            "display_name": "Synthetic C# exporter fixture",
            "repository": "https://example.invalid/org/csharp-repository",
            "component_id": "guardclauses-component",
            "target_id": "net8.0-release-target",
            "configuration": "release-config-v1",
            "included_paths": list(include),
            "excluded_paths": ["generated/**", "obj/**"],
            "languages": list(languages),
            "unsupported_languages": ["C", "C++"],
        }
        _write_json(self.profile_path, profile)
        return profile

    def make_run(self, *, sarif_results=(), diagnostics=None, unit_count=1, project=None,
                 commit=COMMIT, after_status="", after_commit=None, frameworks=("net8.0",),
                 compiled=None, analyzer_version="8.0.0", analyzer_limitations=(),
                 compiler_semantic="4.11.0"):
        self.make_profile()
        root = self.folder / ("20261022T100000Z-" + uuid.uuid4().hex[:12])
        self.run_root = root
        evidence_root = root / "group-1/evidence-bundle"
        metadata = evidence_root / "01-metadata"
        metadata.mkdir(parents=True)
        project_path = Path(project) if project is not None else self.repo / "src/App.csproj"
        units = [str(self.repo / "src" / ("Unit-" + str(index) + ".cs")) for index in range(unit_count)]
        source_hashes = {unit: _sha(Path(unit).read_bytes()) for unit in units}
        dotnet_stdout = b"8.0.100\n"
        dotnet_stderr = b""
        (root / "dotnet.version.txt").write_bytes(dotnet_stdout)
        (root / "dotnet.version.err.txt").write_bytes(dotnet_stderr)
        for name, raw in (("stdout", dotnet_stdout), ("stderr", dotnet_stderr)):
            (metadata / ("dotnet.version." + name + ".txt")).write_bytes(raw)
        for label, status, current in (("before", "", commit), ("after", after_status, after_commit or commit)):
            (metadata / (label + "-git-repo.txt")).write_text(str(self.repo) + "\n")
            (metadata / (label + "-git-branch.txt")).write_text("main\n")
            (metadata / (label + "-git-commit.txt")).write_text(current + "\n")
            (metadata / (label + "-git-status.txt")).write_text(status)
            (metadata / (label + "-git-diff.txt")).write_text("")
        _write_json(metadata / "source-hashes.json", source_hashes)
        (metadata / "code-state.txt").write_text(
            "repo: " + str(self.repo) + "\nbranch: main\ncommit: " + commit + "\ndirty: False\n\n")
        (metadata / "compile-db-selected-tus.txt").write_text("\n".join(units) + "\n")
        (metadata / "analy.config").write_text('{"scan_paths": [], "result_path": "results"}\n')
        _write_json(metadata / "scan-environment.json", {"synthetic": True})
        _write_json(metadata / "scanner-hashes.json", {"scan/runner.py": "c" * 64})
        _write_json(metadata / "repository-checks.json", [{"exit_code": 0}])

        if isinstance(sarif_results, dict):
            results_by_framework = {name: list(sarif_results.get(name, [])) for name in frameworks}
        else:
            results_by_framework = {name: list(sarif_results) for name in frameworks}
        compiled_by_framework = {name: list(compiled.get(name, units)) if compiled else list(units)
                                 for name in frameworks}
        driver = {"name": "Microsoft (R) Visual C# Compiler",
                  "version": "4.11.0-3.24510.3 (cfe3bd0a)",
                  "semanticVersion": compiler_semantic,
                  "dottedQuadFileVersion": "4.11.0.0"}
        sarif = evidence_root / handoff.CSHARP_SARIF_RELATIVE
        sarif.parent.mkdir(parents=True)
        _write_json(sarif, {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [{"tool": {"driver": dict(driver)},
                      "results": results_by_framework[framework],
                      "properties": {"targetFramework": framework}} for framework in frameworks],
        })
        artifacts = evidence_root / "06-csharp-roslyn/csharp-artifacts"
        artifacts.mkdir()
        per_framework = artifacts / "per-tfm"
        per_framework.mkdir()
        builds = []
        for framework in frameworks:
            native = per_framework / ("csharp-report." + framework + ".sarif")
            _write_json(native, {
                "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
                "version": "2.1.0",
                "runs": [{"tool": {"driver": dict(driver)}, "results": results_by_framework[framework]}],
            })
            builds.append({
                "target_framework": framework,
                "command": [DOTNET, "build", str(project_path), *csharp_scanner.FLAGS,
                            "-p:TargetFramework=" + framework,
                            "-p:ErrorLog=" + str(native) + "%2Cversion=2.1", "--no-incremental"],
                "cwd": str(project_path.parent), "exit_code": 0, "timed_out": False, "cancelled": False,
                "started": STAMP, "finished": STAMP_END,
                "stdout": str(root / ("group-1/csharp_roslyn/stdout." + framework + ".log")),
                "stderr": str(root / ("group-1/csharp_roslyn/stderr." + framework + ".log")),
                "sarif": str(native), "sarif_sha256": _sha(native.read_bytes()),
                "compile_items": compiled_by_framework[framework],
                "units_attempted": len(compiled_by_framework[framework]),
                "units_completed": len(compiled_by_framework[framework]),
                "diagnostics": len(results_by_framework[framework]), "ok": True,
            })
        total = sum(build["diagnostics"] for build in builds)
        receipt = {
            "cwd": str(project_path.parent), "project": str(project_path), "policy": dict(POLICY),
            "database": None, "selected_files": units, "target_frameworks": list(frameworks),
            "analyzers": {"name": csharp_scanner.ANALYZERS, "version": analyzer_version,
                          "source": csharp_scanner.ANALYZER_SDK_SOURCE if analyzer_version else None,
                          "limitations": list(analyzer_limitations)},
            "builds": builds, "sarif": str(sarif),
            "diagnostics": total if diagnostics is None else diagnostics,
            "units_attempted": sum(build["units_attempted"] for build in builds),
            "units_completed": sum(build["units_completed"] for build in builds),
            "discovered": 0, "started": STAMP, "finished": STAMP_END, "exit_code": 0, "ok": True,
        }
        _write_json(artifacts / "receipt.json", receipt)
        (artifacts / "command.txt").write_text(
            "\n".join(shlex.join(build["command"]) for build in builds) + "\n")
        (artifacts / "exit-code.txt").write_text(
            "".join("{} {}\n".format(build["target_framework"], build["exit_code"]) for build in builds))
        (artifacts / "version.txt").write_bytes(dotnet_stdout)
        with (artifacts / "csharp-reconciliation.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("tfm", "file", "status"))
            writer.writerows((framework, unit, "analyzed")
                             for framework in frameworks
                             for unit in compiled_by_framework[framework])

        gate = {
            "ok": False,
            "errors": ["Missing tool version: cppcheck", "Missing tool version: clang-tidy",
                       "Compiler dependency baseline unavailable or incomplete",
                       "Source fallback is not a three-tool compilation-database bundle",
                       "All three tools are required for certification"],
            "checks": {handoff.CSHARP_RUNNER_NAME: {"files": list(units), "errors": []}},
            "release_ready": False,
            "pending_certification": ["downstream_schema_and_ingestion"],
        }
        _write_json(evidence_root / "release-gate.json", gate)
        git = {"repo": str(self.repo), "branch": "main", "commit": commit,
               "status": "", "diff": "", "ok": True, "receipts": {}}
        group = {
            "database": None, "database_sha256": None, "mode": "source",
            "selected_database": None, "selected_database_sha256": None,
            "tool_versions": {"dotnet": {"command": [DOTNET, "--version"]}},
            "scan_paths": [str(self.repo)], "selected_files": units,
            "baseline": {"root": str(self.repo), "git": git, "sources": source_hashes, "captured_at": STAMP},
            "evidence_bundle": "group-1/evidence-bundle",
            "results": {handoff.CSHARP_RUNNER_NAME: dict(receipt)},
            "release_gate": gate,
        }
        run_manifest = {
            "runner_schema": handoff.RUNNER_SCHEMA, "run_id": root.name, "started_at": STAMP,
            "completed_at": STAMP_END, "run_path": str(root), "ok": True, "exit_code": 0,
            "cancelled": False, "evidence_gate_required": False, "evidence_checks_ok": False,
            "downstream_compatible": False, "release_ready": False,
            "tools": [handoff.CSHARP_RUNNER_NAME],
            "versions": {"dotnet": {
                "command": [DOTNET, "--version"], "stdout": str(root / "dotnet.version.txt"),
                "stderr": str(root / "dotnet.version.err.txt"), "exit_code": 0,
                "timed_out": False, "cancelled": False, "binary_sha256": "b" * 64,
            }},
            "policy": {"version": "tool-owned-scan-1", "tools": {handoff.CSHARP_RUNNER_NAME: dict(POLICY)}},
            "groups": [group],
        }
        _write_json(root / "manifest.json", run_manifest)
        inventory = []
        for path in sorted(item for item in evidence_root.rglob("*")
                           if item.is_file() and item.name != "manifest.json"):
            relative = path.relative_to(evidence_root).as_posix()
            inventory.append({"path": relative, "sha256": _sha(path.read_bytes()),
                              "size": path.stat().st_size, "layer": evidence.artifact_layer(relative)})
        _write_json(evidence_root / "manifest.json", {
            "schema": "scanner-evidence-1", "target_contract": "three-tool-input-1.0",
            "downstream_compatible": False, "release_ready": False, "commit": commit,
            "selected_files": units, "release_gate": gate, "artifacts": inventory,
        })
        return root

    def rebind_gate(self, root: Path, gate: dict) -> None:
        evidence_root = root / "group-1/evidence-bundle"
        _write_json(evidence_root / "release-gate.json", gate)
        run_manifest_path = root / "manifest.json"
        run_manifest = json.loads(run_manifest_path.read_text())
        run_manifest["groups"][0]["release_gate"] = gate
        _write_json(run_manifest_path, run_manifest)
        evidence_manifest_path = evidence_root / "manifest.json"
        evidence_manifest = json.loads(evidence_manifest_path.read_text())
        evidence_manifest["release_gate"] = gate
        _write_json(evidence_manifest_path, evidence_manifest)
        _rebind_evidence_artifact(root, "release-gate.json")

    def test_export_publishes_csharp_delivery_with_recomputable_bindings(self):
        root = self.make_run()
        result = handoff.export_csharp_run(self.profile_path, root, self.output)

        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], handoff.DELIVERY_SCHEMA)
        self.assertEqual(manifest["schema_version"], "three-tool-scan-delivery-1.0")
        for absent in ("assets", "mapping_policy", "producer_contract"):
            self.assertNotIn(absent, manifest)
        self.assertEqual(manifest["project"]["product_id"], "guardclauses-example")
        self.assertEqual(manifest["project"]["display_name"], "Synthetic C# exporter fixture")

        csproj = self.repo / "src/App.csproj"
        source = manifest["source"]
        self.assertEqual(source["repository"], "https://example.invalid/org/csharp-repository")
        self.assertEqual(source["commit"], COMMIT)
        self.assertEqual(source["build_configuration_sha256"], _sha(csproj.read_bytes()))
        self.assertEqual(source["scope"], {
            "included_components": ["guardclauses-component"],
            "included_paths": ["src/**"],
            "excluded_paths": ["generated/**", "obj/**"],
            "languages": ["C#"],
            "unsupported_languages": ["C", "C++"],
        })

        units = manifest["components"][0]["analysis_units"]
        self.assertEqual(len(units), 1)
        self.assertEqual(manifest["components"][0]["component_id"], "guardclauses-component")
        self.assertEqual(manifest["components"][0]["target_id"], "net8.0-release-target")
        self.assertEqual(manifest["components"][0]["configuration"], "release-config-v1")
        self.assertEqual(manifest["components"][0]["relative_source_paths"],
                         {units[0]: "src/Unit-0.cs"})

        self.assertEqual(len(manifest["tool_runs"]), 1)
        tool_run = manifest["tool_runs"][0]
        self.assertEqual(tool_run["tool"], "dotnet-netanalyzers")
        # The delivery binds the resolved NetAnalyzers version. The SARIF driver is
        # the C# compiler (4.11.0) and the SDK banner is 8.0.100; neither is the
        # analyzer identity.
        self.assertEqual(tool_run["tool_version"], "8.0.0")
        self.assertNotEqual(tool_run["tool_version"], "4.11.0")
        self.assertEqual(tool_run["component"], "guardclauses-component")
        self.assertEqual(tool_run["configuration"], "release-config-v1")
        self.assertEqual(tool_run["started_at"], STAMP)
        self.assertEqual(tool_run["completed_at"], STAMP_END)
        rules = next(item for item in manifest["provenance_inputs"]
                     if item["purpose"] == "rules-configuration")
        self.assertEqual(rules["path"], "provenance/rules/dotnet-netanalyzers.json")
        self.assertEqual(tool_run["rules_config_sha256"], _sha((self.output / rules["path"]).read_bytes()))
        self.assertEqual(rules["sha256"], tool_run["rules_config_sha256"])
        rules_value = json.loads((self.output / rules["path"]).read_text())
        self.assertEqual(rules_value["schema_version"], handoff.CSHARP_RULES_SCHEMA)
        self.assertEqual(rules_value["tool_version"], "8.0.0")
        self.assertEqual(rules_value["tool_version_source"], "dotnet-sdk")
        self.assertEqual(rules_value["sdk_version"], "8.0.100")
        self.assertEqual(rules_value["compiler_semantic_version"], "4.11.0")
        self.assertEqual(rules_value["compiler_versions"], ["4.11.0-3.24510.3 (cfe3bd0a)"])
        self.assertEqual(rules_value["target_frameworks"], ["net8.0"])
        self.assertEqual(rules_value["limitations"], [])
        self.assertEqual(rules_value["binary_sha256"], {"dotnet": "b" * 64})
        self.assertEqual(rules_value["runner_policy"], POLICY)

        target = tool_run["targets"][0]
        # A multi-target .NET project has one build target per framework, so the
        # declared identity carries the framework instead of reusing the profile id.
        self.assertEqual(target["target"], "net8.0-release-target[net8.0]")
        self.assertEqual(target["target_framework"], "net8.0")
        self.assertIs(target["attempted"], True)
        self.assertIs(target["analyzer_completed"], True)
        self.assertEqual(target["exit_status"], 0)
        self.assertEqual(target["outcome"], "success")
        self.assertEqual(target["analysis_units"], units)
        self.assertEqual(target["units_attempted"], len(units))
        self.assertEqual(target["units_completed"], len(units))
        self.assertEqual(target["started_at"], STAMP)
        self.assertEqual(target["completed_at"], STAMP_END)

        self.assertEqual(len(manifest["artifacts"]), 1)
        artifact = manifest["artifacts"][0]
        self.assertEqual(artifact["path"], "native/csharp-report.net8.0.sarif")
        self.assertEqual(artifact["format"], "csharp-sarif")
        self.assertEqual(artifact["tool"], "dotnet-netanalyzers")
        self.assertEqual(artifact["tool_version"], "8.0.0")
        self.assertEqual(artifact["component"], "guardclauses-component")
        self.assertEqual(artifact["configuration"], "release-config-v1")
        self.assertEqual(artifact["target"], "net8.0-release-target[net8.0]")
        self.assertEqual(artifact["sha256"], _sha((self.output / artifact["path"]).read_bytes()))
        # The native artifact is this framework's own compiler SARIF, byte-for-byte;
        # the merged multi-framework document stays a supporting convenience copy.
        per_framework = handoff.CSHARP_PER_FRAMEWORK_PREFIX + "csharp-report.net8.0.sarif"
        self.assertEqual((self.output / artifact["path"]).read_bytes(),
                         (root / "group-1/evidence-bundle" / per_framework).read_bytes())

        purposes = [item["purpose"] for item in manifest["provenance_inputs"]]
        for expected in ("product-scope-profile", "runner-execution-receipt", "scanner-evidence-inventory",
                         "scanner-evidence-gate", "build-configuration", "source-hash-inventory",
                         "rules-configuration", "tool-version-banner", "tool-binary-identities",
                         "native-scanner-output", "native-merged-supporting-copy"):
            self.assertIn(expected, purposes)
        build = next(item for item in manifest["provenance_inputs"] if item["purpose"] == "build-configuration")
        self.assertEqual(build["path"], "provenance/build/App.csproj")
        self.assertEqual(build["sha256"], source["build_configuration_sha256"])
        self.assertEqual((self.output / build["path"]).read_bytes(), csproj.read_bytes())

        for item in manifest["artifacts"] + manifest["provenance_inputs"]:
            listed = item["path"]
            self.assertFalse(Path(listed).is_absolute())
            self.assertNotIn("\\", listed)
            copied = self.output / listed
            self.assertTrue(copied.is_file())
            self.assertFalse(copied.is_symlink())
            self.assertEqual(_sha(copied.read_bytes()), item["sha256"])

        self.assertEqual(manifest["counts"]["selected_analysis_units"], 1)
        self.assertEqual(manifest["counts"]["artifact_count_by_tool"], {"dotnet-netanalyzers": 1})
        self.assertEqual(manifest["counts"]["scanner_reported_diagnostics"],
                         {"dotnet_netanalyzers_native_sarif": 0})
        self.assertEqual(manifest["counts"]["units_by_target_framework"], {"net8.0": 1})
        self.assertEqual(manifest["counts"]["compiled_units_by_target_framework"], {"net8.0": 1})
        self.assertEqual(manifest["counts"]["diagnostics_by_target_framework"], {"net8.0": 0})
        self.assertEqual(result["manifest_sha256"], _sha((self.output / "manifest.json").read_bytes()))

        delivery_receipt = json.loads((self.output / "delivery-receipt.json").read_text())
        self.assertEqual(delivery_receipt["manifest"]["sha256"], result["manifest_sha256"])
        for item in delivery_receipt["files"]:
            copied = self.output / item["path"]
            self.assertEqual(copied.stat().st_size, item["size"])
            self.assertEqual(_sha(copied.read_bytes()), item["sha256"])
        on_disk = {path.relative_to(self.output).as_posix()
                   for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(on_disk, {item["path"] for item in delivery_receipt["files"]}
                         | {"manifest.json", "delivery-receipt.json"})

    def test_positive_finding_is_exported_with_reported_count(self):
        finding = {
            "ruleId": "CA3001",
            "level": "error",
            "message": {"text": "Potential SQL injection vulnerability"},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": "Unit-0.cs"},
                "region": {"startLine": 3, "startColumn": 5},
            }}],
        }
        root = self.make_run(sarif_results=[finding])
        handoff.export_csharp_run(self.profile_path, root, self.output)
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["counts"]["scanner_reported_diagnostics"],
                         {"dotnet_netanalyzers_native_sarif": 1})
        self.assertEqual(manifest["tool_runs"][0]["targets"][0]["units_completed"], 1)

    def test_delivery_is_relocatable_without_the_original_run(self):
        root = self.make_run()
        result = handoff.export_csharp_run(self.profile_path, root, self.output)
        moved = self.folder / "different-machine" / "delivery"
        moved.parent.mkdir()
        shutil.copytree(self.output, moved)
        shutil.rmtree(root)
        manifest = json.loads((moved / "manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], "three-tool-scan-delivery-1.0")
        self.assertEqual(manifest["artifacts"][0]["path"], "native/csharp-report.net8.0.sarif")
        receipt = json.loads((moved / "delivery-receipt.json").read_text())
        self.assertEqual(receipt["manifest"]["sha256"], result["manifest_sha256"])
        self.assertTrue(all((moved / item["path"]).is_file() for item in receipt["files"]))

    def test_missing_sarif_report_is_rejected(self):
        root = self.make_run()
        evidence_root = root / "group-1/evidence-bundle"
        (evidence_root / handoff.CSHARP_SARIF_RELATIVE).unlink()
        manifest_path = evidence_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["artifacts"] = [item for item in manifest["artifacts"]
                                 if item["path"] != handoff.CSHARP_SARIF_RELATIVE]
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "expected exactly one C# SARIF report"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual([item.name for item in self.folder.iterdir() if item.name.startswith(".")], [])

    def test_invalid_sarif_version_and_malformed_sarif_are_rejected(self):
        root = self.make_run()
        sarif = root / "group-1/evidence-bundle" / handoff.CSHARP_SARIF_RELATIVE
        document = json.loads(sarif.read_text())
        document["version"] = "3.0.0"
        _write_json(sarif, document)
        _rebind_evidence_artifact(root, handoff.CSHARP_SARIF_RELATIVE)
        with self.assertRaisesRegex(handoff.HandoffError, "must declare SARIF version 2.1.0"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

        root = self.make_run()
        sarif = root / "group-1/evidence-bundle" / handoff.CSHARP_SARIF_RELATIVE
        sarif.write_bytes(b"{not json")
        _rebind_evidence_artifact(root, handoff.CSHARP_SARIF_RELATIVE)
        with self.assertRaisesRegex(handoff.HandoffError, "not valid UTF-8 JSON"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_sarif_diagnostic_count_must_match_the_scanner_receipt(self):
        finding = {"ruleId": "CA3001", "message": {"text": "synthetic"}}
        root = self.make_run(sarif_results=[finding], diagnostics=0)
        with self.assertRaisesRegex(handoff.HandoffError, "diagnostic count differs"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_git_commit_must_be_a_full_forty_character_sha(self):
        root = self.make_run(commit="abc1234")
        with self.assertRaisesRegex(handoff.HandoffError, "valid captured Git commit"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_source_changed_during_scan_is_rejected(self):
        root = self.make_run(after_status=" M src/Unit-0.cs\n")
        with self.assertRaisesRegex(handoff.HandoffError, "Git baseline changed during scan"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

        root = self.make_run(after_commit="c" * 40)
        with self.assertRaisesRegex(handoff.HandoffError, "Git baseline changed during scan"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_post_scan_git_receipt_is_rejected(self):
        root = self.make_run()
        evidence_root = root / "group-1/evidence-bundle"
        (evidence_root / "01-metadata/after-git-status.txt").unlink()
        manifest_path = evidence_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["artifacts"] = [item for item in manifest["artifacts"]
                                 if item["path"] != "01-metadata/after-git-status.txt"]
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "post-scan Git receipt is missing"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_csharp_receipt_is_rejected(self):
        root = self.make_run()
        evidence_root = root / "group-1/evidence-bundle"
        relative = handoff.CSHARP_ARTIFACT_PREFIX + "command.txt"
        (evidence_root / relative).unlink()
        manifest_path = evidence_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["artifacts"] = [item for item in manifest["artifacts"] if item["path"] != relative]
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "C# evidence receipt is missing: command.txt"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_command_and_version_receipts_must_match_the_scanner_receipt(self):
        root = self.make_run()
        command_path = (root / "group-1/evidence-bundle" / handoff.CSHARP_ARTIFACT_PREFIX / "command.txt")
        command_path.write_text("/tools/dotnet build something-else\n")
        _rebind_evidence_artifact(root, handoff.CSHARP_ARTIFACT_PREFIX + "command.txt")
        with self.assertRaisesRegex(handoff.HandoffError, "command receipt differs"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

        root = self.make_run()
        version_path = root / "group-1/evidence-bundle" / handoff.CSHARP_ARTIFACT_PREFIX / "version.txt"
        version_path.write_text(".NET SDK 9.0.100\n")
        _rebind_evidence_artifact(root, handoff.CSHARP_ARTIFACT_PREFIX + "version.txt")
        with self.assertRaisesRegex(handoff.HandoffError, "version receipt differs"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_reconciliation_csv_must_match_the_compile_units(self):
        root = self.make_run()
        relative = handoff.CSHARP_ARTIFACT_PREFIX + "csharp-reconciliation.csv"
        path = root / "group-1/evidence-bundle" / relative
        path.write_text("tfm,file,status\nnet8.0," + str(self.repo / "src/Other.cs") + ",analyzed\n")
        _rebind_evidence_artifact(root, relative)
        with self.assertRaisesRegex(handoff.HandoffError, "differs from the net8.0 Compile compilation units"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_reconciliation_without_a_target_framework_column_is_rejected(self):
        root = self.make_run()
        relative = handoff.CSHARP_ARTIFACT_PREFIX + "csharp-reconciliation.csv"
        path = root / "group-1/evidence-bundle" / relative
        path.write_text("file,status\n" + str(self.repo / "src/Unit-0.cs") + ",analyzed\n")
        _rebind_evidence_artifact(root, relative)
        with self.assertRaisesRegex(handoff.HandoffError, "tfm,file,status"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_selected_unit_outside_every_target_framework_is_rejected(self):
        root = self.make_run(unit_count=2, compiled={
            "net8.0": [str(self.repo / "src/Unit-0.cs")]})
        with self.assertRaisesRegex(handoff.HandoffError, "not compiled by any target framework"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_each_target_framework_binds_its_own_units(self):
        units = [str(self.repo / "src/Unit-0.cs"), str(self.repo / "src/Unit-1.cs")]
        finding = {"ruleId": "CA2100", "message": {"text": "synthetic"}}
        root = self.make_run(unit_count=2, frameworks=("net8.0", "netstandard2.0"),
                             compiled={"net8.0": units, "netstandard2.0": units[:1]},
                             sarif_results={"net8.0": [finding], "netstandard2.0": []})
        handoff.export_csharp_run(self.profile_path, root, self.output)

        manifest = json.loads((self.output / "manifest.json").read_text())
        targets = manifest["tool_runs"][0]["targets"]
        self.assertEqual([target["target_framework"] for target in targets], ["net8.0", "netstandard2.0"])
        self.assertEqual([target["units_attempted"] for target in targets], [2, 1])
        self.assertEqual([target["units_completed"] for target in targets], [2, 1])
        self.assertEqual([target["analysis_units"] for target in targets], [units, units[:1]])
        self.assertEqual(manifest["components"][0]["analysis_units"], units)
        self.assertEqual(manifest["counts"]["target_frameworks"], ["net8.0", "netstandard2.0"])
        self.assertEqual(manifest["counts"]["units_by_target_framework"],
                         {"net8.0": 2, "netstandard2.0": 1})
        self.assertEqual(manifest["counts"]["compiled_units_by_target_framework"],
                         {"net8.0": 2, "netstandard2.0": 1})
        self.assertEqual(manifest["counts"]["diagnostics_by_target_framework"],
                         {"net8.0": 1, "netstandard2.0": 0})
        self.assertEqual(manifest["counts"]["scanner_reported_diagnostics"],
                         {"dotnet_netanalyzers_native_sarif": 1})
        declared_targets = manifest["tool_runs"][0]["targets"]
        self.assertEqual([item["target_framework"] for item in declared_targets],
                         ["net8.0", "netstandard2.0"])
        # Each framework keeps its own native SARIF artifact bound to its target.
        artifacts_by_framework = {item["target_framework"]: item for item in manifest["artifacts"]}
        self.assertEqual(sorted(artifacts_by_framework), ["net8.0", "netstandard2.0"])
        for framework, item in artifacts_by_framework.items():
            self.assertEqual(item["target"], "net8.0-release-target[" + framework + "]")
            self.assertTrue((self.output / item["path"]).is_file())
        rules = next(item for item in manifest["provenance_inputs"]
                     if item["purpose"] == "rules-configuration")
        rules_value = json.loads((self.output / rules["path"]).read_text())
        self.assertEqual(rules_value["target_frameworks"], ["net8.0", "netstandard2.0"])

    def test_per_framework_sarif_must_match_the_scanned_bytes(self):
        root = self.make_run()
        tampered = (root / "group-1/evidence-bundle" / handoff.CSHARP_ARTIFACT_PREFIX
                    / "per-tfm/csharp-report.net8.0.sarif")
        document = json.loads(tampered.read_text())
        document["runs"][0]["results"] = [{"ruleId": "CA9999", "message": {"text": "injected"}}]
        _write_json(tampered, document)
        relative = tampered.relative_to(root / "group-1/evidence-bundle").as_posix()
        _rebind_evidence_artifact(root, relative)
        with self.assertRaisesRegex(handoff.HandoffError, "differs from the scanned bytes"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_unknown_analyzer_version_is_delivered_with_its_limitation(self):
        limitation = "无法确定 Microsoft.CodeAnalysis.NetAnalyzers 版本"
        root = self.make_run(analyzer_version=None, analyzer_limitations=[limitation])
        handoff.export_csharp_run(self.profile_path, root, self.output)

        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertIsNone(manifest["tool_runs"][0]["tool_version"])
        self.assertIsNone(manifest["artifacts"][0]["tool_version"])
        rules = next(item for item in manifest["provenance_inputs"]
                     if item["purpose"] == "rules-configuration")
        rules_value = json.loads((self.output / rules["path"]).read_text())
        self.assertIsNone(rules_value["tool_version"])
        self.assertEqual(rules_value["limitations"], [limitation])
        self.assertEqual(rules_value["compiler_semantic_version"], "4.11.0")
        self.assertEqual(rules_value["sdk_version"], "8.0.100")

    def test_unknown_analyzer_version_without_a_limitation_is_rejected(self):
        root = self.make_run(analyzer_version=None)
        with self.assertRaisesRegex(handoff.HandoffError, "without a recorded limitation"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_build_configuration_is_binding_or_the_export_fails_closed(self):
        root = self.make_run()
        (self.repo / "src/App.csproj").unlink()
        with self.assertRaisesRegex(handoff.HandoffError, "build configuration path is unavailable"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_solution_project_binds_directory_build_props(self):
        solution = self.repo / "Sample.sln"
        solution.write_text("Microsoft Visual Studio Solution File, Format Version 12.00\n")
        root = self.make_run(project=solution)
        with self.assertRaisesRegex(handoff.HandoffError, "requires a .csproj or Directory.Build.props"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

        props = self.repo / "Directory.Build.props"
        props.write_text("<Project><PropertyGroup><LangVersion>latest</LangVersion></PropertyGroup></Project>\n")
        handoff.export_csharp_run(self.profile_path, root, self.output)
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["source"]["build_configuration_sha256"], _sha(props.read_bytes()))
        build = next(item for item in manifest["provenance_inputs"] if item["purpose"] == "build-configuration")
        self.assertEqual(build["path"], "provenance/build/Directory.Build.props")
        self.assertEqual((self.output / build["path"]).read_bytes(), props.read_bytes())

    def test_three_tool_run_cannot_be_exported_as_a_csharp_delivery(self):
        root = self.make_run()
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["evidence_gate_required"] = True
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "C#-only runner execution"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_each_lane_rejects_the_other_lanes_input(self):
        root = self.make_run()
        with self.assertRaisesRegex(handoff.HandoffError, r"explicit C and/or C\+\+ scope only"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

        manifest = json.loads((root / "manifest.json").read_text())
        cpp_profile = {
            "schema_version": handoff.PROFILE_SCHEMA,
            "product_id": "cpp-product",
            "display_name": "Synthetic C++ profile",
            "repository": "https://example.invalid/org/repository",
            "component_id": "cpp-component",
            "target_id": "cpp-target",
            "configuration": "cpp-config",
            "included_paths": ["src/**"],
            "excluded_paths": [],
            "languages": ["C++"],
            "unsupported_languages": [],
        }
        self.assertEqual(manifest["groups"][0]["mode"], "source")
        _write_json(self.profile_path, cpp_profile)
        with self.assertRaisesRegex(handoff.HandoffError, "three-tool evidence gate"):
            handoff.export_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_cli_export_csharp_prints_the_delivery_receipt(self):
        root = self.make_run()
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scan/handoff.py"), "export-csharp",
             "--profile", str(self.profile_path), "--run", str(root), "--output", str(self.output)],
            text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        receipt = json.loads(completed.stdout)
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(receipt["product_id"], "guardclauses-example")
        self.assertTrue((self.output / "manifest.json").is_file())

    def test_cpp_profile_is_rejected_by_the_csharp_lane(self):
        root = self.make_run()
        profile = json.loads(self.profile_path.read_text())
        profile["languages"] = ["C++"]
        _write_json(self.profile_path, profile)
        with self.assertRaisesRegex(handoff.HandoffError, r"explicit C# scope only"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_failed_csharp_check_is_rejected_with_its_errors(self):
        root = self.make_run()
        gate = json.loads((root / "group-1/evidence-bundle/release-gate.json").read_text())
        gate["checks"][handoff.CSHARP_RUNNER_NAME]["errors"] = ["C# SARIF 报告无效: synthetic"]
        self.rebind_gate(root, gate)
        with self.assertRaisesRegex(handoff.HandoffError, "C# evidence check reports errors: C# SARIF"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_failed_scan_is_not_exportable(self):
        root = self.make_run()
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["ok"] = False
        manifest["exit_code"] = 1
        _write_json(manifest_path, manifest)
        with self.assertRaisesRegex(handoff.HandoffError, "failed or incomplete"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertFalse(self.output.exists())

    def test_existing_output_is_refused_without_mutation(self):
        root = self.make_run()
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("do not overwrite")
        with self.assertRaisesRegex(handoff.HandoffError, "refusing to overwrite"):
            handoff.export_csharp_run(self.profile_path, root, self.output)
        self.assertEqual(sentinel.read_text(), "do not overwrite")


if __name__ == "__main__":
    unittest.main()
