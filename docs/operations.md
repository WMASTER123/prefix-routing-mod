# 新实验服务器操作基线

2026-10-11 最新分配服务器：aarch64、4 张 Ascend 910B2，npu-smi 物理编号 0/1/6/7，每张 HBM 65536 MiB；16 核 CPU 时间配额、128 GiB RAM、32 GiB /dev/shm；Python 3.12.13、npu-smi 26.0.rc1、CANN 9.1.0。重新分配后编号已改变，不能沿用此前 1/2/3/4。约 40 GiB 是根卷已用量，安装前剩余约 254 GiB。平台未确认的独占性不能从空闲利用率推断。

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

系统推理包：torch 2.10.0+cpu、torch-npu 2.10.0.post4、vllm 0.23.0+empty、vllm-ascend 0.23.0，CANN 9.1.0。项目 .venv 与它们隔离。用户已选择优先验证预装组合；本次使用原环境执行功能探测，没有安装/替换这些包。版本及测试边界见兼容矩阵。

## 实验前检查

1. 预约独占时段、核验设备可见性；本次首先使用 mask=0，物理映射重新核验，不能继承旧物理 1 的映射记录。
2. 首先单卡单副本冒烟，再两个 TP=1 副本；TP=2 只有一个路由目的地。
3. 按用户要求选择 `/data/shared_models/Qwen2.5-14B-Instruct`，BF16、TP=1、先 8k 上下文；不下载重复权重，不静默降为 7B，不依据目录名断言 revision。
4. 采集驱动/CANN/torch_npu/引擎、模型/模板版本和 APC 配置；当前验证 HUST 隔离候选，预装局部 eager 成绩不继承。图、完整恢复及性能均须后验。
5. CPU 为 16 核时间配额，不等于 cpuset 或独占物理核；核验 cpu.max、cpuset、实际亲和与平台限制。先单卡，再两个 TP=1 副本，最后四副本容量实验。压测端/Router/后端的竞争必须测量，不外推跨节点收益。
6. 仅绑定明确的监听地址和端口；未经授权不对公网开放服务。

## 单后端局部探测（显式手动触发）

最新 HUST 隔离探测示例（目录/端口每次新选；先确认逻辑设备和无人争用）：

```bash
cd /root/work/prefix-routing-mod
PYTHONPATH=/usr/local/Ascend/cann-9.1.0/opp/built-in/op_impl/ai_core/tbe \
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
