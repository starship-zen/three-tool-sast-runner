# Remaining Acceptance Work

Status as of October 22, 2026. Implementation tests and real analyzer executions
are not substitutes for downstream or business-owner acceptance.

## 1. Tool Upgrade: Deferred By Owner

No production tool installation, tool-path selection or version threshold is changed in this
round. LLVM 10 and the Cppcheck development build still fail version evidence
checks. Execution may succeed while the main command returns 3. Do not remove
this check or relabel the installed binaries to make it pass.

## 2. Product-Scoped Evidence Delivery: Public C++ Sample Accepted

The runner continues to emit its execution evidence as
`scanner-evidence-1`, with `downstream_compatible=false` and
`release_ready=false`. `scan/handoff.py export` now copies one completed,
evidence-gated product scope into a portable `three-tool-scan-delivery-1.0`
directory. Its exact profile and command are documented in
`examples/runner-handoff/README.md`.

The delivery preserves native outputs byte-for-byte and hashes its manifest,
native files and copied build/source/version/scanner receipts. Its profile
contains only owner-declared product, repository, component, target,
configuration, path and language scope. It does not carry reporter paths,
standards, mappings or report orchestration. The receiving
`sast-report-pipeline` owns validation/adaptation into canonical
`three-tool-input-1.0`, binding its own frozen taxonomy assets and applying the
strict mapping/report gates. A delivery is not itself a canonical report
input, proof of authenticated execution, vulnerability confirmation or release
approval.

Synthetic producer tests cover positive/zero analyzer output, zero/mixed
clang-tidy YAML, failed evidence gates, multi-group refusal, source-scope
refusal, prerelease/native version conflicts, tamper rejection, relocation and
no-overwrite behavior. A separate qualified Ubuntu 24.04 / LLVM 18.1.3 /
Cppcheck 2.22.0 public C++ sample completed both selected units. Its unchanged
native evidence passed standalone export, relocation, receiver preparation,
strict mapping audit, candidate verification, packaging and fresh HTTP import.
The original source path was unavailable during receiving acceptance; no
scanner was rerun. This establishes that sample's handoff, not deployment or
business-product acceptance. Report release remains blocked on human decisions.

## CSA single-pass output

Each selected TU now uses one native `plist-html` analyzer invocation instead
of a full plist analysis followed by another full HTML analysis. Per-TU output
directories isolate native plist and sibling HTML, and one actual command
receipt binds the plist path. The standard evidence snapshot still exports
one unchanged plist per TU under `csa-native/`; the downstream delivery format
is unchanged. Valid zero-diagnostic plist may have no HTML; findings require
nonempty regular HTML reports. Failed exit, invalid/non-plist output or missing
positive HTML remains an attributable failed TU, not a successful scan.

The locked Python suite passes 157 tests, including eight single-pass
regressions and exporter root/scope regressions. Native LLVM 18.1.3 probes
cover zero, positive and cross-file findings. The two-TU public sample completed
in 84.344 seconds, with one CSA invocation per TU. Earlier duplicate-pass runs
were capped partials, so no percentage speedup is claimed. The reporter's
locked 235-test run includes the actual exported sample without an opt-in skip.

## 3. Business Product Boundary: Explicit Owner Scope Required

One Git repository is not one business product. Each product profile must be
provided by its owner with an approved product ID, component, target,
configuration, included/excluded paths and language scope. The first exporter
lane accepts one component/target/configuration and verifies that every
selected unit falls within the declared path/language scope. It rejects
multiple compile-database groups and does not infer components or shared-module
ownership from CMake target names. Profile validation is not independent proof
of business ownership; operators must resolve and document scope before
exporting, and must create separate deliveries per product.

## 4. Cross-Run Cache: Implemented For Qualified Main Runs

Cppcheck uses a persistent native build cache with content-based namespacing,
integrity verification, serialized publication and independent per-run copies.
Cold/hot equivalence, corruption recovery, dependency changes, tool/policy
changes, interrupted/failed publication and concurrent access are tested.
Shared-cache setup/publication failures are isolated from native scan success;
warnings remain in execution receipts. Before/after dependency-capture exceptions
and dependency-evidence copy failures preserve tool execution/export while
failing the evidence check. These paths have regression coverage and real remote
analyzer runs with injected failures, not just helper-only tests.

This does not skip native analyzer execution and is not extended to Tidy/CSA.
Without a verified input closure, Cppcheck keeps its existing private cache.
Historical namespaces are retained; do not delete active locks/caches during a
scan. Cache retention/eviction policy is outside this change.

## 5. Dependency Baseline: Auditable Closure Implemented; Full Reproduction Pending

Selected compile-context sources and headers, including system, external,
generated and untracked headers actually discovered by Clang, are content-hashed
and snapshotted. Applicable Clang configuration files, symlink resolution and
effective environment are recorded. Collection is repeated after all analyzers;
changed dependency content or closure fails the evidence check. Analyzer binary
identity is checked again after scanning. Cppcheck cache keys additionally cover
installed Cppcheck configuration/data files.

This is stronger than selected-source hashes, but is not a complete immutable
build environment. Cppcheck's alternative preprocessor configurations, arbitrary
plugins/modules, all runtime shared libraries and an entire reconstructible SDK
are not covered. Before/after comparison cannot detect a transient edit restored
during scanning. Formal offline reproduction still requires controlled read-only
inputs plus pinned toolchain/build resources and validation on a clean host.

`release_ready` remains false. The release gate lists these pending dependencies
explicitly; a passing unit suite, a cache hit or exit code 0 is not certification.

## 6. Review Repairs: Isolated C/C++ Acceptance

The October 22 review follow-up removes direct/forwarded `-save-temps` options
that could overwrite existing preprocessed files, rejects external Clang
`--config*` options rather than using unbound compiler semantics, and handles
SIGTERM through scanner process-group cleanup with cancellation receipts
(exit 143; SIGINT remains 130). Operators must expand approved external config
arguments into `compile_commands.json`. This is not a general sandbox for
arbitrary compiler plugins or untrusted source builds.

Tidy handoff artifacts now optionally bind original stdout/stderr logs for
downstream coordinate corroboration. Native YAML is unchanged; no log means
no promise of recovered line/column. The receiver independently reconciles
declared/native counts and retains relative source labels; reporter mapping
and release policy stay outside this runner.

The repaired working tree passed 166 tests in isolated Ubuntu 24.04 ARM64
with LLVM 18.1.3 and Cppcheck 2.22.0. All source scans used network isolation
and no host mounts. Positive and zero cases ran/exported successfully;
missing-header and external-config cases failed and refused export. Real
three-tool save-temps probes preserved pre-existing `.i/.ii` files. Signals
sent while native clang-tidy was active produced 143/130 cancellation receipts,
no active analyzer-group survivors and export refusal.

The matching reporter working tree ran 368 tests (one optional environment
skip) and separately accepted the relocated native positive/zero deliveries.
The positive sample contained ten native records, nine code observations and
one auxiliary record, with zero mapping gaps. Both samples passed strict
audit, generation, candidate verification and packing; the positive ZIP also
passed a fresh temporary HTTP import/job. Neither received release approval.

Local evidence: `/tmp/runner-fix-native-20261022-JLsjo3/NATIVE-VALIDATION-RECEIPT.txt`
and `/private/tmp/sast-review-integrated-20261022.hf_jf2yw/review-acceptance.json`.
These are ephemeral verification artifacts, not committed customer deliveries.
This slice did not rerun business or C# sources, test other architectures/tool
versions, inspect PDF layout, install on the dedicated host or restart services.
