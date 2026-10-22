# 部署与交付操作指南

[ English ](DEPLOYMENT.md) • [ 简体中文 ](DEPLOYMENT.zh-CN.md)

本文件是 [SPEC.zh-CN.md](SPEC.zh-CN.md) 的运维配套说明，覆盖两条已实现的交付通道
（C/C++：Cppcheck、clang-tidy、Clang Static Analyzer；C#：Roslyn 配合
`Microsoft.CodeAnalysis.NetAnalyzers`），以及把一次已扫描的提交变成
`three-tool-scan-delivery-1.0` 目录并交给报告消费方的完整命令。

本文的命令名、参数、字段名和拒绝信息均取自本仓库实现（`scan/`、`scan/handoff.py`、
`scan/csharp_roslyn/`）以及接收方 `sast-report-pipeline`。扫描完成、交付导出和候选报告
是三个不同状态，都不等于发布批准。

---

## 1. 前置条件

所有检查都在实际执行构建和扫描的机器上运行。取不到期望输出时，该通道即被阻断；
不得用推测值替代缺失的实测值。

### 1.1 锁定的 Python 环境

| 检查项 | 命令 | 期望输出 |
| :--- | :--- | :--- |
| 固定解释器 | `cat .python-version` | `3.12.15` |
| 声明依赖 | `grep PyYAML pyproject.toml requirements.txt` | `PyYAML==6.0.3` |
| 锁定同步 | `uv sync --frozen` | 退出 0，不改变依赖解析 |
| 实际解释器与解析器 | `uv run --frozen python -c "import sys, yaml; print(sys.version.split()[0], yaml.__version__)"` | `3.12.15 6.0.3` |

`pyproject.toml` 限定 `>=3.12,<3.13`，`uv.lock` 固定解释器与 PyYAML 版本。PyYAML 用于
clang-tidy 原生 YAML 校验；缺少解析器属于证据检查失败，不能记为跳过检查。

### 1.2 C/C++ 分析器与版本门禁

| 检查项 | 命令 | 期望输出 |
| :--- | :--- | :--- |
| 二进制存在 | `bash install/preflight.sh` | 四行 `[OK]`，退出 0 |
| Cppcheck 版本 | `cppcheck --version` | `Cppcheck 2.22.0` 或更高 |
| clang 版本 | `clang --version` | `clang version 18.1.3`（不低于 14.0） |
| clang-tidy 版本 | `clang-tidy --version` | `LLVM version 18.1.3`（不低于 14.0） |

`install/preflight.sh` 只检查二进制可用性，不检查 `dotnet`，也不是发布认证。

版本门禁位于 `scan/evidence.py::check_versions`。每次运行都会执行，读取
`cppcheck`、`clang-tidy`、`clang`、`clang++` 被捕获的 `--version` stdout 与 stderr，
匹配 `(?:Cppcheck\s+|(?:LLVM|clang)\s+version\s+)(\d+)\.(\d+)`，并要求：

- `cppcheck` 不低于 `2.22`；
- `clang`、`clang++`、`clang-tidy` 不低于 `14.0`；
- 版本横幅中不得出现独立的 `dev` 字样（不区分大小写）。`install/cpp_check/` 内置的
  Cppcheck 源码是开发快照，可以编译，但其 `dev` 横幅会被拒绝；不得改名绕过。

在 C/C++ 通道，这些错误会使证据门禁失败并阻断导出。C# 运行根本没有 C/C++ 工具横幅，
同一文件会把它们记为错误，而 `export-csharp` 以本通道自己的检查为准（见 2.2）。

`scan/handoff.py` 在导出前会再次校验同批横幅：拒绝 `dev`/`alpha`/`beta`/`pre`/`rc`
字样，要求 `cppcheck` 的版本 token 与原生 Cppcheck XML 根节点 `version` 属性一致，
并要求 `clang`、`clang++`、`clang-tidy` 的版本 token 完全相同。混装 LLVM（例如
clang 18 搭配 clang-tidy 14）因而既通不过导出，也无法形成一致的交付。

