# 三工具自动化扫描执行器证据包与数据契约规范 (v1.2)

[ English ](SPEC.md) • [ 简体中文 ](SPEC.zh-CN.md)

> **文档性质：** 架构设计规范与数据接口契约（Interface Contract）  
> **适用对象：** 负责开发与维护部署机端自动化扫描执行器的研发工程师  
> **下游消费者：** 离线报告生成器（`three_tool_report.py`）

---

## 1. 背景与目标

本规范定义面向工控与工业 C/C++ 软件系统的编译机/部署机端自动化静态代码安全扫描（SAST）执行套件接口。

执行器（Runner）负责自动化调度三套互补的开源静态分析引擎：
1. **Cppcheck**（2.22+ 版本）
2. **Clang-Tidy**（LLVM 14+ 版本）
3. **Clang Static Analyzer (CSA)**（LLVM 14+ 版本）

其核心目标是：在源码编译机上直接跑完三套工具后，产出一份**自包含、不可篡改、证据闭环的结构化证据包**。下游离线报告引擎在无网络和无 AI 介入的环境下直接消费该证据包，完成 TU 覆盖对账、同工具精确去重、将发现项映射至 **CWE Top 25:2025** 与 **OWASP Top 10:2025**、评定 Critical/High/Medium/Low 四级工业审计风险，并直接输出符合 IEC 62443-4-1 SI-1 / SI-2 要求的正式合规交付报告。

---

## 2. 核心设计原则

1. **单产品严格隔离（Single-Product Isolation）**
   * 一次扫描运行只针对单一产品（如 `secp` 或 `tos_hub`），严禁跨产品混合打包。
   * 每个产品必须独立生成各自的制品目录或专用归档包（例如 `<product>-scan-bundle-<timestamp>.zip`）。
2. **三工具闭环，缺一不可（Non-Negotiable Three-Tool Coverage）**
   * 必须在**完全相同的代码提交（Commit）与相同的编译单元（TU）范围**上完整执行 Cppcheck、Clang-Tidy 与 Clang Static Analyzer (CSA)。任何一门工具未运行或产物缺失，下游报告引擎将直接判定为覆盖缺口并拒绝生成合格报告。
3. **产物明确分层标识（Clear Separation of Raw vs Derived）**
   * 严禁将外围脚本二次生成的统计或摘要误标为分析器原生输出。
   * 必须严格区分：分析器原生诊断 `[RAW]`、执行器审计收据 `[RECEIPT]`、内部构建缓存 `[CACHE]`、衍生统计浏览件 `[DERIVED]`。
4. **编译数据库为唯一基线（Compile Database Binding）**
   * 必须基于 CMake 导出的 `compile_commands.json` 执行，以实际选中的 Translation Unit (TU) 集合作为对账基准，而不是单纯比对文件总数。

---

## 3. 四层产物分类定义与标识

| 分层标识 | 类别定义 | 生产者主体 | 在下游报告生成中的作用 |
| :--- | :--- | :--- | :--- |
| **`[RAW]`** | **分析器原生诊断层** | 编译器/分析器二进制直接写出的结构化文件 | **核心法定证据**：下游映射与评级引擎直接消费此层数据 |
| **`[RECEIPT]`** | **执行器元数据与收据层** | 自动化调度 Runner 脚本记录的环境与进程回执 | **审计与对账证据**：证明环境真实、命令可溯、所有 TU 均被执行 |
| **`[CACHE]`** | **分析器内部缓存层** | 分析器二进制内部中间状态 | **性能优化数据**：证明启用了持久化构建缓存，非最终审计报告 |
| **`[DERIVED]`** | **衍生统计与浏览层** | 后处理脚本或转换程序生成的 Markdown/HTML | **人工阅读辅助**：供开发者快速浏览，下游生成器不以此为数据源 |

---

## 4. 标准产出目录结构

扫描工具执行完毕后，交付或导出的制品目录（或解压后根目录）必须严格按照以下结构组织并提供各分层产物：

