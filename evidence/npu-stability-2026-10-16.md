# 双 TP1 Qwen14B 有限稳定性复验

日期：2026-10-16。结论：新分配两张 910B2 的 20 分钟后端推理稳定性通过；不是 Router 容量、终态留存或策略收益成绩。

## 环境与负载

固定 HUST Core ced6857 / Ascend b47795c，项目 rebuild 服务隔离环境；去除平台注入的全局 PYTHONPATH 后 pip check 无冲突，未调整 HUST、CANN 或系统 Python。两张可见物理设备为 0/7，各 65536 MiB，8 核 CPU 配额与 64 GiB Host 内存。公共区 Qwen2.5-14B-Instruct，FP16/eager、TP1/DP1、block128。

`tools.npu_stability_probe --duration-seconds 1200` 复用现有 OwnedBackend 与启动/清理。两副本均就绪后同时运行，每副本一条在途请求，循环实际生成 16/64/128 tokens，ignore_eos=true 保证输出预算被执行，重复公开合成前缀。只保存有界计数和总请求耗时，不保存生成内容。最后一个已发请求允许在期限后完成。

## 实际结果

原始报告位于服务器项目私有 `artifacts/s5/report.json`，状态 pass，实际持续 1209.67 秒。

报告 SHA256：`a6ae093e954d33ba42601ac7ac42008614ef2b2f86d6d44ec0b58a1ae2936082`。本地取回副本已核对一致；同机较早 `r5/report.json` 的 SHA256 为 `b2771cbcfcb8e982fc0708d5c33bb02b32230c73c4662e8616c0a8d844b4a137`。

| 副本 | 完成请求 | 实际生成 token | 报告中的累计 cached_tokens | 最大整请求耗时 |
|---|---:|---:|---:|---:|
| 0 | 177 | 12272 | 67584 | 13.20 s |
| 1 | 171 | 11856 | 65280 | 13.43 s |

运行中一次低频设备采样，AI Core 分别为 51% / 47%；这是设备快照，不是全程利用率统计。两个服务父进程退出码均为 0，未强制清理；结束后两卡没有本项目残留设备进程。

本次明确未启用 terminal observer；它验证固定后端持续真实计算，不绕过当前观测留存上限去宣称组件可长期服务。graph、请求并发容量、TTFT/生成停顿曲线和 Router 性能均未由本实验验证。负载测试期间还运行了有限 CPU 构建，因此整请求耗时只作为稳定性诊断，不作为独占性能基线。
