# Router 进程故障与单 owner 恢复 CPU 验证

日期：2026-10-13。状态：实现及验证中；延续已有恢复/CPU 接线范围，生产 ABI 不变。

## 问题和证据

上一阶段分别证明动态准入、准入 RPC 取消和显式账本交接，尚未在同一实际 Router/接收器进程链上组合验证。DurableLedger 的 SQLite 写事务只保护提交，没有阻止两个进程分别持有陈旧的 ReferenceLedger，因此不能作为完整单 owner 约束。实验使用固定 clock/request/attempt 标识，也不能直接跨 Router 重启复用。

## 决定

- 为工具账本增加规范化路径旁的本地排他锁，创建/重放/事务期间持续持有；打开失败和关闭释放，异常进程退出由 OS 释放。锁文件不删除，避免新旧 inode 的锁同时存在。
- 限于本项目隔离的本地文件系统，不声明 NFS、多主机、恶意锁文件替换或硬链接别名隔离。此锁约束账本 owner，不证明旧 Router 停止；两项分别验证。
- 实际 Router example 可显式配置新 clock 与请求命名空间。原模式保留默认值，故障选项显式开启；准入与观察使用同一运行时时钟，旧记录保持原时钟。
- 复用同一 AdmissionReceiver、DurableLedger 和成熟 Router，增加有限 CPU supervisor：每阶段实际等待旧 Router 退出并检查其进程组，再停止/故障终止该阶段的接收器，然后恢复同一日志。不是新生产服务。
- 组合覆盖提交后丢准入确认、取消等待、接收器异常退出、保留旧债的新运行时准入、再次重启后的额度拒绝及旧观察幂等。CPU 故障接收器在注入确认失效时暂停新准入，已有观察仍能接收。

## 范围与代价

不修改 Router 三文件宿主 patch 或原 Cargo 依赖，只改本项目实验 helper/example 和 Python 工具。原生发送门、默认关闭、stream 和旧在线模式需要回归。SQLite/loopback/阻塞 RPC 仍为语义实验，没有选择为生产热路径。

增加一个常驻的 `.owner` 小文件和一个 OS 文件锁，不引入轮询设备或后台线程。CPU supervisor 的工作进程/网络仅在显式执行工具时创建；MOD 保持 import_only。

实验 fresh WorkerState/instance 身份仍为 fixture；实际 HUST 服务身份、终态安装、长期 GC、真实 KV 订阅及生产自动启停未完成。接收器被终止发生在旧 Router 已退出后，不声称覆盖任意并发 kill 或设备任务终止。结果按[验收记录](../../evidence/router-process-recovery-2026-10-13.md)更新，不能继承历史成绩。

## 下一步

本组合验证通过后，优先把固定 HUST 的真实入口关联和逻辑终态接入同一账本，再接既有 publisher 与目录。真实单后端使用 1 张 NPU；两副本后续 2 张。安装前列明只读适配面、版本守卫、默认关闭、回退与开销检查，按现有用户授权范围执行；需要扩大范围再讨论。
