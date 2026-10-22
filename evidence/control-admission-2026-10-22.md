# 控制新鲜度与准入闭环（2026-10-22）

固定输入：HUST Core `ced6857afa0ea7b2e3f0846a62e1394e90f15607`、Ascend `b47795c63a8e63e7aed4d311f951428d94e045ab`、Router `91b65ee4bdfbe98863a32a2205dced5dbb249ffe` 的既有隔离宿主。两张 910B2（本次物理 4/6）、aarch64、8 核时间配额，公共 Qwen2.5-14B-Instruct、FP16/eager、两个 TP1。未修改 HUST/CANN/全局 Python。原始报告在私有 artifacts，本文只保存脱敏结果。

## 代码和边界

`control_gate.rs` 新增进程内 `WorkerGate`：单调期限从控制请求发出前计算；默认关闭；过期/失败改变发送围栏；新同源验证可恢复，来源冲突隔离；drain 不可逆。`runtime_owner.rs` 在 candidates、reserve 和 before_send 三处使用该门。已发/uncertain 债务保持，可靠终态仍可结清。旧 CPU oracle 没有安装该门时保留原行为；真实公开链明确安装。

固定 HUST 源控制原验证复用于独立健康工作线程，区分 `Unavailable` 和 `IdentityChanged`；每后端资格参数为 500ms 刷新、3s TTL。M2 的失信与 M3 的控制门分开，没有逐请求控制 RPC、设备同步或 npu-smi。新期限不写入持久 schema，既有 commit→ACK→retire 顺序不变。这些时限未作为已校准生产默认。

自审发现“先取时间、再加锁”会把两个线程的采样顺序误判为时钟倒退；已改为持有门锁后取时间，补 8 线程×1000 读取反例。关闭前的未发 lease 即使遇到同身份恢复仍被围栏拒绝，不能复活；已发送尝试不因此释放。

## 自动回归

- Windows Python：全量 593 passed、7 skipped；CPU 子集 590 passed、7 skipped、3 deselected。修复下述旧 fixture 后 CPU 子集连续三轮通过，最终统一 `tools/check.py` CPU 再通过；integration 3 passed。sdist/wheel、干净临时环境安装、惰性描述与包内 manifest 验证通过。
- aarch64 Rust helper：68 tests passed；`clippy --all-targets -- -D warnings` 通过，无新增 lint 放宽。其中新增 10 项控制门反例。
- aarch64 原索引/真实 IPC/源控制：`--features hust-control`，93 release tests passed。
- 固定完整宿主：`--features durable-candidate --example hust_runtime_probe`，39 release tests passed；最终 release 构建完成，记录 `BUILD_RESULT=0`。完整宿主仍有上游 deprecation/组合 dead-code warnings，不冒称整库零 warning 或整库 Clippy 通过。

首次全量 Python 收尾暴露既有 retire 故障 fixture 的间歇性 502，记录到 `ConnectionAbortedError`。测试后端未消费 POST 请求体就关闭连接，在 Windows 上可能中断尚未读完的 200 响应。已消费完整请求体、显式关闭响应，并加断言证明首次错误前代理确实丢弃了已提交回复，禁止把普通上游失败误算为回复丢失。代理仅保存最多四个异常类型，不记录 token/原始异常地址；目标 fixture 先单独重复六轮，修复后全量 CPU 再连续三轮通过。首次失败没有计入通过。

本地首次使用 `build --no-isolation` 缺少 hatchling 构建后端而失败；改用标准 PEP 517 隔离构建后通过（hatchling 1.32.4），没有安装/升级全局包或推理环境。这是本地构建命令的前置条件，不是 HUST 运行不兼容。

## 新分配上的原有恢复复验

`artifacts/c22-base` 使用上一阶段保留二进制 `7e71973266df5c4aa3a37c4674af647b5a0c46383fe1eddca5d853bf8a1c8ad6`，重跑真实请求强退、暂停保债、同源恢复、旧 cursor 与实际后端实例替换。通过，旧债和恢复后请求共退役 2 family，持久 cursor `[2,0]`；三个后端均返回 0，无强制组清理。

