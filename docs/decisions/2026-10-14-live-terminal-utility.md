# cedb477 单后端只读终态观测实验

日期：2026-10-14。状态：沿用用户批准的小型可选观测范围，先合同/反例，再本项目隔离环境实机；不冻结生产接口或激活MOD。

新分配32068有2×910B2，物理5/6；8核配额/64GiB。旧服务venv固定版本/接口与189包通过清除SDK PYTHONPATH后的检查。首验1张卡、公共Qwen2.5-14B、FP16/eager/TP1/DP1；不因默认SDK工具包元数据冲突更换可用引擎。

固定HUST已有`vllm.endpoint_plugins`和EngineCore utility消息通道。复用这两个入口：general plugin只注册一个只读core观测方法；endpoint plugin在loopback已有API服务初始化时安装前端关联，并通过现有utility拉取core正终态。不新增代理/监听进程、核心IPC字段或引擎源码fork。

core在显式握手后检查实际scheduler清理callable的来源，再包装实例的原`_free_request`；原调用成功返回后才记录真实child ID，保持原返回/异常。观测仅有界内存操作，没有远程ACK、文件写入、设备同步、分配或淘汰修改。utility返回进程/观测代次、连续序号与有限重放；满额/身份冲突令观测失效，不能抹掉账本。

前端继续复用EntryCapture/TerminalJournal，移到包内单一实现，旧tools保留兼容导出；render后预留完整family、实际generate/add_request处捕获组和child，全部提交才seal。流式生成器持有自己的关联上下文，不能依赖HTTP方法返回后仍存活的隐式上下文。原生取消/HTTP结束不构成core确认。只捕获显式管理前缀的请求，未知流量不伪造成已管理的请求。

endpoint只在显式allowlist、默认关闭开关、固定版本/文件指纹和loopback配置下启用，控制读取另需本次随机凭据。不得记录凭据、prompts/tokens。控制侧提交原账本后才确认，通用Ended与业务结果分别输入；该入口不修改推理请求或重发。

首轮有限留存，到上限停止增强准入；不宣称长期GC、跨前端重建/完整热接管、生产恢复、性能容量或所有模型支持。关闭在无未决受管family且调用方已停止派发时恢复自有包装；新进程default-off须验证。装载后callable被Ascend替换、需要大幅fork/新增核心字段、无法控制开销则停止并讨论。现场事实和失败单独记录。

固定来源：[endpoint接口](https://github.com/vLLM-HUST/vllm-hust/blob/ced6857afa0ea7b2e3f0846a62e1394e90f15607/vllm/plugins/endpoint_plugins/interface.py)、[utility与取消](https://github.com/vLLM-HUST/vllm-hust/blob/ced6857afa0ea7b2e3f0846a62e1394e90f15607/vllm/v1/engine/core_client.py)、[core调用](https://github.com/vLLM-HUST/vllm-hust/blob/ced6857afa0ea7b2e3f0846a62e1394e90f15607/vllm/v1/engine/core.py)。
