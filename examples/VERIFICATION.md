# Example verification — 2026-10-22

## Scope and environment

验证对象为本目录固定版本的 TinyXML2 11.0.0 与 GuardClauses v5.0 源码子集，
命令见 [README.md](README.md)。不是业务项目扫描、漏洞检出率基准或发布认证。

实际运行环境为隔离的 Ubuntu 24.04.5 LTS / aarch64 容器，使用 GCC 13.3.0、
CMake 3.28.3、Clang/clang-tidy 18.1.3、Cppcheck 2.13.0、Python 3.12.3、
PyYAML 6.0.1、.NET SDK 8.0.132。宿主源码只读挂载，实际构建和扫描在容器副本执行；
副本未携带 `.git`。没有扫描业务源码或向生产机器安装依赖。

## Results

- C++：CMake 配置、构建均返回 0，CTest 1/1 通过；编译数据库和三工具均选择
  `smoke.cpp`、`tinyxml2/tinyxml2.cpp` 两个编译单元。
- clang-tidy：两个单元执行成功，42 条原始记录、19 条去重告警。
- Cppcheck：执行成功，123 条诊断；含 information、style 等级，不能等同为 123 个安全漏洞。
- Clang Static Analyzer：两个单元的 plist 与 HTML 分析命令均成功，0 条诊断。
- C#：按 README 限定 `net8.0` 构建返回 0，0 个错误；两个 SourceLink 警告说明隔离副本
  缺少 Git 元数据、无法生成上游源码链接。执行 Smoke.dll 返回 0，输出
  `GuardClauses smoke passed.`，覆盖正常值、空值和负数。
- C# 负向检查：直接传入 `Guard.cs`，主 CLI 返回 2，提示仅支持 C/C++，且不创建结果目录。
  这验证的是拒绝边界，不是 C# 安全扫描能力。
- 回归测试：Ubuntu 128/128 通过；独立复核在 macOS 也通过 128 项测试及 C++ 构建、CTest。
  源码清单及逐文件 SHA-256 检查均包含在这 128 项中。

## Evidence gate and retained output

本次扫描编号为 `20261022T042635Z-cb21ae03f372`。外层 Docker 等待在 120 秒超时返回
124，但容器内任务随后完成。最终 `manifest.json` 记录 `ok=true`、`exit_code=3`、
`evidence_checks_ok=false`、`downstream_compatible=false`、`release_ready=false`。
记录的 3 是执行器最终状态，不是外层 Docker 命令的实测退出码。

证据门禁未通过的具体原因是 Cppcheck 2.13.0 低于要求的 2.22+，以及缺少 Git 提交和
源码仓库基线。门禁另报 `Git baseline changed during scan`；在无 Git 元数据的本次
副本中，该消息不能证明源码实际被修改。未绕过门禁或伪造版本/提交信息。

本机保留的原生输出、逐工具 HTML 和证据包位于以下 Git 忽略目录，不随源码提交：

- [运行入口](results/cpp/20261022T042635Z-cb21ae03f372/index.html)
- [最终清单](results/cpp/20261022T042635Z-cb21ae03f372/manifest.json)
- [证据门禁](results/cpp/20261022T042635Z-cb21ae03f372/group-1/evidence-bundle/release-gate.json)

重新运行时编号和告警数量可能随环境、工具版本变化。取得认可版本的分析器和有效 Git 基线，
也不等于本仓库尚未完成的下游接入、产品隔离及完整离线复现验收自动通过。