### 1.3 C# 通道的 .NET SDK

| 检查项 | 命令 | 期望输出 |
| :--- | :--- | :--- |
| SDK 横幅 | `dotnet --version` | 实测为 `8.0.404`；也可是能声明项目目标框架的其他 .NET SDK |
| 已装 SDK | `dotnet --list-sdks` | 至少一条 `8.0.*` |
| 还原可用 | `dotnet restore <project>` | 退出 0（首次需要 NuGet 访问，或已有包缓存） |

扫描器执行 `dotnet build`，默认包含还原。断网主机必须已解析好 NuGet 包与
NetAnalyzers 包缓存，否则构建失败，本次运行不算完成的扫描。

### 1.4 干净的 Git 基线

两条通道都会在扫描前后各捕获一次 Git 状态，两个导出通道都要求基线干净。

| 检查项 | 命令 | 期望输出 |
| :--- | :--- | :--- |
| 提交标识 | `git -C <repo> rev-parse HEAD` | 40 位十六进制提交号 |
| 工作树状态 | `git -C <repo> status --porcelain` | 无输出 |
| 已跟踪改动 | `git -C <repo> diff HEAD --binary` | 无输出 |

`01-metadata/code-state.txt` 记录 `repo`、`branch`、`commit`、`date`、`dirty` 与
porcelain 状态。提交号不是 40 位十六进制时证据门禁失败；扫描后的 `commit`、
`status` 或 `diff` 收据与扫描前不一致时同样失败。因此仓库内的构建产物必须被 Git 忽略；
扫描过程写入已跟踪文件，或留下未跟踪文件，都无法导出为完整交付。

---

## 2. 命令序列

下列路径为运维占位值。每个产品使用独立的结果根目录，不在产品之间复用结果目录。

### 2.1 C/C++ 通道

```sh
# 1. 扫描：每个扫描路径都需要编译数据库。
uv run --frozen bash scan/scan_all.sh \
  --scan-path /srv/product/src \
  --result-path /srv/scan-results/product-a

# 2. 导出一个通过证据门禁的产品组。
uv run --frozen python scan/handoff.py export \
  --profile /etc/sast/product-a-profile.json \
  --run /srv/scan-results/product-a/<run_id> \
  --output /srv/deliveries/product-a-<run_id>

# 3-5. 接收端报告步骤见 2.3。
```

| 步骤 | 输入 | 输出 | 成功判据 | 常见失败码 |
| :--- | :--- | :--- | :--- | :--- |
| `scan/scan_all.sh` | 扫描路径；其祖先目录中的 `compile_commands.json`、`build/compile_commands.json` 或 `build/Debug/compile_commands.json` | `/srv/scan-results/product-a/<run_id>/`，含 `manifest.json`、`group-1/evidence-bundle/`、逐工具 HTML | 退出 `0`，`ok=true`，`evidence_checks_ok=true` | `1` 执行或报告失败，`2` 输入错误，`3` 执行成功但证据门禁失败，`130` 取消 |
| `handoff.py export` | 完成的运行目录加产品 profile | 新目录，含 `manifest.json`、`delivery-receipt.json`、`native/`、`provenance/` | 退出 `0`，stdout 输出 `{"status":"delivered",...}` 收据 | 退出 `2`，stderr 输出 `error: <原因>` |

退出 `3` 是版本门禁或基线门禁失败的正常结果：扫描确实执行了，证据不可导出。门禁细节在
`group-1/evidence-bundle/release-gate.json`；在第 2 步导出之前，该文件必须为 `ok: true`。

### 2.2 C# 通道

```sh
# 1. 扫描：不需要编译数据库，但项目必须可还原。
uv run --frozen bash scan/csharp_roslyn/scan.sh \
  --scan-path /srv/product/src \
  --result-path /srv/scan-results/product-b

# 2. 导出纯 C# 运行。
uv run --frozen python scan/handoff.py export-csharp \
  --profile /etc/sast/product-b-profile.json \
  --run /srv/scan-results/product-b/<run_id> \
  --output /srv/deliveries/product-b-<run_id>
```

