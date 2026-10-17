# 下一次单后端真实接线与验收清单

日期：2026-10-13。状态：下一工作包的具体边界；没有安装终态 observer，没有通过现场验收。本轮用户要求先审查，NPU 服务器待下次提供。

## 目标与资源

用 1 张 NPU、公共区 Qwen2.5-14B-Instruct、固定 HUST 隔离组合，验证一条 Router→真实 HTTP frontend→core 逻辑终态→同一账本的请求闭环，再接已有 KV publisher→复用目录。首个现场运行用私有 loopback 端点、单前端、TP=1/DP=1、文本采样；多 prompt/n>1/chat 随关联测试逐项开启，不能把一项通过扩大为全部支持。目标图/并发/取消仍按 G3 门槛验收。

重新确认设备、版本、模型、项目隔离环境与端口后使用已有恢复入口，不升级全局或修改公共模型。实际 HUST/Ascend 固定提交分别为 13f11da44455d2f85f00f810a04e1bd2eaf30ba6 / 3a61053d8b4133eae7c468c1567648048750be21；当前 CPU 模拟成绩不证明新分配的运行兼容性。

## 必需接线与本次源码审查判断

| 边界 | 复用与必要适配 | 验收证据 |
|---|---|---|
| Router 标识→HTTP 入口 | 复用已验证的自有 X-Request-Id carrier；入口读取实际 header 并保留实际外部 ID，不能解析 cmpl/chatcmpl 后缀猜关联 | 与 body ID/重复 header 同时出现、completion/chat、stream/非 stream |
| HTTP 总 family→实际 child | 复用 renderer、AsyncLLM fanout 与 EntryCapture 语义；完整渲染组数、实际 params.n 和实际 internal ID 必须在相应调用点捕获 | 多 prompt×n、提前 child 结束、部分提交失败和取消均不能提前封口 |
| core 逻辑终态→关联 | 候选仍为 Scheduler._free_request 原调用成功后的只读记录，不改调度/资源管理；关联必须在前端映射被删除前保留 | 正常/长度停止/stop string/取消、原清理失败不能发布成功结束 |
| 观察→同一 M3 owner | 复用有界 journal/ACK/重放/实例代次和现有账本；使用独立终态输入，不在 transport 通道伪造 ENDED | 迟到、重复、断连、丢确认、超窗恢复；generic ended 不伪装业务成功 |
| 运行时交接 | 复用已经验证的保债交接；现场获取真实 service instance/健康，分别证明旧派发已停止 | 同地址重启和旧身份拒绝、重启后不凭历史健康直接发送 |
| KV 与 M2 | 复用已通过子集验证的可选 publisher 和现有规范化/索引；先单分区实际订阅与共同水位，再扩全池 | 热接入/存储/淘汰/清空/重放/断连/重启及目录撤销 |

这次只读源码核查发现：EngineCoreRequest.external_req_id 已有字段，但 scheduler 使用的 Request 没有直接保留它，不能在核心结束时仅靠该字段恢复 Router attempt。Request 可保留 trace_headers，但 serving 根据现有 tracing 路径决定是否传入，不能假设关闭 tracing 时仍有可靠载体，也不在这轮决定复用 tracing 作为控制协议。下一实现必须证明实际 ID 的传递/关联边界，不能单凭字段存在宣布接通。

固定源码补充核查：v1/engine/__init__.py SHA256 为 9e4e81babe115eaf1f864f8f44d0a667260fe4b6d007dd650ce45d0a2eb9c150；v1/request.py 为 06b1c6ad3cc0223c2036b4a2e317868fa4988391b4b024bc5e5f54672b7d2c80。其余入口/AsyncLLM/core 源文件沿用已有 guard；此次未运行或改动引擎源码。

## 安装、开销和回退

适配默认关闭、固定完整源文件守卫；复用已有插件加载与隔离部署机制。引擎线程只做有界本地关联/正证据记录，不等待远程 ACK、不调用设备同步，不写 prompts/tokens。控制侧另做投递与恢复，安装点、进程角色和可撤销状态需在安装前形成可审查清单。若必须新增核心 IPC 字段、重写 serving/调度器或大幅 fork，先讨论，不以“观测”为名直接扩张。

关闭时原函数行为恢复，未结清请求保留对账责任。先比较直接服务、仅 Router、Router 加观测三种功能与 Host 开销，再验收目标执行配置；不把 CPU FULL SQLite 作为生产热路径，也不以功能通过宣称性能改善。

## 本轮审查后的仍未完成项

真实跨进程 HUST 关联/终态、实际 KV 订阅、长期去重/GC、生产安装启停/排空、生产持久化实现及 SLO 容量比较均未完成。独立人工审查未替代，MOD 仍 import_only，暂不冻结最终生产接口/分工。本页用于下次直接进入真实接线，不要求先增加算法或完整遥测档案。
