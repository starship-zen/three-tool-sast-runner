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

### 5.4 Clang Static Analyzer 规范 (`05-clang-static-analyzer/`)
1. **原生 Plist 强制落盘机制 (`[RAW]` 与零结果机制)：**
   * 调度脚本通过 `clang --analyze` 遍历编译数据库时，必须配置 `-Xclang -analyzer-output=plist -o csa-native/<slug>.plist`。
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

当上游扫描工具产出符合本规范的目录包（假设路径为 `/path/to/<product>-scan-bundle/`）后，下游自动化报告生成体系的标准对接流程如下：

```bash
# 生成最终版合规交付报告（含 10 页 PDF 主报告、HTML/Markdown 附录、CSV、SARIF）
python3 scripts/three_tool_report.py generate \
  --product <secp_or_tos_hub> \
  --manifest "/path/to/<product>-scan-bundle/manifest.json" \
  --output "workspaces/mm7-safety/sast/runs/partner-<product>-final" \
  --mapping-mode strict \
  --pdf
```
