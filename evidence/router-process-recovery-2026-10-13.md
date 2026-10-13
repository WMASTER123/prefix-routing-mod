# 实际 Router 进程故障与恢复验收

日期：2026-10-13。基础提交 2177297，分支 codex/router-recovery-faults。状态：CPU/实际 Router 恢复组合及自审完成；真实 HUST 接入与独立人工审查未完成。执行代码 71f0c951a0a8c4c009da65048c6e0966550c97f3；后续证据提交不改变运行代码。[草稿 PR #17](https://github.com/WMASTER123/prefix-routing-mod/pull/17) 基于 PR #16，未合并。

## 最终验证与实际含义

| 验证 | 最终结果 | 证明范围 |
|---|---|---|
| Windows / Python 3.12 CPU | 448 passed、4 skipped | Ruff/格式、接口与故障测试；跳过项仍为平台限制 |
| Linux aarch64 / Python 3.12 CPU | 452 passed | 本项目 CPU 服务器全量，不是 NPU |
| integration | 两端各 3 passed | 既有 mock 集成 |
| 原生 helper | 21 passed / fmt 通过 | 与最终宿主使用相同 helper 源码；此次 example 的屏障改动不改变 helper |
| 原锁 release | 初版 11m15s，审查版 2m50s | 原 Cargo 依赖不变，隔离 aarch64 完整 Router |
| 实际 HTTP 恢复 | 两组 × 三阶段，pass | 实际 Router/接收子进程/同一日志，模拟后端与服务身份 |
| 实际关闭/旧在线/动态/观察 ACK 丢失/stream | 五组均 pass | 最新准入 binary；stream binary 内容未变 |
| 新 sdist→wheel / 干净安装 | 两端审查版均 pass | import_only、默认关闭、无 torch/vllm 导入、doctor 未激活 |

相对 2177297 新增 11 项 Python 测试：owner 排他/失败清理/正常与异常进程退出，真实接收器准入确认丢失/调用方断开/时钟交接/旧时钟拒绝/缺失日志拒绝，native probe 前置条件、证据读取预算，以及提交后取消的双向屏障。初版 446/450 只记录过程，以上最终 448/452 替代它。

运行时新身份与排他是 tools/experiments 的 CPU 接线，不改变 component-v0.1 或生产组件数。没有新增生产代理/状态服务；MOD 仍 import_only。

## 六阶段组合结果

| 组别 | 第 1 阶段 | 第 2 阶段 | 第 3 阶段 |
|---|---|---|---|
| 准入确认丢失 | 已提交 1 次预留、0 条发送观察、0 扩展发送；新准入由注入的暂停拒绝 | 新 clock/命名空间，保留旧占用 1，再发送 1 次；总占用 2、观察 2 | 再次重启保留占用 2，0 扩展发送；旧观察 duplicate |
| 提交后取消 | 接收器先提交并写通知，native 取消等待后写释放，再允许回复；0 扩展发送；随后接收进程被显式 kill | 重放同一日志，保留原预留 1；新尝试发送 1 次，总占用 2、观察 2 | 再次重启仍保留占用 2，0 扩展发送；旧观察 duplicate |

两组均为债务序列 1→2→2；每阶段重新打开日志均 admission_ready=false，显式交接后才准入。六次第二 owner 打开全部拒绝；每次恢复前检查本工具拥有的旧 Router 及进程组已结束。接收器异常 kill 在旧 Router 结束后发生，不证明任意并发崩溃或真实 NPU 已终止。

模拟 HTTP 统计 fixture=14、fixture-error=6：包含六个 Router 的默认关闭基线调用和恢复中两次扩展发送，不能把 20 次总调用都算扩展发送。没有因丢准入确认、取消或满额重启额外发送。

初始未确认预留在账本中仍为 reserved_without_observation，不伪装成真实 backend accepted/ended 或已观察 unknown；保守保留额度。故障接收器显式暂停新准入是测试注入条件，不能据此声明生产链路会自动检测全部丢 ACK。

## 编译输入、制品和 CI

- 最终 admission binary SHA256：f2fb0c64dcf18d91ff60f8f4eaf75254f1ea9525848aa218b64e582c08658543。
- stream binary：322a7dc10cd91fd7ea9ad3eb085f528eddbe1c9efaa9c7a1ee7bdd2cae6be4ed。
- 原 Cargo.lock：8675caa4ef6227d0f2cb273ac921e9dd0a6989cdad304b5600887a9795355156。
- 宿主三文件与上一阶段实际编译副本逐字节一致；当前六个本项目 helper/example 与新宿主编译输入逐字节一致。三文件 patch 没有新增行数，完整成本包含本轮新增工具、测试与 example，不以 patch 行数代替全部维护成本。
- Windows wheel：e1ea88b103b2fd2369338696c689f842506a36ac013077954ec93fac9c92fd28；sdist：dc362dfb990855ffff3fcae27bbe795d13dfc3245f5b625572d4a410160fa08a。
- Linux wheel：e65c6be99fe110ed7736084567abbda57df598d1e3be7ef39f65be5ad30290e5；sdist：f701a57749e0a1d9db5e6e8c5b3f9440281b777fee1fc72e85af2281021f16b2。两端构建工具/制品分别记录，不声明跨平台字节一致。
- 71f0c95 的 CPU CI [38367344865](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38367344865) 与 helper CI [38367345002](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38367345002) 已核对 headSha/status/conclusion，completed/success；后续记录提交另查对应 head。

报告路径均位于 artifacts/router-recovery-20261013/，原生最终目录为 http-*-reviewed。原始诊断/失败保留，Git 只保存本总结，不保存 capability、原始 prompts/tokens 或模型。

## 初版过程（不替代最终成绩）

- 初版 89b2662：Windows CPU 446 passed、4 skipped；Linux aarch64 CPU 450 passed；integration 两端各 3。
- 初版新增 9 项 Python 测试：单 owner 打开/失败清理/真实进程正常或异常退出，真实接收子进程准入确认丢失/调用方断开/新时钟恢复、旧时钟拒绝、缺失日志拒绝及原生 probe 前置条件。
- 两端 sdist→wheel 和干净安装均通过，MOD import_only/默认关闭/无引擎及 torch 导入断言保留。
- 原生 helper 21 passed，原锁 release 构建完成（11m15s）；实际恢复两组共六阶段通过，default-off/online/dynamic/观察 ACK 丢失/stream 均通过。初版取消使用时间期限，不能单独证明“提交先于取消”，已改明确双向屏障，最终需重建复验。

## 未完成与下一步

本地 owner 锁限定规范化本地路径，不声明多主机、NFS 或硬链接别名 fencing；不能代替旧 Router 停止证明。真实 HUST 入口/终态、现场 instance/健康、任意并发 kill 恢复、长期 GC、真实 KV 订阅及生产启停仍未完成，没有 NPU 或性能收益结论。

自审修正完成，独立人工审查待完成，PR 不自动合并。用户要求本轮先审查，等待下一次 NPU 分配。下一工作包按[单后端清单](../docs/planning/hust-first-real-chain.md)执行：1 张 NPU/公共 Qwen14B，真实入口关联与终态→真实 KV 订阅→接口/交付收敛；两副本后续 2 张，不继续增加算法。

## 设计与复现入口

见[ADR](../docs/decisions/2026-10-13-router-process-recovery.md)。统一 Python 测试入口不变；实际原生 probe：

```bash
python -m tools.router_recovery_probe --binary <本次构建的prefix_extension_probe> --output <新的artifacts目录>
```

`tools.recovery_receiver_probe --resume` 仅供 CPU supervisor 使用；调用方须先停止它拥有的旧 Router。fresh 身份/健康为 fixture，不能把本报告当成真实 HUST 自动恢复验收。

## 已遇到并处理的验证问题

- 首次格式检查发现长行，统一格式化后 CPU 检查通过。
- 本地 `python -m build --no-isolation` 因测试 venv 未安装 hatchling 失败，改用此前已有的独立 uv CLI 和离线隔离构建后成功；未修改全局或服务依赖。
- Linux 的 uv CLI 不在当前 PATH，改用已有 Python build 的隔离临时构建环境，hatchling 1.32.4，构建/干净安装通过；不是 HUST 服务依赖冲突。
- 一次 bundle fetch 在传输完成前执行，安全失败 early EOF；等待传输结束并比对 SHA256、bundle verify 后同步成功。没有覆盖工作树或放宽 Git 校验。
- 自审修复恢复 transcript 的额外读取上限，复用现有完整验证器；补超限/缺失反例。取消测试从固定延时改为提交→通知→取消→释放确认的双向屏障，避免仅靠时间估计先后顺序。初次追加屏障测试时位置错误被 Ruff/pytest 捕获，修正后 11 项新增测试通过。
- 构建保留原提交已有 deprecated 警告；兼容实验默认构造器在新宿主 example 未被使用，产生一条 dead_code 诊断。该构造器仍被旧 helper 测试使用，实验 source API 暂保留；没有因此更改原依赖或放宽版本。此 example 尚非生产制品，正式 API 收敛时一并清理。