```text
<product_name>-scan-bundle/
├── 01-metadata/                                # 【模块一：环境与代码基线】
│   ├── scan-environment.json                   # [RECEIPT] 机器、OS、编译器、工具版本等机器可读信息
│   ├── scan-environment.md                     # [DERIVED] 人类可读的环境摘要表格
│   ├── code-state.txt                          # [RECEIPT] Git 仓库、分支、Commit SHA 及未提交改动状态
│   ├── compile-db-selected-tus.txt             # [RECEIPT] 实际纳入本次扫描的源码 TU 路径清单
│   └── analy.config                            # [RECEIPT] 本次扫描使用的真实配置文件副本
├── 02-compile-db/                              # 【模块二：构建与编译数据库】
│   └── compile_commands.json                   # [RECEIPT] 完整的编译数据库（用于下游追溯和命令绑定）
├── 03-cppcheck/                                # 【模块三：Cppcheck 扫描产物】
│   ├── result/
│   │   ├── cppcheck-compile-report.xml         # [RAW] 原生 XML v2 诊断（包含 id, severity, msg, location, cwe）
│   │   └── cppcheck-compile-html/              # [DERIVED] 可选：cppcheck-htmlreport 生成的人工浏览网页
│   ├── cppcheck-artifacts/                     # [RECEIPT] 调度脚本生成的执行凭证
│   │   ├── command.txt                         # [RECEIPT] 实际执行的完整命令行参数
│   │   ├── exit-code.txt                       # [RECEIPT] 进程真实退出码（通常为 0）
│   │   ├── version.txt                         # [RECEIPT] `cppcheck --version` 终端回读结果
│   │   ├── summary.json                        # [DERIVED] 调度脚本解析 XML 得出的各级别初步统计
│   │   └── input-files.csv                     # [RECEIPT] 实际输入给 cppcheck 的源文件列表
│   └── cppcheck-build/                         # [CACHE] cppcheck 中间分析 AST 缓存目录
├── 04-clang-tidy/                              # 【模块四：Clang-Tidy 扫描产物】
│   └── result/
│       ├── fixes/                              # [RAW] 核心：逐源文件导出的原生规则诊断事件 YAML
│       │   ├── <target1>.cpp.yaml              # [RAW] 通过 `-export-fixes` 导出的原生 YAML（零告警文件不产生）
│       │   └── <target2>.cpp.yaml              # [RAW]
│       ├── logs/                               # [RECEIPT] 逐源文件 stdout/stderr 运行日志容器
│       │   ├── <target1>.cpp.log               # [RECEIPT]
│       │   └── <target2>.cpp.log               # [RECEIPT]
│       ├── clang-tidy-summary.json             # [RECEIPT] 调度脚本汇总的 TU 状态对账表与告警统计
│       ├── clang-tidy-summary.md               # [DERIVED] 可选：Markdown 格式统计摘要
│       └── clang-tidy-report.html              # [DERIVED] 可选：人工浏览 HTML 报告
└── 05-clang-static-analyzer/                   # 【模块五：Clang Static Analyzer (CSA) 产物】
    └── result/
        ├── csa-native/                         # [RAW] 核心：原生 Plist 诊断证据链
        │   ├── <target1>.plist                 # [RAW] 逐 TU 原生 plist 文件（零缺陷文件必须输出合法的空 plist）
        │   └── <target2>.plist                 # [RAW]
        ├── tu-mapping.csv                      # [RECEIPT] 调度脚本生成的 TU 对账表（slug,file,diagnostics,exit_code）
        ├── compile-db/                         # [RECEIPT/DERIVED] 执行状态与辅助索引
        │   ├── analyzer-status.json            # [RECEIPT] 运行统计（total, analyzed, failed, reports, elapsed_seconds）
        │   ├── analyzer-files.log              # [RECEIPT] 分析文件清单
        │   ├── analyzer.log                    # [RECEIPT] 完整执行聚合日志
        │   └── index.html                      # [DERIVED] 可选：网页目录索引
        └── analyzer-summary.md                 # [DERIVED] 可选：总括 Markdown 摘要
```