该复验不包含新增控制门；它与下面新二进制资格分别记录。

## 新控制门双 NPU 资格

`artifacts/c22-health` 使用新二进制，通过真实公开 HTTP、Rust M1–M5/M7、持久账本和真实 HUST 服务执行：

1. 原 A 冷/A 热/B 热仍选择 A/A/B，实际 cached_tokens 为 0/384/384；生成后取消取得 `FINISHED_ABORTED`。
2. 保留 A 上一个已发送、尚未消费终态的债务；回环故障代理对**健康线程的真实身份 HTTP 请求**返回 503（实际 2 次），A 的新准入关闭，后续业务请求在 B 执行。此时 A 的旧债仍在，只有可靠终态和持久提交后才结清。
3. 恢复同一源的真实身份响应后，A 重新通过核验并服务请求。
4. 显式暂停 A 健康线程的刷新，使读取时真正超过 3s 期限，A 不再接新请求，B 继续执行；恢复线程后再次验证。
5. 代理仅修改身份响应的 observer 字段，注入一次冲突；A 隔离，即使代理随后恢复原响应也不能自动开放，业务仍由 B 完成。这是**注入字段冲突**，不是再次启动了真实替换实例；真实实例替换见前一节。
6. 调用所有控制门 drain 后，真实 HTTP 新请求被拒绝，无新增负债。共退役 9 family，最终 active=0；两个后端自然退出、returncode=0、forced_group_cleanup=false；设备检查无遗留进程。

报告 `control_gate_qualification` 七项均 true，且核验代理确实产生 HTTP 失败和身份冲突。不能把零注入场景中的 `retire_response_loss_recovered=true` 当作新增回复丢失资格；该字段只说明实际次数符合配置，真正回复丢失证据仍见 2026-10-18。

复现（Linux 项目私有环境，先核验设备/模型/端口和最终二进制；新输出目录）：

```bash
python -m tools.hust_runtime_chain --execute \
  --model /data/shared_models/Qwen2.5-14B-Instruct \
  --binary artifacts/durable-host-minimal-20261017/target/release/examples/hust_runtime_probe \
  --output artifacts/control-next --durable-store \
  --managed-terminal-retirement --exercise-control-gate --port 18570
```

`python` 指已核验的项目服务解释器；本机复用经审计的 `--pycache-root`，不能据此声称冷启动性能。

## 指纹

| 输入/制品 | SHA256 |
|---|---|
| 新实验二进制 | `f4fa5fe0400b63660a8b1730a9f1d2e17d94ac308f5b6f829c79c6098b40eda6` |
| 新控制门 report.json | `e73b046c875556846056ffea306e801494b1dbbc30bdc40a45f443855fb8a8d6` |
| 原恢复复验 report.json | `07a65319bfb29f0927c86def6706ade1c8dd38e63f611841f680ef0775db07fb` |
| 最终构建/测试日志 | `6eda0c384df2e028939c45fb58a5c47358a6ab3342842fb9c2f02c489c3f2975` |
| 编译 control_gate.rs | `ca9dbb1ce83aa3c2779a3053147aac90a973b07f2ff008389873dd5f668b1eb9` |
| 编译 runtime_owner.rs | `067d0680f58e948852bc7c731ea36a3f221c880a4a3f0ecba28b96089179052a` |

## 后续门槛

本次完成有限公开链上的控制关闭/保债/同源恢复与排空拒绝；没有交付常驻 service、终态后台循环、SIGTERM 排空、Manager 真启停、生产目录迁移、公开配置/错误映射、完整图模式、长期全链或性能收益。MOD 仍 `import_only`，没有扩大兼容范围。下一步按[生产工作包](../docs/planning/production-runtime-and-overhead.md)实施，不再重复搭第二套 Router/索引/账本。独立人工审查未完成，不自动合并。
