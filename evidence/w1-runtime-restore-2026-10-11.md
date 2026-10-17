# W1 运行环境恢复与四卡真实验收

日期：2026-10-11。类型：隔离恢复、四设备真实局部功能、CPU/制品及代码自检。完整 KV 恢复、图/并发、Router 和性能不在本页通过范围。

## 恢复结果

代码提交 f41bdfc099b4a0af2942c08375f658d809b752b3，通过统一 tools/restore_runtime.py 入口在全新 artifacts/w1-restore-20261011 恢复。两个源使用完整本地历史新克隆，固定提交与 [HUST 候选](hust-runtime-candidate-2026-10-11.md)一致；缓存、源码、venv 和原始日志均为项目私有，原通过环境与系统环境保留。

13 个步骤全部通过：双源码克隆/固定、venv、186 包 hash 安装、Ascend 文件覆盖、双 editable 构建、依赖审计、RECORD 审计、实际导入/库清单及模型烟测。依赖安装 70.098 秒，core 构建 39.523 秒，Ascend 构建 9.077 秒，烟测 167.080 秒；这些为一次工程执行耗时，不是性能比较。

Triton Ascend 的 92 个 RECORD 文件全部符合 SHA，含 35 个共用文件；实际 backend 只有 ascend。依赖审计通过、两个引擎实际导入路径指向新克隆。已记录驱动 26.0.rc1、CANN compiler/OPP/toolkit 9.1.0 的版本文件、继承动态库搜索路径和实际加载库路径，私有 JSON 保留细节。Python/aarch64 与来源锁保持既定值。

主模型的 config.json SHA256 为 0f2085dbbe2ee251bd6a6a0797d84a6ce34436044d629aa3cba793b43d311a9e；这是配置摘要，不是全部权重 revision。平台镜像 digest 与模型 revision 未获得，明确为未知。

新恢复环境普通/重复/SSE 请求和真实 IPC BlockStored 全部通过；每次 648 输入 tokens、24 输出 tokens，事件 5 个块/640 tokens/block size 128。临时服务父进程 exit 0，无强制组清理。完整恢复/性能布尔标志保持 false。

G1 的本机研发恢复技术证据已齐，支持继续 G2 测试；正式冻结待人工复核。editable 源构建不等于独立发行 wheel，可移植镜像与第二成员复现仍为发布门槛。

## 四设备真实检查

使用此前验证的 service-venv-rebuild，按逻辑设备 0、1、2、3 串行加载 Qwen2.5-14B-Instruct，物理映射为 0、1、6、7。四个设备分别完成普通、重复、流式与真实 IPC 存储检查；四次均 pass、父进程 exit 0、无强制组清理。

每次使用相同 TP=1/BF16/eager 功能配置，每次均观察到 5 个完整 KV 块。只是逐卡一致性检查，不是同时运行四个路由副本、跨卡通信或吞吐实验。检查后 npu-smi 无模型残留进程，没有运行保活或空转负载。

## 验证与审查

Windows：CPU 90 passed、2 个 symlink 用例因权限条件 skipped；mock 3 passed。Linux aarch64：CPU 92 passed、mock 3 passed。两端 sdist/wheel 和源码目录外干净安装检查通过。上述成绩对应 W1 代码，后续 G2 工具独立记录。

[Actions](https://github.com/WMASTER123/prefix-routing-mod/actions/runs/38127202357) 对 f41bdfc completed/success。[PR #5](https://github.com/WMASTER123/prefix-routing-mod/pull/5) 为 draft，未合并。

已自检来源/锁变更拒绝、安装覆盖顺序、目标路径与进程所有权、超时/中断清理和未实现能力标志；没有第二成员独立审查，不把 CI 通过写成人工审查通过。

## 下一步

按 [W2](../docs/planning/next-execution-plan.md) 执行原生存储、自然淘汰、清空、重放、断连和重启。源码恢复边界的合成测试与真实缓存实验分别留证；缺口确认后才决定窄幅适配，G2 未关闭前不接生产 Router。