---

## 5. 各工具技术契约细则与实测引擎机制

### 5.1 环境与代码基线 (`01-metadata/`)
1. `code-state.txt` (`[RECEIPT]`)：扫描前由脚本动态抓取，严禁手写，必须包含四要素：
   ```text
   repo: <git_remote_url_or_directory_name>
   branch: <git_branch>
   commit: <full_40_char_git_commit_sha>
   date: <iso_or_local_timestamp>
   --- git status --porcelain ---
   <若存在未提交改动则如实打印，无改动则留空表示 clean>
   ```
2. `scan-environment.json` (`[RECEIPT]`)：工具链实际版本必须从执行二进制实时回读（如 `cppcheck --version`、`clang-tidy --version`、`clang --version`），严禁使用静态配置字符串填充。
3. `compile-db-selected-tus.txt` (`[RECEIPT]`)：记录实际参与分析的源码 Translation Unit 相对路径清单，行数 $N$ 必须与后续三工具各自执行的 TU 数量完全对齐。

### 5.2 Cppcheck 规范 (`03-cppcheck/`)
1. **原生命令要求 (`[RAW]` 与零结果机制)：**
   * 必须启用编译数据库模式：`--project=compile_commands.json --xml --xml-version=2 2> result/cppcheck-compile-report.xml`。
   * **实测机制知晓：** 当被测源文件为 0 缺陷时，Cppcheck 原生会输出包含合法 `<results version="2"><cppcheck .../><errors></errors></results>` 的空结构 XML。空文件或非 XML 属于异常失败。
2. **缓存配置 (`[CACHE]`)：**
   * 必须指定 `--cppcheck-build-dir=cppcheck-build`，加速二次执行并证明缓存机制生效。
3. **调度收据要求 (`[RECEIPT]` 与 `[DERIVED]`)：**
   * 调度脚本必须将实际命令写入 `command.txt`，将退出码写入 `exit-code.txt`，将工具版本写入 `version.txt`，将实际输入文件列表写入 `input-files.csv`（均为 `[RECEIPT]`）。`summary.json` 为严重度统计摘要（`[DERIVED]`）。

### 5.3 Clang-Tidy 规范 (`04-clang-tidy/`)
1. **原生诊断导出机制 (`[RAW]` 与零结果机制)：**
   * 调度脚本逐源文件调用 clang-tidy，必须附带原生参数 `-export-fixes=fixes/<slug>.yaml`。
   * **实测机制必须知晓：** **当源文件完全干净（零告警）时，Clang-Tidy 原生机制不会生成 YAML 文件！**
   * 因此，`fixes/` 目录下的 `.yaml` 文件数通常 $\le N$，下游报告引擎绝不以“yaml 文件数是否等于 TU 数”作为门禁。
2. **调度收据与对账 JSON (`[RECEIPT]`)：**
   * 零告警的合规证明必须由调度脚本写入 `clang-tidy-summary.json`。该文件必须包含：
     * `selected_file_count` (int)：实际选择分析的 TU 总数 $N$；
     * `warnings.raw_total` (int)：全量原始警告总数；
     * `warnings.unique_total` (int)：去重后的规则警告总数；
     * `warnings.by_check` (array)：分项规则统计清单；
     * `files` (array)：记录**每一个 TU 的执行状态**，格式为 `{"file": "相对路径", "status": "ok", "unique_diagnostics": 数量}`。当 `unique_diagnostics == 0` 且 `status == "ok"` 时，即证明该文件已由 Clang-Tidy 成功分析且确认无告警。

对于 v1.2 报告交接，`warnings.raw_total` 与 `by_check[].raw` 统计提交的原生 YAML
诊断记录条数，不是 CLI 日志中的汇总数字。日志统计如有不同须另行保留，不能替代原生条数。

