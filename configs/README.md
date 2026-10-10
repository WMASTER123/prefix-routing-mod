# 配置

example.json 只是静态单池样例，不会启动服务。worker endpoint 是源站地址，不包含 /v1。model 是模型服务名，不是自动下载指令。

本地真实配置使用 configs/local.<name>.json（Git 忽略）；不要把凭据嵌入 URL。未来鉴权从安全配置/secret 引用提供。compatibility_domain 需要 P1b 验证真实模型身份后填写，不能把它当作已检查的指纹。

当前 doctor 只校验结构、ID/地址唯一性和安全格式，未探测网络或 NPU。运行配置、动态开关、资源上限、超时及 SLO 在对应模块实现时加入 schema 与测试，不预先填无依据的参数。