| 步骤 | 输入 | 输出 | 成功判据 | 常见失败码 |
| :--- | :--- | :--- | :--- | :--- |
| `scan/csharp_roslyn/scan.sh` | 恰好包含一个 `.sln`/`.csproj` 的扫描路径；可还原的包 | `<run_id>/group-1/csharp_roslyn/`，含 `per-tfm/*.sarif`、`csharp-report.sarif`、`csharp-reconciliation.csv`、`receipt.json`；以及 `<run_id>/group-1/evidence-bundle/` | 退出 `0`，`receipt.ok=true` | `2` 输入错误（无项目文件、多个项目、目标框架标识不安全），`1` 构建失败或选中源文件未被编译，`130` 取消 |
| `handoff.py export-csharp` | 完成的 C# 运行加 C# 产品 profile | 相同交付结构，唯一工具为 `dotnet-netanalyzers` | 退出 `0`，stdout 输出 `{"status":"delivered",...}` 收据 | 退出 `2`，stderr 输出 `error: <原因>` |

C# 通道不会设置 `evidence_gate_required`，因此完成扫描时退出 `0`，也不使用退出 `3`。
`release-gate.json` 里仍会列出 C/C++ 家族的门禁错误（`Missing tool version: cppcheck`、
`All three tools are required for certification`、`Source fallback is not a three-tool
compilation-database bundle`、`Compiler dependency baseline unavailable or incomplete`），
并保持 `ok: false`。当且仅当 `checks` 中唯一的 `csharp_roslyn` 条目没有错误、且与选中
源文件单元精确对账时，`export-csharp` 接受这样的运行。也就是说，该文件整体 `ok` 为
false 并不阻断本通道；`checks.csharp_roslyn.errors` 非空才阻断。

### 2.3 接收端报告步骤

接收方是独立的 `sast-report-pipeline` 仓库。它只需要交付目录，不需要本仓库、原始运行目录
或源码检出。

```sh
# 准备规范 three-tool-input-1.0 输入并绑定本地资产。
uv run --frozen --offline python scripts/sast_scan.py prepare-runner \
  --delivery /srv/deliveries/product-a-<run_id> \
  --product product-a \
  --output-dir /srv/report-inputs/product-a-<run_id>

# 可选：生成前做严格映射审计。
uv run --frozen --offline python scripts/sast_mapping.py audit \
  --manifest /srv/report-inputs/product-a-<run_id>/manifest.json \
  --product product-a

# 生成并做候选级校验。
uv run --frozen --offline python scripts/three_tool_report.py generate \
  --manifest /srv/report-inputs/product-a-<run_id>/manifest.json \
  --product product-a --mapping-mode strict \
  --output /srv/reports/product-a-<run_id>

# 复核已完成的包。
uv run --frozen --offline python scripts/three_tool_report.py verify \
  --run /srv/reports/product-a-<run_id> --level candidate
```

| 步骤 | 输入 | 输出 | 成功判据 | 常见失败码 |
| :--- | :--- | :--- | :--- | :--- |
| `prepare-runner` | 交付目录，以及与交付声明一致的 `product_id` | 新目录，含规范 `manifest.json`、复制的 `assets/`、`provenance/runner-delivery/` | 退出 `0`，`status: prepared`，`execution_authenticated: false` | 退出 `2`，`error.code` 如 `invalid_runner_delivery`、`product_mismatch`、`scope_mismatch` |
| `generate` | 已准备的 manifest | 新报告包 | 退出 `0`（`candidate`）或 `10`（`candidate-pending`），`verification.status: verified` | 退出 `2`（映射缺口、输入无效、校验失败） |
| `verify --level candidate` | 报告包 | 校验 JSON | 退出 `0`，`status: verified`，`level: candidate` | 退出 `2` |

即使交付中存在尚未映射的规则，`prepare-runner` 也可以成功，随后严格审计与严格生成会
fail-closed 失败。退出 `10` 表示候选包已完成、仍有待审事项，不是工具故障。

---

## 3. Product profile

