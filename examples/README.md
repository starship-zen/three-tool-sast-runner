# C++ and C# example libraries

从仓库根目录运行下列命令。样例源码随仓库提供，不使用子模块；上游文件保持原样，
各目录的 `UPSTREAM.json` 记录固定提交、来源、许可证和逐文件 SHA-256。

| 语言 | 示例库 | 固定版本 | 验证边界 |
| --- | --- | --- | --- |
| C++ | [TinyXML2](https://github.com/leethomason/tinyxml2/tree/9148bdf719e997d1f474be6bcc7943881046dba1) | 11.0.0 | 构建、运行及现有三工具扫描 |
| C# | [GuardClauses](https://github.com/ardalis/GuardClauses/tree/ad43aa02babf3bc8aee8efc2258f5ad3571c8ec5) | v5.0 | .NET 8 构建和运行；当前不支持 C# 安全扫描 |

这些是真实库的最小源码子集，不是故意植入漏洞的检测基准，不保证固定的告警数量。
本仓库新增的冒烟程序与上游源码分开放置；未复制上游完整测试集。
本次实际验证的环境、结果和限制见 [VERIFICATION.md](VERIFICATION.md)。

## C++: build, run, scan

需要 CMake 3.15+、C++11 编译器及相应构建工具。扫描另需根 README 所列的
Python 依赖和三个分析器。

```bash
cmake -S examples/cpp -B examples/cpp/build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cmake --build examples/cpp/build --parallel 2
ctest --test-dir examples/cpp/build --output-on-failure
bash scan/scan_all.sh --scan-path examples/cpp --result-path examples/results/cpp
```

编译数据库包含 `smoke.cpp` 与 `tinyxml2/tinyxml2.cpp` 两个编译单元，头文件随依赖闭包
分析，不另算编译单元。只扫描上游库可将 `--scan-path` 指向 `examples/cpp/tinyxml2`。

扫描结果仍遵循执行器原有门禁：执行完成、检测到告警和发布就绪是不同状态。
即使三个工具执行成功，版本证据或其他门禁不满足时，主命令仍可能返回 3。
不要将 CTest 通过、零告警或样例结果当作客户发布验收。

## C#: build and run only

需要 .NET 8 SDK。首次还原需要访问 NuGet，下载上游指定的
`Microsoft.SourceLink.GitHub` 8.0.0 构建依赖。只验证 `net8.0`，不还原或验证上游其他目标框架。

```bash
dotnet build examples/csharp/smoke/Smoke.csproj -c Release \
  -p:TargetFrameworks=net8.0 -p:GeneratePackageOnBuild=false
dotnet examples/csharp/smoke/bin/Release/net8.0/Smoke.dll
```

冒烟程序验证正常字符串可用、空值被拒绝、负数被拒绝。
上游 SourceLink 构建任务可能因缺少 Git 元数据而告警；来源以固定提交及文件哈希记录为准，
不将本仓库的 Git 信息视为上游 SourceLink 认证。
这不是 C# SAST 验证：当前 runner 没有 C# 后端，直接传入 `.cs` 会报输入错误，
不能将混合目录中的 C# 文件计作已覆盖。覆盖 C# 安全扫描需要后续接入独立的 C# 分析器
及其原生证据、风险与映射支持，本次未实现。

## Source integrity and regression checks

```bash
python3 -m unittest discover -s tests -p test_examples.py
```

此检查验证所收源码与记录哈希相符，并确认 C# 输入不会被伪报为三工具扫描成功。
哈希校验不是上游签名认证，也不替代实际构建、运行或扫描。
构建产物和 `examples/results/` 已忽略，不提交编译缓存、原始扫描结果或 NuGet 缓存。
