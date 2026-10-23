# 新实验服务器操作基线

2026-10-23 后续：新增候选用户入口 `prefix-routing-mod manager-run --drain-seconds 30 --shutdown-grace-seconds 35 -- /absolute/router /private/config.json serve`。它只复用固定 Manager 监督显式 Router 命令，不启动或停止 HUST backend；grace 必须至少比 drain 多 1 秒。固定宿主和真实双 NPU 尚未资格，因此该命令是候选启动入口，manifest 仍不可 enable。范围与下一次实机步骤见[记录](../evidence/source-lifecycle-manager-candidate-2026-10-23.md)。

2026-10-23 service 增量：CPU 资格入口和边界见[记录](../evidence/service-lifecycle-cpu-2026-10-23.md)。Router drain 使用显式预算，Manager grace 必须另加清理余量；Manager 收到 SIGTERM 返回 143，需结合 Router 0/75/70 和保债状态判定。

2026-10-23 后台资格入口：在已验证的 `tools.hust_runtime_chain --execute ... --durable-store --managed-terminal-retirement --use-terminal-driver` 上显式增加 `--background-terminal-driver`，使用全新输出目录。它运行每来源唯一后台任务，HTTP 侧只等待进度；仅接受独立资格模式，拒绝与未验证的恢复/churn/控制故障组合。具体制品、实测状态和边界见[本轮记录](../evidence/background-terminal-tasks-2026-10-23.md)。它仍是有限实验，不是正式 Manager 启动命令。

2026-10-22 诊断入口：当前 ced6857/b47795c 候选的发行名来自 `hust-cedb477-native-v1` 映射，其中 Ascend 名为 `vllm-ascend`；旧归档的 `vllm-ascend-hust` 不可写死到新候选诊断。使用 `tools/inspect_runtime.py --execute --profile hust-cedb477-native-v1 --output <新私有报告>`，先验证固定版本和接口指纹再显式导入。报告保留注册后端目录，同时按已安装 Triton 的 `driver.is_active()` 检查唯一 Ascend 可激活后端；目录含 AMD/NVIDIA 不等于实际使用这些后端。该报告仍不替代完整依赖审计、设备计算或容量验收。旧模式省略 profile 只保留旧归档库存查询，不能作为当前候选配方。

2026-10-17 当前操作入口：[接手指南](项目接手指南_2026-10-17.md)与[progress](progress.md)。本轮已核验 aarch64、两张910B2（本次物理4/5）、8核CPU配额/64GiB；使用项目隔离 HUST ced6857/b47795c 与 Qwen2.5-14B FP16/eager、双TP1。重分配后重新检查设备、路径与来源。已有公开HTTP全组件和真实持久commit→ACK有限资格，实验已正常清理；生产恢复/回收与完整MOD启停仍未通过。

历史分配（2026-10-11）：CPU-only aarch64、16核CPU配额、64GiB RAM、16GiB /dev/shm、0 NPU。该资源与下方旧版命令不描述本轮机器；项目目录仍为 /root/work/prefix-routing-mod。

此前最近一次 NPU 分配为 4 张 Ascend 910B2，物理枚举 0/1/2/7、每张 HBM 65536 MiB、128 GiB RAM、32 GiB /dev/shm，身份/hook 主回归只使用逻辑 0。该分配及更早的卡映射、磁盘和四卡测试均为历史记录，不能用于当前操作。下面真实推理命令仅在新 NPU 分配核验通过后执行。

最新执行转入[HUST 隔离候选](decisions/2026-10-11-hust-runtime-candidate.md)，原预装环境仅保留历史对照。临时源码及服务环境位于项目私有 artifacts；不使用旧项目、不改全局 Python/CANN。任何环境变量调整仅限本次进程，须在真实推理中验证后才能成为恢复配方。

## 存储和权限

- 项目唯一目录 /root/work/prefix-routing-mod；旧 /root/work/prefix-router 不在本项目修改范围。
- /root 是持久卷；代码、虚拟环境、模型私有缓存、证据保存在这里。停机保留，删除工作区会丢失。
- /data/shared_models、/data/shared_datasets、/models 作为只读输入。
- /data/shared_upload 是公共共享区，不放私有代码、密钥或实验原始日志。
- /opt、/usr/local、/tmp 不作为唯一持久副本。
- 禁止改驱动、全局 Python、设备配置或清理其他任务。访问地址/凭据不进入 Git。

平台规则：完整一小时 AI Core 无负载会自动 Stop。不得通过空转计算绕过；CPU 开发尽量本地完成，及时推送/保存。恢复后重新核验环境，不假定临时系统安装仍在。