### 5.4 Clang Static Analyzer 规范 (`05-clang-static-analyzer/`)
1. **原生 Plist 强制落盘机制 (`[RAW]` 与零结果机制)：**
   * 每个 TU 只执行一次 `clang --analyze ... -Xanalyzer -analyzer-output=plist-html -o reports/<slug>/<slug>.plist`，由 LLVM 同次生成支持跨文件路径的原生 plist 与 HTML。每个 TU 使用独立目录，导出以真实执行收据的 `artifact` 为准，不猜测平铺路径，也不伪造第二条 `html_execution`。
   * 合法零诊断 plist 可以没有 HTML；有诊断则必须有非空普通 HTML 文件。非零退出、无效 plist 或有诊断却无 HTML 均判执行失败。不根据普通单文件 plist 的零结果跳过 HTML，因为它可能遗漏多文件格式支持的跨文件路径。
   * 跨文件缺陷的原生计数可能因此增加；对账以诊断记录为准，不能把联合输出消费者重复打印的警告行数当作发现项数。
   * **实测机制必须知晓：** **即使该文件没有任何缺陷，Clang 依然会写出一个合法的空 Plist 文件**（包含完整的 XML 声明和 `<dict>` 标签，但 `<key>diagnostics</key><array></array>` 为空）。
   * **门禁铁律：** `csa-native/` 目录下的 `.plist` 文件总数，**必须严格等于选定的 TU 总数 $N$**！
2. **调度对账表 (`[RECEIPT]`)：**
   * 调度脚本必须生成 `tu-mapping.csv`，严格包含以下四列：
     ```csv
     slug,file,diagnostics,exit_code
     0f4533aa883f,src/service/main.cpp,0,0
     95b99c0f081d,src/service/handler.cpp,1,0
     ```
   * 数据行数必须与 `compile-db-selected-tus.txt` 的行数完全一致。

---

## 6. 上游开发交付前自检门禁（Preflight Checklist）

在将制品包上传 GitHub 或交付给报告生成引擎前，自动化流程或开发人员应通过以下自检清单：

| 校验维度 | 检查项与判据 | 异常后果（若不满足） |
| :--- | :--- | :--- |
| **产品隔离** | 制品内仅包含单一产品，无同级混装其他产品数据 | 下游报错拒收，无法生成合法单产品报告 |
| **TU 身份集合闭环** | `compile-db-selected-tus.txt` 集合 == `cppcheck-artifacts/input-files.csv` 集合 == `clang-tidy-summary.json.files` 集合 == `tu-mapping.csv` 的 file 集合 == CSA 分析目标集合 | 判定为扫描范围不一致，对账失败 |
| **原生证据完整度** | `cppcheck-compile-report.xml` 包含合法 XML 结构；<br>`fixes/` 下的 `.yaml` 数量与 summary 中告警文件数匹配；<br>`csa-native/` 下的 `.plist` 数量严格等于 TU 总数 $N$ | 判定为假报告或未验证状态（Unverified） |
| **版本不可篡改追溯** | `code-state.txt` 中的 Commit 为完整 40 位 SHA-1，非 `not recorded` 或留空 | 审计不可否认性失败，Release Gate 阻断 |

---

## 7. 对接下游离线报告生成器

下游是独立的 `sast-report-pipeline` 仓库，不是历史合作方混包适配器。
其规范 `three-tool-input-1.0` 清单不是 Runner 的产品交付格式：本仓库导出
`three-tool-scan-delivery-1.0`，接收端校验后绑定其本地冻结资产并准备报告输入。
仅有编号目录不能作为任一边界的合法输入。

```bash
# 生成待审候选报告包，不等于客户发布批准。
python3 scripts/three_tool_report.py generate \
  --product secp \
  --manifest "/path/to/<product>-scan-bundle/manifest.json" \
  --output "/path/to/new-product-report" \
  --mapping-mode strict
```

