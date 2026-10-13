# 实际 Router 进程故障与恢复验收

日期：2026-10-13。基础提交 2177297，分支 codex/router-recovery-faults。状态：初版组合通过，审查修正后的最终验证进行中。

## 已完成

- 初版 89b2662：Windows CPU 446 passed、4 skipped；Linux aarch64 CPU 450 passed；integration 两端各 3。
- 初版新增 9 项 Python 测试：单 owner 打开/失败清理/真实进程正常或异常退出，真实接收子进程准入确认丢失/调用方断开/新时钟恢复、旧时钟拒绝、缺失日志拒绝及原生 probe 前置条件。
- 两端 sdist→wheel 和干净安装均通过，MOD import_only/默认关闭/无引擎及 torch 导入断言保留。
- 原生 helper 21 passed，原锁 release 构建完成（11m15s）；实际恢复两组共六阶段通过，default-off/online/dynamic/观察 ACK 丢失/stream 均通过。初版取消使用时间期限，不能单独证明“提交先于取消”，已改明确双向屏障，最终需重建复验。

## 尚待本次执行

审查修正后的两端全量、完整 Router release 与实际 HTTP 恢复组合。没有 NPU 或性能结论，代码自审与另一成员独立审查分别记录。用户本轮要求先审查，真实 HUST/NPU 接入等下次提供服务器。

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
- 构建原提交保留其已有 deprecated 警告，不修改原依赖掩盖问题。新 default 构造器只在 helper 测试使用，完整 example 的 dead_code 警告需在最终审查收敛。
