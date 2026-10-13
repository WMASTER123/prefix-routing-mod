# 实际 Router 进程故障与恢复验收

日期：2026-10-13。基础提交 2177297，分支 codex/router-recovery-faults。状态：验证进行中，本文只记录已执行项。

## 已完成

- 本地 Windows CPU：446 passed、4 skipped；integration：3 passed。
- 新增 9 项 Python 测试：单 owner 打开/失败清理/真实进程正常或异常退出，真实接收子进程准入确认丢失/调用方断开/新时钟恢复、旧时钟拒绝、缺失日志拒绝及原生 probe 前置条件。
- 本地 sdist→wheel 构建成功；干净安装验证仍执行中。

## 尚待本次执行

Linux aarch64 全量、原生 helper、完整 Router release、真实 HTTP 恢复组合以及 default-off/旧在线/stream 回归。没有 NPU 或性能结论，代码自审与另一成员独立审查分别记录。

## 设计与复现入口

见[ADR](../docs/decisions/2026-10-13-router-process-recovery.md)。统一 Python 测试入口不变；实际原生 probe：

```bash
python -m tools.router_recovery_probe --binary <本次构建的prefix_extension_probe> --output <新的artifacts目录>
```

`tools.recovery_receiver_probe --resume` 仅供 CPU supervisor 使用；调用方须先停止它拥有的旧 Router。fresh 身份/健康为 fixture，不能把本报告当成真实 HUST 自动恢复验收。

## 已遇到并处理的验证问题

- 首次格式检查发现长行，统一格式化后 CPU 检查通过。
- 本地 `python -m build --no-isolation` 因测试 venv 未安装 hatchling 失败，改用此前已有的独立 uv CLI 和离线隔离构建后成功；未修改全局或服务依赖。
- 构建原提交保留其已有 deprecated 警告，不修改原依赖掩盖问题。新 default 构造器只在 helper 测试使用，完整 example 的 dead_code 警告需在最终审查收敛。