profile 由运维方维护，是声明产品范围的唯一位置。复制
[`examples/runner-handoff/product-profile.example.json`](../examples/runner-handoff/product-profile.example.json)
后填写。两个导出通道都要求且仅要求下列十一个字段，多一个或少一个都会被拒绝。

| 字段 | 约束 |
| :--- | :--- |
| `schema_version` | `three-tool-product-profile-1.0` |
| `product_id` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`，必须与接收端 `--product` 相同 |
| `display_name` | 非空文本 |
| `repository` | 非空文本；含 URL 用户信息的写法（`https://user@host/...`）会被拒绝 |
| `component_id`、`target_id`、`configuration` | 与 `product_id` 相同的标识符规则 |
| `included_paths` | 非空数组，仓库相对 glob；不得绝对路径、不得含 `..`、不得反斜杠、不得重复 |
| `excluded_paths` | 同类型的数组，可以为空 |
| `languages` | 非空，且必须是本通道语言集合的子集：C/C++ 为 `C`、`C++`，C# 为 `C#` |
| `unsupported_languages` | 显式字符串数组，可以为空 |

范围判定基于仓库相对路径，而不是扫描路径。基准是捕获到的
`git rev-parse --show-toplevel`，因此仓库根为 `/srv/product`、扫描
`/srv/product/src` 的产品需要 `src/**`；在本仓库内扫描 `examples/cpp` 需要
`examples/cpp/**`。匹配使用 `fnmatch`，`*` 会跨 `/`，不是 Git ignore 语义，
也不存在 `!` 取反。

`included_paths` 与 `excluded_paths` 不能在实际效果上重叠。每个选中单元先要求匹配某个
include，随后若匹配任一 exclude 就被拒绝：

```text
error: selected source falls outside the explicit product-profile include scope: src/generated/Codes.cs
error: selected source also matches an explicit product-profile exclusion: src/generated/Codes.cs
```

落在重叠区的文件对该范围是致命的。正确做法是把 include 收窄到真正属于产品的代码；
为了让导出通过而删掉 exclude 会把生成代码或第三方代码拉进产品范围。接收端会重复同样的
逐单元检查，并报 `scope_mismatch`。

profile 同时决定通道：`languages: ["C#"]` 只能用于 `export-csharp`，
`languages: ["C"]`/`["C++"]` 只能用于 `export`。声明成另一族时报
`this delivery lane supports explicit C# scope only`（C# 通道）或
`this delivery lane supports explicit C and/or C++ scope only`（C/C++ 通道）。

---

## 4. 多目标框架（TFM）语义

.NET 项目可以声明多个目标框架。扫描器逐个构建，交付中彼此独立。

- profile 中的 `target_id` 是产品构建目标。实际交付的目标身份是
  `target_id + "[" + target_framework + "]"`，例如
  `product-b-service-core[net8.0]`。`tool_runs[].targets[].target` 与
  `artifacts[].target` 都使用该形式；多个框架共用一个身份会让下游无法区分。
- 每个框架一次独立构建，各自拥有 `units_attempted`/`units_completed` 与原生
  `per-tfm/csharp-report.<tfm>.sarif`：

  ```sh
  dotnet build <project> \
    -p:RunAnalyzersDuringBuild=true -p:EnableNETAnalyzers=true \
    -p:AnalysisLevel=latest -p:AnalysisModeSecurity=AllEnabledByDefault \
    -p:TargetFramework=<tfm> \
    -p:ErrorLog=<绝对路径>.sarif%2Cversion=2.1 \
    --no-incremental
  ```
- 合并的 `csharp-report.sarif` 供人工阅读，交付时只作为
  `provenance/native-merged/csharp-report.sarif`；每个 artifact 是该框架编译器的原样输出。
- 项目未声明 `TargetFramework`（仅有 `.sln` 不够）或框架数超过 32，以及框架标识不能
  安全用于文件命名时，扫描器直接失败。

### 4.1 计数含义

交付 manifest 的 `counts` 包含：

