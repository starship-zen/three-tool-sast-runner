# Deployment and Delivery Guide

[ English ](DEPLOYMENT.md) • [ 简体中文 ](DEPLOYMENT.zh-CN.md)

This guide is the operational companion to [SPEC.md](SPEC.md). It covers the two
implemented delivery lanes — C/C++ (Cppcheck, clang-tidy, Clang Static Analyzer)
and C# (Roslyn with `Microsoft.CodeAnalysis.NetAnalyzers`) — and the commands
that turn one scanned commit into a `three-tool-scan-delivery-1.0` directory,
plus the receiving report steps.

Command names, option names, field names and rejection messages below were read
from this implementation (`scan/`, `scan/handoff.py`, `scan/csharp_roslyn/`) and
from the receiving `sast-report-pipeline`. A completed scan, an exported
delivery and a candidate report are three different states; none of them is
release approval.

---

## 1. Prerequisites

Run every check on the machine that will execute the build and the scan. A
check that cannot produce the expected output blocks that lane; do not replace a
missing measurement with a plausible value.

### 1.1 Locked Python environment

| Check | Command | Expected |
| :--- | :--- | :--- |
| Pinned interpreter | `cat .python-version` | `3.12.15` |
| Declared dependency | `grep PyYAML pyproject.toml requirements.txt` | `PyYAML==6.0.3` |
| Locked sync | `uv sync --frozen` | exit 0, no resolution change |
| Actual interpreter and parser | `uv run --frozen python -c "import sys, yaml; print(sys.version.split()[0], yaml.__version__)"` | `3.12.15 6.0.3` |

`pyproject.toml` requires `>=3.12,<3.13`; `uv.lock` fixes the interpreter and
PyYAML version. PyYAML is required for native clang-tidy YAML validation — a
missing parser is an evidence-check failure, not a skipped check.

### 1.2 C/C++ analyzers and the version gate

| Check | Command | Expected |
| :--- | :--- | :--- |
| Binaries present | `bash install/preflight.sh` | four `[OK]` lines, exit 0 |
| Cppcheck release | `cppcheck --version` | `Cppcheck 2.22.0` or newer |
| clang release | `clang --version` | `clang version 18.1.3` (14.0 or newer) |
| clang-tidy release | `clang-tidy --version` | `LLVM version 18.1.3` (14.0 or newer) |

`install/preflight.sh` checks binary availability only; it does not check
`dotnet`, and it is not a release certification.

The gate lives in `scan/evidence.py::check_versions`. It runs for every run and
reads the captured `--version` stdout and stderr of `cppcheck`, `clang-tidy`,
`clang` and `clang++`, matches
`(?:Cppcheck\s+|(?:LLVM|clang)\s+version\s+)(\d+)\.(\d+)`, and requires:

- `cppcheck` at least `2.22`;
- `clang`, `clang++` and `clang-tidy` at least `14.0`;
- no `dev` token anywhere in the banner (case-insensitive). The bundled Cppcheck
  source in `install/cpp_check/` is a development snapshot: it builds, but its
  `dev` banner is rejected. Do not relabel a binary to pass this check.

In the C/C++ lane these errors fail the evidence gate and block export. A C# run
has no C/C++ tool banners at all, so the same file reports them as errors while
`export-csharp` judges the lane by its own check (section 2.2).

`scan/handoff.py` re-validates the same banners before export: it rejects
`dev`/`alpha`/`beta`/`pre`/`rc` text, requires the `cppcheck` token to equal the
`version` attribute of the native Cppcheck XML root, and requires the
`clang`, `clang++` and `clang-tidy` release tokens to be identical. A mixed
installation (for example clang 18 with clang-tidy 14) therefore passes neither
export nor a consistent delivery.

### 1.3 .NET SDK for the C# lane

| Check | Command | Expected |
| :--- | :--- | :--- |
| SDK banner | `dotnet --version` | `8.0.404` in the verified run; any .NET SDK that declares the project's target frameworks |
| Installed SDKs | `dotnet --list-sdks` | at least one `8.0.*` entry |
| Restore available | `dotnet restore <project>` | exit 0 (first run needs NuGet access, or a warmed package cache) |

The scanner runs `dotnet build`, which restores by default. A disconnected host
needs the NuGet packages and the NetAnalyzers package already resolved in the
local cache; otherwise the build fails and the scan is not a completed run.

### 1.4 Clean Git baseline