PDF 是命令行可选输出，须单独预置并验收渲染工具链。本机网页工作台当前不生成 PDF。

### 7.1 接收端根清单与零诊断契约（2026-10-21）

报告消费者的根目录必须包含 `manifest.json`，采用 `schema_version: "three-tool-input-1.0"`
和 `producer_contract: "three-tool-sast-runner-1.2"`。必需部分包括 `run_id`、
`project`（含 `product_id`）、`source`、`tool_runs`、`artifacts`、`assets` 与
`provenance_inputs`。引用路径均相对根目录，并绑定实际 SHA-256。可运行的合成交接
样例与完整字段示例由 `sast-report-pipeline` 的 `docs/runner-handoff.md` 和
`examples/runner-handoff/` 维护；它们是测试样例，不是已执行的扫描。

- `artifacts` 只放真实原生 XML/YAML/plist，绑定实际工具、版本、组件、配置、
  目标及已知源分析单元；缓存和衍生 HTML 不作为原生诊断。
- 从实际记录填写 `source.repository`、`source.commit`、源码范围和
  `source.build_configuration_sha256`，用 `purpose: "build-configuration"`
  保留对应配置原件。如果明确将编译数据库作为构建配置身份，就绑定该文件及其实际哈希，
  不编造另一个配置哈希。
- 每项 `tool_runs` 记录实际版本、命令和规则配置哈希，以及各目标的实际起止时间、
  尝试/完成状态、退出码、结果和选中/完成分析单元；用 `purpose: "rules-configuration"`
  及匹配身份保留各工具配置。三工具声明的 TU 集合须对账。未知值保持 JSON null，
  形成不完整候选和发布门禁，不猜测成功。
  已完成目标须显式提供整数 `units_attempted`、`units_completed`，均等于选中 TU
  数量；缺少计数不能作为完成证据。
- 在 `assets` 中绑定冻结标准和规则映射。完整 CWE、CWE Top 25:2025 与
  OWASP Top 10:2025 是独立视图；运行时不依赖 AI 或在线分类。未知规则不能自动填 N/A。
- 源码、环境、选中范围和执行收据放入哈希绑定的 `provenance_inputs`，不混作原生诊断。
  新共同批次的目标 ID 应是实际目标身份，不沿用历史 `new-delivery` / `prior-delivery`
  收集批次标签。

clang-tidy 目标包含零告警单元时，使用以下显式支持证据条目：

```json
{
  "purpose": "supporting-evidence",
  "evidence_type": "clang-tidy-zero-diagnostic-receipt",
  "path": "04-clang-tidy/result/clang-tidy-summary.json",
  "sha256": "<该 summary 原始字节的实际 SHA-256>",
  "tool": "clang-tidy",
  "tool_version": "<实际版本；确实未知时用 null>",
  "component": "<匹配的组件>",
  "configuration": "<匹配的配置；未知时用 null>",
  "target": "<匹配的目标>"
}
```

这只是模板，不是完整可执行清单。summary 使用 §5.3 的字段，其唯一文件身份、
选中数量须与声明目标单元一致；成功零诊断行不能与原生诊断矛盾，非零行仍须有可归属 YAML。
只有全零目标才可完全不提供原生 YAML。不得伪造空 YAML 或诊断记录；Cppcheck 和 CSA
仍须提供真实原生零结果文件。收据校验只证明所交证据内部一致，不认证执行、源码不可变性、
漏洞确认或发布批准。

### 7.2 Runner 产品范围证据交付（2026-10-22）

使用 `scan/handoff.py export --profile PROFILE --run RUN --output NEW_DIRECTORY`，将一个已完成且通过证据门禁的编译数据库组导出为 `three-tool-scan-delivery-1.0`。产品档案 `three-tool-product-profile-1.0` 只声明产品 ID/名称、仓库标识、组件、目标、配置、包含/排除路径和支持/不支持语言；不包含映射、标准资产或接收端路径。