## 安装与检查

```bash
cd /root/work/prefix-routing-mod
python3 -m venv .venv
.venv/bin/python -m pip install uv==0.11.19
.venv/bin/uv sync --frozen --extra test
.venv/bin/python tools/collect_environment.py --output artifacts/environment-venv.json
python3 tools/collect_environment.py --output artifacts/environment-system.json
.venv/bin/python tools/check.py --suite cpu
.venv/bin/python tools/check.py --suite integration
```

环境收集只读、不导入 torch/引擎；包清单仅代表运行该脚本的 Python 环境，必须区分系统与项目 venv。不自动安装或覆盖当前推理栈。

私有仓库首次可通过本地已验证提交的 Git bundle 传入并克隆，随后设置 GitHub origin；这不在服务器保存个人 token，也不意味着后续 git pull 已获得认证。以后可继续 bundle 传递经审查提交，或由用户选择专用最小权限凭据。

### 本次网络限制与可重复恢复

服务器到 GitHub 的 HTTPS 连接超时，但 PyPI 下载成功。固定 Manager 的完整历史 bundle 存在 /root/work/.prefix-routing-bootstrap/extension-manager-complete.bundle，已导入同目录 extension-manager-complete.git，pinned 分支指向 00d1b940dc6ce8d60e2cd28708b80d80e9c33a09。若 Git 缓存需重建，可在本项目目录执行：

```bash
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0=url.file:///root/work/.prefix-routing-bootstrap/extension-manager-complete.git.insteadOf \
GIT_CONFIG_VALUE_0=https://github.com/vLLM-HUST/extension-manager.git \
.venv/bin/uv sync --frozen --extra test
```

这是单次进程的来源镜像映射，版本仍由 uv.lock 的 commit 校验；不改变全局 Git 配置。创建 bundle 时必须包含完整对象历史且实际导入指定 ref；浅克隆生成的 bundle 不足以保证异机可还原。辅助目录也是私有 /root 持久存储，不应放共享区。

历史系统推理包：torch 2.10.0+cpu、torch-npu 2.10.0.post4、vllm 0.23.0+empty、vllm-ascend 0.23.0，CANN 9.1.0。项目 .venv 与隔离 HUST 服务环境分别记录；当前统一恢复入口已通过，见[环境恢复](runtime.md)。预装包保留，当前源与测试范围见兼容矩阵。

## 实验前检查

1. 预约独占时段、核验设备可见性；本次首先使用 mask=0，物理映射重新核验，不能继承旧物理 1 的映射记录。
2. 首先单卡单副本冒烟，再两个 TP=1 副本；TP=2 只有一个路由目的地。
3. 按用户要求选择 `/data/shared_models/Qwen2.5-14B-Instruct`。当前已验组合为 FP16、TP=1、eager，1024 上下文仅用于功能资格；BF16/图/8k与性能调优另验。不下载重复权重，不静默降为7B，不依据目录名断言 revision。
4. 采集驱动/CANN/torch_npu/引擎、模型/模板版本和 APC 配置；当前验证 HUST 隔离候选，预装局部 eager 成绩不继承。图、完整恢复及性能均须后验。
5. 本轮 CPU 为8核时间配额，不等于 cpuset 或独占物理核；核验 cpu.max、cpuset、实际亲和与平台限制。单副本用1卡，双TP1联调/比较用2卡；扩展设备规模另行申请与测量。压测端/Router/后端竞争必须测量，不外推跨节点收益。
6. 仅绑定明确的监听地址和端口；未经授权不对公网开放服务。

新实例必须保留镜像组合好的完整 CANN `PYTHONPATH`（至少包含 `cann-9.1.0/python/site-packages` 与 TBE 路径）。禁止把它覆盖为单一 TBE 目录：这会使已存在的 `acl.so` 无法导入，且只会在昂贵的 EngineCore 启动阶段失败。下列命令只清理外层虚拟环境指针，不重写镜像的 CANN 搜索路径；运行前仍需用 `tools/inspect_runtime.py` 核验来源。

## 可选 publisher 实验与关闭回退

仅在完成固定来源/依赖审计的项目服务 venv 中安装本次已验证 wheel，不用全局 pip，不把开发工具依赖装入推理环境。普通启动不设置 opt-in，原生 publisher 保持不变。以下入口自行给它拥有的临时服务设置显式开关和 allowlist，不修改当前 shell 或现有服务：

