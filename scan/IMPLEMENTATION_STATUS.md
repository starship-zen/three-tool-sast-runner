# Remaining Acceptance Work

Status as of October 22, 2026. Implementation tests and real analyzer executions
are not substitutes for downstream or business-owner acceptance.

## 1. Tool Upgrade: Deferred By Owner

No production tool installation, tool-path selection or version threshold is changed in this
round. LLVM 10 and the Cppcheck development build still fail version evidence
checks. Execution may succeed while the main command returns 3. Do not remove
this check or relabel the installed binaries to make it pass.

## 2. Downstream Schema And Ingestion: Deferred Integration

The reporting consumer is `sast-report-pipeline`; its implementation has been
located during review. The runner does not yet implement its authoritative
`three-tool-input-1.0` contract and continues to export the honestly labeled
`scanner-evidence-1` manifest with `downstream_compatible=false`.

Follow-up integration must pin the actual consumer version and schema/validator,
provision its dependencies, and use a known-good evidence bundle as a reference.
Implement its exact exporter without guessing fields; pin the
generator version; validate relocation and strict real ingestion; inspect all
generated reports including the zero-diagnostic case. Deleted Plists, changed
hashes and inconsistent TU sets must be rejected in negative tests. No extra
public scanner argument or acceptance-bypass switch is introduced.

## 3. Business Product Boundary: Owner Mapping Required

One Git repository is not one business product. Product IDs, build targets,
included/excluded modules and shared-module ownership need owner confirmation.
The internal product policy will be derived from those confirmed mappings, not
from a new user-facing product/checks parameter. CMake targets may inform that
mapping, but cannot establish business ownership by themselves.

Until that mapping exists, preserve useful scan results but do not certify
product isolation. Mixed-product rejection versus independent product bundles
also requires a confirmed policy. No product IDs are invented in this round.

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