交付根目录含 `manifest.json` 与 `delivery-receipt.json`。清单复用规范输入的 project/source/component/tool_run/artifact/provenance 字段形状，但不是 `three-tool-input-1.0`；文件使用相对路径并绑定大小和 SHA-256。它保留原样的原生字节、实际数值版本、完整版本横幅、二进制哈希、源码/构建/执行收据和计数。缺失 clang-tidy YAML 的单元有类型化引用指向原始 per-file 零结果收据；接收端须解析原生 YAML 并自行生成规范计数收据，不合成空 YAML。

首个通道仅支持一个组、组件、目标和配置。所有选中 TU 必须符合档案路径/语言范围；多组或失败门禁会被拒绝，不从编译数据库推断业务归属。绝对选中路径作为不透明分析单元 ID 保留，仓库相对范围另行绑定到 Git 基线。原始收据可能暴露主机路径，原生输出可能包含源码片段，转移前须按产品方数据策略处理。该交付是未经认证的扫描证据，不是发布批准。

## 8. 已实现的执行器安全契约（2026-10-22）

### 8.1 范围

本节覆盖安装清理、编译配置选择、衍生 HTML、产品范围证据交付和文档配置；不实现接收端规范映射/报告适配器，不独立认证业务归属，也不升级分析器版本。

### 8.2 入口

环境预装后，使用 `uv run --frozen --offline bash scan/scan_all.sh
[--scan-path PATH ...] [--result-path PATH]`。
未提供命令行值时，从 `scan/analy.config` 读取 JSON；根目录模板须复制到此处。
`bash install/preflight.sh` 仅检查二进制可用性。

### 8.3 输入与归属契约

配置只接受 `scan_paths` 和 `result_path`。配置内相对路径以配置目录为基准，命令行
相对路径以当前目录为基准。同一数据库内，解析后源码的工作目录和参数向量相同才可去重，不能静默
保留冲突配置中的第一条。Cppcheck 只自动清理本次安装新建或新解包、且通过安全检查的
默认目录；显式自定义、已存在、不安全或符号链接目录保留。
`CPPCHECK_CLEAN_AFTER_BUILD=0` 禁用清理。衍生 HTML 转义不可信文本和属性，
不改动原生证据，也不改动读取源码时使用的文件系统路径。

### 8.4 校验与错误

配置格式错误、未知字段或编译配置冲突产生输入错误（主 CLI 退出码 2），且不创建扫描
运行目录。无法证明安全的清理路径保留，不递归删除。缺少 YAML 校验依赖不能标为证据
通过，须通过 `requirements.txt` 预置 PyYAML。

### 8.5 正例、基线与反例

普通单配置 TU 正常运行；重复的相同配置只选择一次；同一源码的两个宏配置明确失败。
自定义构建目录保留无关文件；含 HTML 标记的文件名按文本显示，不成为可执行标记。

### 8.6 必需回归检查

`python3 -m unittest discover -s tests` 应覆盖相同和冲突配置及其顺序、扫描目录创建前
拒绝冲突、安装器自有与调用方自有及符号链接目录、HTML 索引/详情/统计页转义，以及用
真实解析器加载所交配置模板。使用替身工具的测试不代表真实分析器或下游验收通过。

### 8.7 错误与正确做法

错误：保留第一个宏配置，清空调用方构建目录，将原始文件名插入 HTML，或执行旧 Shell
配置。正确：拒绝冲突配置，保留调用方产物，转义显示值，并解析文档约定的 JSON 配置。

### 8.8 固定版本的示例库

`examples/README.md` 记录 TinyXML2 C++ 构建/扫描样例和 GuardClauses C# 构建/运行
样例。两个源码子集均保留许可证、固定上游提交和逐文件 SHA-256，并由
`tests/test_examples.py` 校验。C# 样例不新增 C# 分析器支持：直接输入 `.cs` 仍须在
创建运行目录前拒绝。构建成功不代表安全覆盖或发布验收通过，两个样例也不作为漏洞检出率基准。