Both lanes capture the Git state before and after the scan and both exports
require a clean baseline.

| Check | Command | Expected |
| :--- | :--- | :--- |
| Commit identity | `git -C <repo> rev-parse HEAD` | a 40-character hexadecimal commit |
| Working tree | `git -C <repo> status --porcelain` | no output |
| Tracked changes | `git -C <repo> diff HEAD --binary` | no output |

`01-metadata/code-state.txt` records `repo`, `branch`, `commit`, `date`,
`dirty` and the porcelain status. The evidence gate fails when the commit is not
40 hexadecimal characters, and fails again when the after-scan `commit`,
`status` or `diff` receipt differs from the before-scan one. Build outputs
inside the repository must therefore be ignored by Git; a scan that writes
tracked files, or leaves untracked files, cannot be exported as a complete
delivery.

---

## 2. Command sequences

Paths below are operator placeholders. Use one result root per product; never
reuse a result directory between products.

### 2.1 C/C++ lane

```sh
# 1. Scan: needs a compilation database for every scan path.
uv run --frozen bash scan/scan_all.sh \
  --scan-path /srv/product/src \
  --result-path /srv/scan-results/product-a

# 2. Export one evidence-gated product group.
uv run --frozen python scan/handoff.py export \
  --profile /etc/sast/product-a-profile.json \
  --run /srv/scan-results/product-a/<run_id> \
  --output /srv/deliveries/product-a-<run_id>

# 3-5. Receiving report steps: see 2.3.
```

| Step | Input | Output | Success | Common failures |
| :--- | :--- | :--- | :--- | :--- |
| `scan/scan_all.sh` | scan paths; `compile_commands.json`, `build/compile_commands.json` or `build/Debug/compile_commands.json` in an ancestor of each path | `/srv/scan-results/product-a/<run_id>/` with `manifest.json`, `group-1/evidence-bundle/`, per-tool HTML | exit `0`, `ok=true`, `evidence_checks_ok=true` | exit `1` execution or report failure, `2` input error, `3` execution succeeded but the evidence gate failed, `130` cancelled |
| `handoff.py export` | the completed run plus a product profile | a new directory with `manifest.json`, `delivery-receipt.json`, `native/`, `provenance/` | exit `0` and a `{"status":"delivered",...}` receipt on stdout | exit `2` with `error: <reason>` on stderr |

Exit `3` is the expected outcome of a version-gate or baseline-gate failure: the
scan executed, the evidence is not exportable. The gate detail is in
`group-1/evidence-bundle/release-gate.json`; the export in step 2 refuses the run
until the gate reports `ok: true`.

### 2.2 C# lane

```sh
# 1. Scan: no compilation database is needed, but the project must be restorable.
uv run --frozen bash scan/csharp_roslyn/scan.sh \
  --scan-path /srv/product/src \
  --result-path /srv/scan-results/product-b

# 2. Export the C#-only run.
uv run --frozen python scan/handoff.py export-csharp \
  --profile /etc/sast/product-b-profile.json \
  --run /srv/scan-results/product-b/<run_id> \
  --output /srv/deliveries/product-b-<run_id>
```

| Step | Input | Output | Success | Common failures |
| :--- | :--- | :--- | :--- | :--- |
| `scan/csharp_roslyn/scan.sh` | scan paths containing exactly one `.sln`/`.csproj`; restorable packages | `<run_id>/group-1/csharp_roslyn/` with `per-tfm/*.sarif`, `csharp-report.sarif`, `csharp-reconciliation.csv`, `receipt.json`; `<run_id>/group-1/evidence-bundle/` | exit `0` and `receipt.ok=true` | exit `2` input error (no project file, several projects, unsafe target framework), `1` build failure or an uncompiled selected source file, `130` cancelled |
| `handoff.py export-csharp` | the completed C# run plus a C# product profile | the same delivery layout, with `dotnet-netanalyzers` as the only tool | exit `0` and a `{"status":"delivered",...}` receipt | exit `2` with `error: <reason>` |

The C# lane never sets `evidence_gate_required`, so it exits `0` on a completed
scan and `3` is not used. `release-gate.json` still lists the C/C++-family gate
errors (`Missing tool version: cppcheck`, `All three tools are required for
certification`, `Source fallback is not a three-tool compilation-database
bundle`, `Compiler dependency baseline unavailable or incomplete`) and keeps
`ok: false`. `export-csharp` accepts such a run when — and only when — the single
`csharp_roslyn` entry inside `checks` reports no errors and reconciles exactly
the selected source units. A failing `ok` on that file is therefore not by
itself an export blocker for this lane; a failing `checks.csharp_roslyn.errors`
is.