```bash
cd /root/work/prefix-routing-mod
env -u PYTHONHOME -u VIRTUAL_ENV \
PYTHONPYCACHEPREFIX=/root/work/prefix-routing-mod/artifacts/g2-recovery-20261011/new-server/pyc \
artifacts/w1-restore-20261011/service-venv/bin/python tools/regress_kv_identity.py \
  --execute --executable artifacts/w1-restore-20261011/service-venv/bin/vllm \
  --model /data/shared_models/Qwen2.5-14B-Instruct --device 0 --port 18324 \
  --core-source artifacts/w1-restore-20261011/core \
  --ascend-source artifacts/w1-restore-20261011/ascend \
  --pycache-root artifacts/g2-recovery-20261011/new-server/pyc \
  --output artifacts/g2-identity-next/hook --publisher-hook
```

路径是本机已审计环境示例，运行前重核；输出不能已存在，端口须空闲。此配置只用 1024 context/1 GiB KV/4 批窗口/eager 来放大事件与恢复边界，不是最终性能配置。测试会有限次重启自己创建的模型服务，reset dev API 仅在临时 loopback 进程中开放。

关闭回退：移除 `PREFIX_ROUTING_HUST_PUBLISHER=1`（或设 0）并从 general plugin allowlist 移除 `prefix_routing_publisher`，重启自己的服务；不能热切换已经实例化的 publisher。不带 `--publisher-hook` 的身份入口复验原生三帧协议。新旧协议不得混用，已有消费者必须重新建立连接/代次/已知起点；无可信恢复时不继续使用旧目录。普通用户暂不推荐部署此实验 hook，生产交付与开销验收尚未完成。

## 单后端局部探测（显式手动触发）

本轮复现全新 Python 字节码缓存的 Torch 导入约 22.36 秒，超过 HUST CLI 20 秒预检；已生成缓存的诊断约 10.75 秒。它与模型 KV/图预热无关。连续运行同一已审计服务 venv 时，可显式指定项目私有 `--pycache-root`，复用准备阶段的 bytecode；默认仍为每次 probe 的独立缓存，报告记录模式与路径。先在该缓存下执行 tools/inspect_runtime.py 的显式导入及来源审计，再用于启动；软件/解释器变更重新审计，不能将该准备流程写成冷启动性能。

```bash
env -u PYTHONHOME -u VIRTUAL_ENV \
PYTHONPYCACHEPREFIX=/root/work/prefix-routing-mod/artifacts/runtime-ready-next/pyc \
  artifacts/w1-restore-20261011/service-venv/bin/python tools/inspect_runtime.py \
  --execute --output artifacts/runtime-ready-next/imports.json

env -u PYTHONHOME -u VIRTUAL_ENV \
  artifacts/w1-restore-20261011/service-venv/bin/python tools/smoke_backend.py \
  --execute --executable artifacts/w1-restore-20261011/service-venv/bin/vllm \
  --model /data/shared_models/Qwen2.5-14B-Instruct --device 3 --port 18247 \
  --kv-events-ipc --pycache-root artifacts/runtime-ready-next/pyc \
  --output artifacts/card-check-next
```

该选项仅接受实际项目 artifacts 子目录，拒绝逃逸和模型写入；图缓存、KV 与日志仍按各 probe 隔离。原预检保持启用及 20 秒，不修改 HUST、驱动或依赖。

恢复故障回归同样使用已审计的 HUST venv。输出为新的项目私有目录；其中两次服务启动共用本次 HTTP/IPC 地址，分别保存日志和缓存。该工具不接生产 Router：

```bash
env -u PYTHONHOME -u VIRTUAL_ENV \
  artifacts/w1-restore-20261011/service-venv/bin/python tools/regress_kv_recovery.py \
  --execute --executable artifacts/w1-restore-20261011/service-venv/bin/vllm \
  --model /data/shared_models/Qwen2.5-14B-Instruct \
  --device 0 --port 18242 --output artifacts/g2-recovery-20261011/run-next
```

它覆盖受控空起点、三批重放、实际缓冲重复帧、SUB 断连补齐、空闲确认、超窗拒绝恢复、停止后的控制请求超时和同地址重启。缺少一致边界的快照保持 unknown；声明范围见[事件合同](kv-event-contract.md)。逐卡独立检查使用不同 HTTP 端口，绑定预检失败时保存记录并确认监听状态，不能直接认定为 NPU 故障或清理未知进程。

此前分配已完成四张设备的串行真实模型烟测；当前 CPU 机器未运行推理。历史环境恢复与四卡结果见 [W1](../evidence/w1-runtime-restore-2026-10-11.md)，真实存储/淘汰/清空/重放子集见 [G2](../evidence/g2-native-events-subset-2026-10-11.md)。所有当时临时服务已关闭，不通过空转维持在线。

