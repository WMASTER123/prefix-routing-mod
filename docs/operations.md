# 新实验服务器操作基线

2026-10-10 只读检查：aarch64、2 张 Ascend 910B2，物理设备编号 2/3，每张 HBM 65536 MiB；8 核 CPU 配额、64 GiB RAM；系统 Python 3.12.13、npu-smi 26.0.rc1。平台未确认的独占性不能从空闲利用率推断。

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

本次观察到系统推理包：torch 2.10.0+cpu、torch-npu 2.10.0.post4、vllm 0.23.0+empty、vllm-ascend 0.23.0，CANN 9.1.0。项目 .venv 与它们隔离，不因包名存在就认定可以执行 NPU 推理；后续先确定复用现成环境还是创建独立推理环境。

## 实验前检查

1. 预约独占时段、核验设备可见性；物理编号 2/3 不直接等同容器内可见逻辑号，启动前确认映射。
2. 首先单卡单副本冒烟，再两个 TP=1 副本；TP=2 只有一个路由目的地。
3. 已发现公共 Qwen2.5-7B-Instruct、3B、14B 和 Qwen3-0.6B。首验优先从 7B/3B 中按运行验证选择；不下载重复权重，不依据目录名断言 revision。
4. 采集驱动/CANN/torch_npu/引擎、模型/模板版本和 APC 配置；候选引擎组合尚未验证，先隔离安装方案再启动。
5. CPU 为 8 核时间配额，不等于 cpuset 或独占物理核；核验 cpu.max、cpuset、实际亲和与平台限制。压测端/Router/后端的竞争必须测量。两卡可做功能与初步趋势，不外推四副本或跨节点收益。
6. 仅绑定明确的监听地址和端口；未经授权不对公网开放服务。

当前不包含模型服务启动脚本：必须通过 P1b 版本兼容和设备映射检查后按真实 API 编写，避免示例“看起来能跑”却覆盖平台环境。

Ascend 性能验证遵循 [N0–N6](ascend-design.md)：图捕获预热与 KV 预热分开，记录有效 KV blocks 和 workspace；不要从 HBM 总量反推并发承诺。CPU 绑定仅限已授权进程及容器允许范围，不运行文档中的全局 IRQ/irqbalance 修改。npu-smi 和 profiler 用于受控诊断，不进入请求热路径；不写空转计算来维持服务器在线。

2026-10-11 仅更新规划与本地仓库，本次没有连接、更新服务器 checkout 或改变运行环境。远端继续保持此前已验证提交，待审查后再按授权同步。