| 键 | 含义 |
| :--- | :--- |
| `target_frameworks` | 实际构建的目标框架 |
| `units_by_target_framework` | 该框架下编译的产品分析单元（选中单元与该框架 Compile 项的交集） |
| `compiled_units_by_target_framework` | 该框架全部 Compile 项，包含产品之外生成的源码 |
| `diagnostics_by_target_framework` | 该框架原生 SARIF 的诊断数 |
| `selected_analysis_units` | 交付单元清单大小 |
| `artifact_count_by_tool` | 每个工具的原生产件数 |
| `scanner_reported_diagnostics.dotnet_netanalyzers_native_sarif` | 各框架 SARIF 的诊断总数 |

`units_by_target_framework` 可以小于 `compiled_units_by_target_framework`。包可以为项目
提供被编译的源码，例如多个 NuGet 包在旧框架下向 `obj/` 添加的 `NullableAttributes.cs`。
该文件确实是那个框架的 Compile 项，因此计入 `compiled_units`，也计入该框架的
`units_attempted`/`units_completed`；但它不是产品分析单元，不会出现在 `analysis_units` 中。

单元清单来自 MSBuild，而不是目录列举：

```sh
dotnet build <project> -getItem:Compile -p:TargetFramework=<tfm> -nologo -v:q
```

`-getItem:Compile` 返回该框架求值后的 Compile 项，因此 `units_*` 既不是目录中的 `.cs`
文件数，也不是过滤后的目录列举。路径中含 `bin` 或 `obj` 的条目会被扫描侧的
`IGNORED_PATH_PARTS` 排除，构建产物不会成为选中单元。`-getItem` 查询失败即 fail-closed：
该框架报告 0 个尝试、0 个完成单元。

被选中却不属于任何框架 Compile 项的 `.cs` 文件会让扫描失败，收据中会点出该文件：

```text
以下源文件不是项目任何 TargetFramework 的 Compile 编译单元，未被分析: src/Legacy/Old.cs
```

同一状况也阻断导出（`C# selected source units are not compiled by any target
framework: ...`）。应把这类文件移出扫描路径，或在 profile 范围中排除；文件放在目录里
并不等于它被分析过。

### 4.2 已知限制：单框架交付

`export-csharp` 总会写入 `target_frameworks`、`units_by_target_framework`、
`compiled_units_by_target_framework`、`diagnostics_by_target_framework` 四个计数。当前
接收端只在交付声明多个框架时接受这四个键，单框架交付会被拒绝：退出 `2`，
`error.code: invalid_runner_delivery`，message 为
`delivery counts does not match the delivery contract`。

在该代码对齐之前，只有多目标框架的 C# 交付能通过 `prepare-runner`。这是两个仓库之间的
契约差异，不是扫描器或 profile 问题；不要通过手工编辑交付来绕过。

---

## 5. 版本语义

一次 C# 运行由三个不同版本描述，彼此不可替代。

| 值 | 捕获位置 | 含义 |
| :--- | :--- | :--- |
| `tool_version` | `tool_runs[].tool_version`、`artifacts[].tool_version`、`provenance/rules/dotnet-netanalyzers.json` | 产生 CA 诊断的 `Microsoft.CodeAnalysis.NetAnalyzers` 包版本；实测为 `8.0.0` |
| `dotnet.sdk_version` | `provenance/tool-versions/binary-identities.json`、`dotnet.version.stdout.txt` | 执行构建的 SDK 横幅（`dotnet --version`）；实测为 `8.0.404` |
| `dotnet.compiler.semantic_version` / `compiler.versions` | 同一文件，取自 SARIF `tool.driver` | 生成 SARIF 的 C# 编译器；实测为 `4.11.0` / `4.11.0-3.24510.3 (cfe3bd0a)` |

分析器版本从项目的 `project.assets.json` 解析（`tool_version_source: project.assets.json`）。
还原产物不可用时退回 SDK 属性 `_NETAnalyzersSDKAssemblyVersion`
（`tool_version_source: dotnet-sdk`）。两者都取不到时 `tool_version` 保持 `null`，并在
`limitations` 中写明原因；接收端随后以 `invalid_tool_version` 拒绝该交付，因此未解析的
分析器身份不是可交付状态。