### 2.3 Receiving report steps

The receiver is the standalone `sast-report-pipeline` checkout. It needs the
delivered directory, not this repository, the original run or the source
checkout.

```sh
# Prepare canonical three-tool-input-1.0 input and bind local assets.
uv run --frozen --offline python scripts/sast_scan.py prepare-runner \
  --delivery /srv/deliveries/product-a-<run_id> \
  --product product-a \
  --output-dir /srv/report-inputs/product-a-<run_id>

# Optional strict mapping audit before generation.
uv run --frozen --offline python scripts/sast_mapping.py audit \
  --manifest /srv/report-inputs/product-a-<run_id>/manifest.json \
  --product product-a

# Generate and candidate-verify the package.
uv run --frozen --offline python scripts/three_tool_report.py generate \
  --manifest /srv/report-inputs/product-a-<run_id>/manifest.json \
  --product product-a --mapping-mode strict \
  --output /srv/reports/product-a-<run_id>

# Re-verify the finished package.
uv run --frozen --offline python scripts/three_tool_report.py verify \
  --run /srv/reports/product-a-<run_id> --level candidate
```

| Step | Input | Output | Success | Common failures |
| :--- | :--- | :--- | :--- | :--- |
| `prepare-runner` | the delivery directory and the same `product_id` the delivery declares | new directory with canonical `manifest.json`, copied `assets/`, `provenance/runner-delivery/` | exit `0`, `status: prepared`, `execution_authenticated: false` | exit `2` with `error.code` such as `invalid_runner_delivery`, `product_mismatch`, `scope_mismatch` |
| `generate` | prepared manifest | new report package | exit `0` (`candidate`) or `10` (`candidate-pending`), `verification.status: verified` | exit `2` (mapping gap, invalid input, verification failure) |
| `verify --level candidate` | report package | verification JSON | exit `0`, `status: verified`, `level: candidate` | exit `2` |

`prepare-runner` also succeeds for a delivery whose observed rules are not yet
mapped; the strict audit and strict generation then fail closed. Exit `10` is a
completed candidate package with pending review items, not a tool failure.

---

## 3. Product profile

The profile is operator-owned and is the only place product scope is declared.
Copy [`examples/runner-handoff/product-profile.example.json`](../examples/runner-handoff/product-profile.example.json)
and complete it. Both exporters require exactly these eleven fields — an extra
or missing field is rejected.

| Field | Constraint |
| :--- | :--- |
| `schema_version` | `three-tool-product-profile-1.0` |
| `product_id` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`; must equal the `--product` used by the receiver |
| `display_name` | non-empty text |
| `repository` | non-empty text; a URL userinfo part (`https://user@host/...`) is rejected |
| `component_id`, `target_id`, `configuration` | same identifier pattern as `product_id` |
| `included_paths` | non-empty array of repository-relative globs; no absolute path, no `..`, no backslash, no duplicates |
| `excluded_paths` | array of the same kind; may be empty |
| `languages` | non-empty subset of the lane: `C`, `C++` for C/C++, `C#` for C# |
| `unsupported_languages` | explicit string array, may be empty |

Scope checking works on repository-relative paths, not on the scan path. The
captured `git rev-parse --show-toplevel` is the base, so a product scanned from
`/srv/product/src` inside a repository rooted at `/srv/product` needs
`src/**`, and a scan of `examples/cpp` inside this repository needs
`examples/cpp/**`. Patterns are matched with `fnmatch`, so `*` also crosses
`/`; they are not Git ignore rules and `!` negation does not exist.

`included_paths` and `excluded_paths` must not overlap in effect. Each selected
unit is first required to match an include pattern, then rejected if it matches
any exclude pattern:

```text
error: selected source falls outside the explicit product-profile include scope: src/generated/Codes.cs
error: selected source also matches an explicit product-profile exclusion: src/generated/Codes.cs
```

A file inside the overlap is therefore fatal for that scope. Narrow the include
to the code that really belongs to the product; do not delete the exclude rule
to make the export pass, which would pull generated or vendored code into the
declared product scope. The receiver repeats the same per-unit check and reports
`scope_mismatch`.

