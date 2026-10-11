# G2 断连、重放与真实同地址重启回归

日期：2026-10-11。代码：53203ba3cf5e854a8ba3fb389d8f3ca908e682a0。范围：同机 IPC、受控空起点的事件连续性；完整生产恢复与性能尚未通过。

## 新分配服务器与环境

仓库、源码和服务 venv 保留在项目 /root 持久目录，源码无修改。实际导入 core/Ascend 分别来自 w1-restore-20261011 下固定提交 13f11da / 3a61053。新分配四张 910B2，物理 1/2/3/4 对应逻辑 0/1/2/3，65536 MiB HBM/卡；Python 3.12.13、aarch64、16 核 CPU 时间配额、128 GiB RAM。不能继承上一分配物理映射。

依赖审计、Ascend RECORD 覆盖和实际导入/加载库检查均通过；实际 Triton backend 只有 ascend。未重新安装环境或改引擎。逻辑卡 0 的原生命周期子集在新机器复测 pass；详细文件保留于 artifacts/g2-recovery-20261011/new-server 和 server-lifecycle。

## 新增真实恢复子集

tools/regress_kv_recovery.py 在逻辑卡 0、Qwen2.5-14B-Instruct、TP=1/BF16/eager、1 GiB KV/1024 context/4 批重放窗口下运行，报告 run01 为 pass。这是放大故障的专用配置，不能作为性能基线。

| 项目 | 实际观察 |
|---|---|
| 受控空起点 | 自有新服务无模型请求时，next_seq=0、快照只含清空标记；起点依据自有生命周期建立 |
| 多批缺口 | 实时消费暂停后，完整重放序号 1、2、3 |
| 缓冲重复帧 | 同一 SUB 实际收到已重放的三批，全部判 duplicate，游标不重复推进 |
| 显式断连 | 关闭旧 SUB，生成缺失事件，重新接入后完整重放 4、5 |
| 正常空闲 | 控制面确认同 epoch，重放区间为空，连续性仍成立 |
| 超窗恢复 | 缺口超过四批，快照 next_seq=12；结果 snapshot_unknown，撤销已知完整起点 |
| 服务停止 | 控制请求真实超时，之前连续的检查点按 5 秒测试租期过期；该数值不是生产推荐阈值 |
| 同地址重启 | 共用本次 HTTP 与两条 IPC 地址，两次 publisher epoch 不同；旧起点先失效，新自有空服务重新建起点后收到真实存储事件 |
| 旧连接帧 | 显式本地合成注入返回 old_connection；不能宣称原生实时协议含 epoch |
| 清理 | 两次服务 parent_returncode=0，forced_group_cleanup=false |

报告仅 stream_continuity_validated=true；snapshot_boundary_validated、identity_validated、complete_recovery_validated、performance_validated 均 false。不持有生产驻留树，MOD 保持 import_only。

## 四设备检查

逻辑 0 已通过生命周期和新恢复子集；逻辑 1 已通过普通、重复、SSE 和真实 IPC 存储，正常退出。逻辑 2 首次在模型启动前因 HTTP 端口绑定冲突失败，保留 card-2/summary.json；独立端口 card-2-retry 已通过、正常退出。逻辑 3 首次在 HUST 20 秒 preflight 超时，保留失败及 parent_returncode=1；bytecode 适配后的 card-3-retry 已完成普通、重复、SSE 和真实 IPC 存储，pass、正常退出。没有将启动前预检失败归因为 NPU 或依赖问题。

本轮四张卡均实际使用并验证；逻辑 3 的成功包含显式共享私有 bytecode 准备条件，与前三张的默认 probe 缓存分别记录。最终 npu-smi 四卡健康 OK、无运行进程，core/Ascend 源码仍清洁。没有同时启动四副本或宣称冷启动全条件通过。

逻辑 3 的分段诊断：已生成私有 bytecode 的总计 10.7526 秒；全新 bytecode 总计 24.0078 秒，其中 torch 导入 22.3630 秒（含自动加载 torch_npu），设备选择 1.2842 秒、分配并同步 0.3606 秒。冷导入单项已超过原预检期限；单次设备诊断成功不代表完整模型通过。两次测量是启动诊断，不是性能基线或普遍时延保证。

fbfb47155f89164e3698af4ad75b7fe2c01f8d93 增加 smoke 的显式 --pycache-root：仅允许项目私有 artifacts，环境来源审计后复用同一 venv 的 bytecode；默认不变，报告模式/路径，图/KV/日志仍隔离。没有禁用预检或扩大 HUST 20 秒期限。CLI 配置 CPU 探针同时实证自定义 publisher 被 ValidationError 拒绝、KVEventBatch 无 epoch 字段；没有更改注册表。

逻辑 3 成功报告的 pycache_mode=shared_private、pycache_root 指向本次 new-server/pyc；三次请求各 648 输入/24 输出 tokens，真实事件 5 块/640 tokens，parent_returncode=0、forced_group_cleanup=false。该结果对应 fbfb471；恢复 run01 对应 53203ba，不将未复跑的新增缓存参数成绩混入恢复子集。

## 确定性协议审计与适配判断

使用固定源码真实 publish/_build_replay_response 方法再次验证，尚未发布的存储和移除已进入镜像，next_seq 仍为 1。新增反例实际消费队列中的存储后，快照已淘汰的哈希被暂时重新引入，直到后续移除消费后收敛。两个反例标志均 true。它是合成 CPU 方法审计，不是实际错误路由或性能损失测量。

固定 KVEventsConfig.publisher 只接受 null/zmq；register_publisher 不等于可以配置自定义 publisher。默认缓存哈希为 xxhash，NONE_HASH 未指定 PYTHONHASHSEED 时随机初始化。后续必须验证请求哈希、首块 seed、group/模板及 Router 表示方式，不能照搬官方默认。最小共同水位修正候选见[ADR](../docs/decisions/2026-10-11-g2-recovery-regression.md)，尚未应用引擎补丁。

## 测试与审查

恢复代码 53203ba：Windows CPU 127 passed、2 个权限相关 symlink 用例 skipped；Linux aarch64 CPU 129 passed；两端 mock 3 passed、sdist/wheel 和干净 wheel 检查通过。bytecode 适配代码 fbfb471：Windows CPU 129 passed/2 skipped，Linux aarch64 CPU 131 passed，mock 各 3 passed。本地构建/干净 wheel 与 Actions 3.11/3.12 对 fbfb471 均通过；aarch64 构建复验成绩对应 53203ba，包内运行代码未因此修改。

自检重点：先完整解码再推进游标、异常撤销连续性、重复内容校验、恢复窗口/消息/摘要上限、旧连接隔离、控制确认时效、私有输出拒绝覆盖、自有进程清理和正常退出。单元测试在开发中暴露的过长参数用例名与不完整测试报告字段已修正并复测。原硬件失败报告均保留。

[PR #7](https://github.com/WMASTER123/prefix-routing-mod/pull/7) 为叠在事件子集 PR #6 上的草稿，人工独立审查待完成，未合并。CI 通过不代替人工审查。

## 下一步

G2 仍开放：优先冻结精确 hash/group/语义身份和候选 Router 的索引表示，再给出共同水位及实时 epoch 的最小适配实现与对抗测试。已热服务冷接入、重启检测前迟到消息、跨节点/legacy 声明范围尚未通过。完成后才进入 G3 图/并发/取消/持续运行，随后 G4 两副本。