SARIF 的 driver 永远是 C# 编译器，不是分析器：`driver.version`、`semanticVersion`、
`dottedQuadFileVersion` 都属于编译器来源。把编译器版本当作分析器版本，会给每条发现
贴上错误标签。

C/C++ 通道的版本 token 是上文捕获的 `cppcheck`、`clang-tidy`、`clang`/`clang++`，
`clang-static-analyzer` 绑定 clang token，原生 plist 的 `clang_version` 字符串原样保存在
`provenance/tool-versions/csa-native-version-strings.json`。

---

## 6. MSBuild `ErrorLog` 编码

SARIF 开关必须写成百分号编码的逗号：

```sh
-p:ErrorLog=/srv/scan-results/run/group-1/csharp_roslyn/build.net8.0.sarif%2Cversion=2.1
```

MSBuild 按逗号切分 `-p:` 的值，因此文档常见的编译器写法
`ErrorLog=<file>,version=2.1` 到达 `csc` 时已丢掉 `version=2.1`，产出旧格式 SARIF
**1.0.0**。该错误的症状：

- `scan/csharp_roslyn/reporting.py::read_runs` 抛出
  `SARIF 报告必须为 2.1.0 版本且包含非空 runs 数组`；
- 构建收据记录 `error`，本次运行不是 `ok`；
- 被导出或手工修改后的文件在导出时报
  `error: C# SARIF report must declare SARIF version 2.1.0`，接收端也以
  `unsupported SARIF root or version` 拒绝该原生证据，并经 `prepare-runner`
  呈现为 `invalid_runner_delivery`。

任何复现扫描命令的脚本都要保留 `%2C`。实测可用形式为
`-p:ErrorLog=<绝对路径>%2Cversion=2.1`（.NET SDK 8.0.404）。

---

## 7. 证据分层

`group-1/evidence-bundle/manifest.json` 中每个产件记录都带 `layer`，由
`scan/evidence.py::artifact_layer` 判定。

| 分层 | 内容 | 下游作用 |
| :--- | :--- | :--- |
| `[RAW]` | `03-cppcheck/result/cppcheck-compile-report.xml`、`04-clang-tidy/result/fixes/*.yaml`、`05-clang-static-analyzer/result/csa-native/*.plist`、`06-csharp-roslyn/**/*.sarif` | 原生分析器字节，诊断的唯一事实来源 |
| `[RECEIPT]` | 执行收据、`tu-mapping.csv`、`csharp-reconciliation.csv`、`code-state.txt`、`source-hashes.json`、版本横幅、扫描器与二进制哈希 | 证明跑了哪些单元、怎么跑、相对哪个提交 |
| `[CACHE]` | 路径含 `cppcheck-build` 的条目 | 本机 Cppcheck 分析缓存，永不作为报告输入 |
| `[DERIVED]` | `.html`、`.css`、`.md` 视图、`summary.json`、`severity.csv`、`release-gate.json` | 人工阅读辅助与门禁结论 |

只有 `[RAW]` 与 `[RECEIPT]` 可对外移交，且由交付包承载：原生字节放在 `native/`，复制的
JSON/CSV/TXT 收据（含 `release-gate.json`）放在 `provenance/`。HTML/CSS/Markdown 阅读
视图与 Cppcheck 缓存不会进入交付包。

移交前须按产品方的数据处理要求审核：原始 provenance 可能包含构建与源码的绝对路径，
原生输出（CSA plist、clang-tidy YAML、SARIF）可能包含源码片段。

---

## 8. 排错表

