# 组合原生成熟 Router 与 HUST 终态：有限联调

沿用已批准的顺序，不增加生产服务/索引或扩大 HUST 包装。新 example 调用既有固定 Router 的 typed HTTP 路径，复用 AdmissionClient、Outbox、LifecycleTrace 与原 DurableLedger；不是重新实现代理。工具实验接收器从受控 HUST 启动取得明确 worker/instance/observer 绑定，不继续使用 fixture 身份。

健康与终态由既有 loopback 控制接口提供，固定绑定失效/超时立即拒绝新准入并保债。单后端无性能策略。仍为有限 CPU/NPU 实验协议：Router 事件只在其自身时钟域校验；终态对账使用该已知源时钟的最新逻辑位置，不拿后端/其他进程 monotonic 值相减，不报告接收延迟。真实健康检查单独以调用端实际期限控制，不用合成0负载代替健康。

验证正常完成、流式断连、确认前额度拒绝、核心确认后再次准入、终态重复、源故障拒绝及提交后丢准入ACK零发送/重开保债。调度/淘汰/设备执行不变，普通事件不能伪造ENDED；先合同/反例，再真实固定实例。复用原实验存储/loopback RPC 不代表已选生产IPC/SQLite。M7→M2及双副本容量在该链路通过后推进，不提前宣布生产ABI、MOD启停或性能通过。