恢复环境下事件子集示例，运行前确认卡/端口可用及新输出目录：

```bash
env -u PYTHONHOME -u VIRTUAL_ENV \
  artifacts/w1-restore-20261011/service-venv/bin/python tools/regress_kv_events.py \
  --execute --executable artifacts/w1-restore-20261011/service-venv/bin/vllm \
  --model /data/shared_models/Qwen2.5-14B-Instruct \
  --device 0 --port 18231 --output artifacts/g2-events-20261011/run-next
```

该工具只在自有 loopback 服务进程开启 reset 所需 dev API；采用专用小 KV 配置产生自然淘汰，不能作为最终性能配置。完整事件恢复、断连/重启和精确索引仍未通过，见[事件合同](kv-event-contract.md)。

最新 HUST 隔离探测示例（目录/端口每次新选；先确认逻辑设备和无人争用）：

```bash
cd /root/work/prefix-routing-mod
env -u PYTHONHOME -u VIRTUAL_ENV \
  artifacts/hust-runtime-20261011/service-venv-rebuild/bin/python \
  tools/smoke_backend.py --execute \
  --executable artifacts/hust-runtime-20261011/service-venv-rebuild/bin/vllm \
  --model /data/shared_models/Qwen2.5-14B-Instruct \
  --device 0 --port 18180 --kv-events-ipc --startup-timeout 600 \
  --output artifacts/hust-runtime-20261011/probe-next
```

此次 private IPC 路径长度必须 <=100 bytes，过长明确拒绝；PUB bind/SUB connect。新工具允许字节码但统一重定向至 output/cache/pyc，保留 HUST preflight。普通 ZMQ 存储样本不代表全量订阅覆盖、重放、代次或恢复已通过。端口检查失败不清理未知进程；先核验，再换端口/等待正常释放，报告不覆盖。

更新注意：以下为早期探测命令，不能当成已验收环境/事件配方。当前先完成[环境门槛](runtime.md)；原 TCP SUB-bind 接线未收到事件，已有单独 IPC 正向诊断，但还没纳入本工具的可重复恢复回归。勿依据下面 `--kv-events-port` 参数宣告事件能力失败或成功。G1 通过后按[事件合同](planning/runtime-event-gates.md)修正正式回归入口。

核验设备未被占用后，在项目目录执行以下示例；输出目录必须是新的。工具只启动临时 loopback 服务，完成后关闭自己创建的进程组，不是部署管理器。没有跨操作者的设备锁，运行前仍须预约；不得并行争用同卡。

```bash
python3 tools/smoke_backend.py --execute \
  --executable /usr/local/python3.12.13/bin/vllm \
  --model /data/shared_models/Qwen2.5-14B-Instruct \
  --device 0 --port 18140 --kv-events-port 18141 \
  --startup-timeout 600 --output artifacts/qwen14b-probe-new-run
```

固定参数：TP=1、BF16、APC、chunked prefill、eager、8192 context、8 sequences、2048 batched tokens、0.70 memory utilization。它们用于功能探测，不是已调优参数。只加载 Ascend 必要插件，关闭使用统计，缓存保存在本次 artifacts；不写公共模型目录。显式 `enable_cpu_binding=false`：本机 Ascend 默认绑定路径可能操作 IRQ/irqbalance，未获得主机级授权不能照用。`OMP_NUM_THREADS=2` 仅限本次进程，不代表性能最优。

报告包含脱敏请求统计、原生事件摘要与进程清理结果；原始后端日志仅留忽略目录。`pass` 只指该局部探测；`shutdown_validated`、`kv_events_validated`、`routing_validated`、`performance_validated` 保持 false，完整语义另验。事件的 medium 字符串即使为 GPU，也不能据此认定设备是 NVIDIA。

Ascend 性能验证遵循 [N0–N6](ascend-design.md)：图捕获预热与 KV 预热分开，记录有效 KV blocks 和 workspace；不要从 HBM 总量反推并发承诺。CPU 绑定仅限已授权进程及容器允许范围，不运行文档中的全局 IRQ/irqbalance 修改。npu-smi 和 profiler 用于受控诊断，不进入请求热路径；不写空转计算来维持服务器在线。

本轮已按授权同步开发分支并进行局部真机探测；尚未合并主分支。见[证据](../evidence/qwen14b-preflight-2026-10-11.md)。不改引擎源码、全局环境、公共权重或旧项目。