| 症状 | 原因 | 定位文件 | 处置 |
| :--- | :--- | :--- | :--- |
| `error: product delivery requires a clean captured Git baseline` | 扫描或导出时 `status`/`diff` 非空，或提交号不是 40 位十六进制 | `01-metadata/code-state.txt`、`before-git-status.txt`、`before-git-diff.txt`；`scan/handoff.py::_captured_git_baseline` | 提交源码状态、忽略构建产物后重扫 |
| `release-gate.json` 中 `error: Git baseline changed during scan` | 工具运行期间出现或改动了已跟踪/未跟踪文件 | `before-*`/`after-*` Git 收据；`scan/evidence.py::finish` | 扫描期间不要向工作树写入；重扫 |
| `error: Unsupported or unverified tool version: cppcheck: ...` 或 `... clang-tidy: ... dev ...` | Cppcheck 低于 2.22、LLVM 低于 14，或使用开发构建 | `release-gate.json`；`01-metadata/cppcheck.version.*`、`clang-tidy.version.*` | 安装发布版后重扫，不得改名二进制 |
| `error: clang, clang++ and clang-tidy captured release tokens must agree` | `PATH` 上存在多套 LLVM | 待导出运行的 `provenance/tool-versions/*` | 统一为同一 LLVM 发布版后重扫 |
| `error: C# SARIF report must declare SARIF version 2.1.0` | `-p:ErrorLog` 缺 `%2C`，`version=2.1` 被吞掉 | `group-1/csharp_roslyn/build.<tfm>.stderr.log`、`receipt.json.builds[].sarif` | 恢复 `%2Cversion=2.1` 后重扫；不要修补 SARIF |
| 扫描退出 `1`，提示 `以下源文件不是项目任何 TargetFramework 的 Compile 编译单元` | 选中的 `.cs` 文件不被任何目标框架编译 | `csharp-reconciliation.csv` 中 `status: discovered` 行 | 从扫描路径移除该文件，或在 profile 中排除 |
| `error: selected source falls outside the explicit product-profile include scope: <path>` | include 相对 Git 顶层目录，范围过窄 | 交付的 `components[].relative_source_paths`、`git rev-parse --show-toplevel` | 把 include 放宽到真实产品子树（如 `src/**`、`examples/cpp/**`） |
| `error: selected source also matches an explicit product-profile exclusion: <path>` | 选中单元同时匹配 include 与 exclude | profile 的 `included_paths`/`excluded_paths` | 收窄 include；不要删掉 exclude 把生成代码拉进范围 |
| `error: this delivery lane supports explicit C# scope only`（或 C/C++ 版本） | profile `languages` 属于另一条通道 | profile 与所选子命令 | C# 用 `export-csharp` 配 `["C#"]`；C/C++ 用 `export` 配 `["C"]`/`["C++"]` |
| 导出退出 `2`，`failed or incomplete scanner execution cannot be exported as a complete delivery` | 运行 `manifest.json` 记录 `ok: false`、非零退出、取消或报告错误 | `<run_id>/manifest.json`、各工具 `receipt.json` | 修复工具或构建问题后重扫 |
| `error: the C# evidence check reports errors: ...` | C# 门禁未与选中源文件单元精确对账 | `release-gate.json` 的 `checks.csharp_roslyn` | 确认运行是纯 C#，且扫描后未被改动 |
| C# 运行 `release-gate.json` 为 `ok: false`，但 `export-csharp` 成功 | 预期行为：C/C++ 家族的门禁原因会留在该文件 | `release-gate.json` 的 `errors` | 无需处理；只有 `checks.csharp_roslyn.errors` 会阻断本通道 |
| 接收端退出 `2`，`error.code: invalid_runner_delivery`，message `delivery counts does not match the delivery contract`（C# 交付） | 单目标框架，接收端当前不接受该计数形状 | 交付 `manifest.json` 的 `counts` 与 `counts.target_frameworks` | 见 4.2 已知限制，需修改接收端代码 |
| 接收端 `invalid_tool_version` | `tool_version` 不是数字版本（分析器版本未解析） | `provenance/tool-versions/binary-identities.json`、`rules/dotnet-netanalyzers.json` 的 `limitations` | 修好还原、使 `project.assets.json` 解析到包版本后重扫并重新导出 |
| 导出退出 `2`，`error: output already exists; refusing to overwrite` | 输出目录已存在 | `--output` 参数 | 使用新目录；交付不会被覆盖 |