The profile also decides the lane: `languages: ["C#"]` is only accepted by
`export-csharp`, `languages: ["C"]`/`["C++"]` only by `export`. A profile that
declares the other family fails with `this delivery lane supports explicit C#
scope only` (C# lane) or `this delivery lane supports explicit C and/or C++
scope only` (C/C++ lane).

---

## 4. Multi-target-framework semantics (C#)

A .NET project may declare several target frameworks. The scanner builds each of
them separately and the delivery keeps them apart.

- `target_id` in the profile is the product build target. The delivered target
  identity is `target_id + "[" + target_framework + "]"`, for example
  `product-b-service-core[net8.0]`. Every entry of `tool_runs[].targets[].target`
  and of `artifacts[].target` uses that form; two frameworks with one shared
  identity would make the targets indistinguishable.
- Each framework gets one isolated build, with its own
  `units_attempted`/`units_completed` and its own native
  `per-tfm/csharp-report.<tfm>.sarif`:

  ```sh
  dotnet build <project> \
    -p:RunAnalyzersDuringBuild=true -p:EnableNETAnalyzers=true \
    -p:AnalysisLevel=latest -p:AnalysisModeSecurity=AllEnabledByDefault \
    -p:TargetFramework=<tfm> \
    -p:ErrorLog=<absolute path>.sarif%2Cversion=2.1 \
    --no-incremental
  ```
- The merged `csharp-report.sarif` is written for human reading and is delivered
  only as `provenance/native-merged/csharp-report.sarif`; each artifact is the
  per-framework compiler output, byte for byte.
- The scanner aborts when the project declares no `TargetFramework` (a solution
  file alone is not enough) or more than 32 frameworks, and when a declared
  framework identifier is unsafe for file naming.

### 4.1 Counters

`counts` in the delivery manifest carries:

| Key | Meaning |
| :--- | :--- |
| `target_frameworks` | the frameworks that were actually built |
| `units_by_target_framework` | product analysis units compiled under that framework (selected units ∩ that framework's Compile items) |
| `compiled_units_by_target_framework` | every Compile item of that framework, including sources generated outside the product |
| `diagnostics_by_target_framework` | diagnostics in that framework's native SARIF |
| `selected_analysis_units` | size of the delivered unit inventory |
| `artifact_count_by_tool` | native artifacts per tool |
| `scanner_reported_diagnostics.dotnet_netanalyzers_native_sarif` | total diagnostics across the per-framework SARIF files |

`units_by_target_framework` can be smaller than
`compiled_units_by_target_framework`. A package can contribute source that the
project compiles, for example the `NullableAttributes.cs` that several NuGet
packages add under `obj/` for older frameworks. That file is a real Compile item
of that framework, so it counts in `compiled_units` (and in that framework's
`units_attempted`/`units_completed`), while it is not an analysis unit of the
product and never appears in `analysis_units`.

The unit inventory itself comes from MSBuild, not from the directory:

```sh
dotnet build <project> -getItem:Compile -p:TargetFramework=<tfm> -nologo -v:q
```

`-getItem:Compile` reports the evaluated Compile items of that framework, so
`units_*` are neither the count of `.cs` files in a directory nor a filtered
directory listing. Paths containing `bin` or `obj` are excluded from the scan
side by `IGNORED_PATH_PARTS`, and generated build output therefore never becomes
a selected unit. A failed `-getItem` query is fail-closed: the framework reports
zero attempted and zero completed units.

A selected `.cs` file that is not a Compile item of any framework fails the
scan, with the file named in the receipt:

```text
以下源文件不是项目任何 TargetFramework 的 Compile 编译单元，未被分析: src/Legacy/Old.cs
```

The same condition blocks the export (`C# selected source units are not compiled
by any target framework: ...`). Remove such files from the scan path or exclude
them from the profile scope; they are not analysed just because they sit in the
directory.

### 4.2 Known limitation: single-framework deliveries

`export-csharp` always writes `target_frameworks`,
`units_by_target_framework`, `compiled_units_by_target_framework` and
`diagnostics_by_target_framework`. The current receiver requires those four keys
only for a delivery that declares more than one framework, and rejects them for a
single-framework delivery: exit `2`, `error.code: invalid_runner_delivery`,
message `delivery counts does not match the delivery contract`.

Until that code is reconciled, only multi-target-framework C# deliveries pass
`prepare-runner`. This is a contract mismatch between the two repositories, not
a scanner or profile problem; do not work around it by editing a delivery.

---

## 5. Version identity

Three different versions describe a C# run and none of them may stand in for
another.

| Value | Where it is captured | What it means |
| :--- | :--- | :--- |
| `tool_version` | `tool_runs[].tool_version`, `artifacts[].tool_version`, `provenance/rules/dotnet-netanalyzers.json` | the `Microsoft.CodeAnalysis.NetAnalyzers` package release that produced the CA diagnostics; `8.0.0` in the verified run |
| `dotnet.sdk_version` | `provenance/tool-versions/binary-identities.json`, `dotnet.version.stdout.txt` | the `dotnet --version` banner of the SDK that ran the build; `8.0.404` in the verified run |
| `dotnet.compiler.semantic_version` / `compiler.versions` | same file, from the SARIF `tool.driver` block | the C# compiler that emitted the SARIF; `4.11.0` / `4.11.0-3.24510.3 (cfe3bd0a)` in the verified run |

The analyzer version is resolved from the project's `project.assets.json`
(`tool_version_source: project.assets.json`). If restore output is unavailable
it falls back to the SDK property `_NETAnalyzersSDKAssemblyVersion`
(`tool_version_source: dotnet-sdk`). If neither can be read, `tool_version`
stays `null` with an explicit entry in `limitations`; the receiver then rejects
the delivery with `invalid_tool_version`, so an unresolved analyzer identity is
not a shippable delivery.

The SARIF driver is the C# compiler, never the analyzer: `driver.version`,
`semanticVersion` and `dottedQuadFileVersion` are compiler provenance. Reporting
the compiler version as the analyzer version would silently mislabel every
finding.

For the C/C++ lane the release tokens are `cppcheck`, `clang-tidy` and
`clang`/`clang++` as captured above, with `clang-static-analyzer` bound to the
clang token and the native plist `clang_version` strings preserved in
`provenance/tool-versions/csa-native-version-strings.json`.

---

## 6. MSBuild `ErrorLog` encoding

The SARIF switch must be written with a percent-encoded comma:

```sh
-p:ErrorLog=/srv/scan-results/run/group-1/csharp_roslyn/build.net8.0.sarif%2Cversion=2.1
```

MSBuild splits `-p:` values on commas, so the documented compiler form
`ErrorLog=<file>,version=2.1` reaches `csc` without `version=2.1` and produces
legacy SARIF **1.0.0**. Symptoms of that mistake:

- `scan/csharp_roslyn/reporting.py::read_runs` raises
  `SARIF 报告必须为 2.1.0 版本且包含非空 runs 数组`;
- the build receipt records `error` text and the run is not `ok`;
- an exported or hand-edited file fails export with
  `error: C# SARIF report must declare SARIF version 2.1.0`, and the receiver
  rejects it as native evidence (`unsupported SARIF root or version`, surfaced by
  `prepare-runner` as `invalid_runner_delivery`).

Keep `%2C` in any script that reproduces the scanner command. The verified form
is `-p:ErrorLog=<absolute path>%2Cversion=2.1` with .NET SDK 8.0.404.

---

## 7. Evidence layers

Every artifact row in `group-1/evidence-bundle/manifest.json` carries a `layer`
value assigned by `scan/evidence.py::artifact_layer`.

| Layer | Contents | Downstream role |
| :--- | :--- | :--- |
| `[RAW]` | `03-cppcheck/result/cppcheck-compile-report.xml`, `04-clang-tidy/result/fixes/*.yaml`, `05-clang-static-analyzer/result/csa-native/*.plist`, `06-csharp-roslyn/**/*.sarif` | native scanner bytes; the only ground truth for diagnostics |
| `[RECEIPT]` | execution receipts, `tu-mapping.csv`, `csharp-reconciliation.csv`, `code-state.txt`, `source-hashes.json`, version banners, scanner and binary hashes | proves which units ran, how, and against which commit |
| `[CACHE]` | paths containing `cppcheck-build` | local Cppcheck analysis cache; never a report input |
| `[DERIVED]` | `.html`, `.css`, `.md` views, `summary.json`, `severity.csv`, `release-gate.json` | human reading aids and the gate verdict |

Only `[RAW]` and `[RECEIPT]` evidence is externally shippable, and the delivery
is what carries it: native bytes under `native/`, and the copied JSON/CSV/TXT
receipts (including `release-gate.json`) under `provenance/`. HTML/CSS/Markdown
reading views and the Cppcheck cache are never copied into a delivery.

Before transferring a delivery, apply the product owner's data-handling policy:
raw provenance can contain absolute build and source paths, and native outputs
(CSA plists, clang-tidy YAML, SARIF) can contain source excerpts.

---

## 8. Troubleshooting

| Symptom | Cause | Where to look | Fix |
| :--- | :--- | :--- | :--- |
| `error: product delivery requires a clean captured Git baseline` | scan or export saw a non-empty `status`/`diff`, or a commit that is not 40 hexadecimal characters | `01-metadata/code-state.txt`, `before-git-status.txt`, `before-git-diff.txt`; `scan/handoff.py::_captured_git_baseline` | commit the source state, ignore build output, rescan |
| `error: Git baseline changed during scan` in `release-gate.json` | tracked or untracked files appeared or changed while the tools ran | `before-*`/`after-*` Git receipts; `scan/evidence.py::finish` | stop writing into the work tree during a scan; rescan |
| `error: Unsupported or unverified tool version: cppcheck: ...` or `... clang-tidy: ... dev ...` | Cppcheck older than 2.22, LLVM older than 14, or a development build | `release-gate.json`; `01-metadata/cppcheck.version.*`, `clang-tidy.version.*` | install a release build and rescan; do not rename binaries |
| `error: clang, clang++ and clang-tidy captured release tokens must agree` | mixed LLVM installations on `PATH` | `provenance/tool-versions/*` of the attempted export | install one LLVM release for all three and rescan |
| `error: C# SARIF report must declare SARIF version 2.1.0` | `%2C` missing from `-p:ErrorLog`, so `version=2.1` was dropped | `group-1/csharp_roslyn/build.<tfm>.stderr.log`, `receipt.json.builds[].sarif` | restore `%2Cversion=2.1` and rescan; do not patch the SARIF |
| scan exits `1` with `以下源文件不是项目任何 TargetFramework 的 Compile 编译单元` | selected `.cs` file is not compiled by any target framework | `csharp-reconciliation.csv` rows with `status: discovered` | remove the file from the scan path or exclude it in the profile |
| `error: selected source falls outside the explicit product-profile include scope: <path>` | include globs are relative to the Git top level and too narrow | `components[].relative_source_paths` of the delivery, `git rev-parse --show-toplevel` | widen the include to the real product subtree (for example `src/**`, `examples/cpp/**`) |
| `error: selected source also matches an explicit product-profile exclusion: <path>` | a selected unit matches both an include and an exclude pattern | the profile's `included_paths`/`excluded_paths` | narrow the include; do not delete the exclude and pull generated code into scope |
| `error: this delivery lane supports explicit C# scope only` (or the C/C++ wording) | profile `languages` belongs to the other lane | the profile and the chosen subcommand | use `export-csharp` with `["C#"]`, or `export` with `["C"]`/`["C++"]` |
| export exits `2` with `failed or incomplete scanner execution cannot be exported as a complete delivery` | the runner `manifest.json` records `ok: false`, a non-zero exit, a cancellation or a report error | `<run_id>/manifest.json`, the per-tool `receipt.json` | fix the tool or build failure and rescan |
| `error: the C# evidence check reports errors: ...` | the C# gate does not reconcile exactly the selected source units | `release-gate.json` → `checks.csharp_roslyn` | confirm the run is C#-only and was not modified after scanning |
| `release-gate.json` has `ok: false` on a C# run, yet `export-csharp` succeeds | expected: the C/C++-family gate reasons stay in the file | `release-gate.json` `errors` list | none; only `checks.csharp_roslyn.errors` blocks this lane |
| receiver: exit `2`, `error.code: invalid_runner_delivery`, message `delivery counts does not match the delivery contract` (C# delivery) | single target framework, whose counters the receiver does not accept yet | the delivery `manifest.json` `counts` and `counts.target_frameworks` | known limitation in section 4.2; needs a code fix in the receiver |
| receiver: `invalid_tool_version` | `tool_version` is not a numeric release (analyzer version unresolved) | `provenance/tool-versions/binary-identities.json`, `rules/dotnet-netanalyzers.json` `limitations` | restore/clean the project so `project.assets.json` resolves the package, rescan, re-export |
| export exits `2` with `error: output already exists; refusing to overwrite` | the output directory exists | the `--output` argument | choose a new directory; deliveries are never overwritten |
