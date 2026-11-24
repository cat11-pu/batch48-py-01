# batch48-py-safety-a-25

## 题面

出口代理切换上线后，值班接到三类工单：一条链路在配置里写了两遍同一个代理，解析器没有报错，请求在链上绕了一圈又回到起点；内网域名被误判成走代理，连数据库的流量绕出去再回来，延迟翻了十倍；主出口连续探测失败已经被降级，某次探测偶然成功就立刻被切回去，流量在坏链路上来回抖，用户看到页面反复超时。范围是 gsb/batch48-py-safety-a-25 下的 proxy_chain_resolver.py，类 ProxyChainResolver，负责解析代理链、判定直连 bypass、按优先级与健康状态选择出口。

公开接口：ProxyChainResolver(*, max_hops: int = 8, cooldown: float = 60.0, failure_threshold: int = 3, clock=None)；parse(spec: str) -> list；set_bypass(rules) -> None；bypass(target: str) -> bool；register_egress(egress_id: str, *, priority: int, address: str) -> None；remove_egress(egress_id: str) -> bool；select(now=None) -> str；probe_result(egress_id: str, ok: bool, now=None) -> dict；health(egress_id: str) -> dict；snapshot() -> dict；restore(state: dict) -> None；stats() -> dict。拒绝统一抛 ProxyError(code)，code 取 PROXY_INVALID_ARGUMENT、PROXY_INVALID_SPEC、PROXY_UNSUPPORTED_SCHEME、PROXY_INVALID_PORT、PROXY_TOO_MANY_HOPS、PROXY_LOOP、PROXY_INVALID_BYPASS、PROXY_DUPLICATE_EGRESS、PROXY_UNKNOWN_EGRESS、PROXY_NO_HEALTHY_EGRESS、PROXY_SNAPSHOT_INVALID。

要修的点：一、parse 按逗号拆分并按出现顺序返回每跳的 scheme、host、port、auth，userinfo 存在时 auth 为真，凭据原文不得出现在结果里；二、scheme 只支持 http、https、socks5、socks5h，其余报 PROXY_UNSUPPORTED_SCHEME；三、端口必须是 1 到 65535 的整数，缺端口、空段、空串分别报 PROXY_INVALID_SPEC 或 PROXY_INVALID_PORT；四、跳数超过 max_hops 报 PROXY_TOO_MANY_HOPS；五、环检测按归一化后的 host:port 比对，大小写、默认端口省略、IPv6 方括号都要先归一，否则 8080 与省略端口会被当成不同节点；六、bypass 同时支持 CIDR、域名后缀、通配与精确主机，后缀规则匹配子域与自身，通配只匹配子域；七、bypass 规则非法报 PROXY_INVALID_BYPASS，0.0.0.0/0 与 ::/0 表示全部直连，必须与空 bypass 列表（全部走代理）区分开；八、出口按优先级数值小的优先，同优先级按注册顺序，全部降级时 select 报 PROXY_NO_HEALTHY_EGRESS；九、连续失败达到 failure_threshold 后按当前时刻加 cooldown 降级，降级期间不参与选择；十、冷却到期且一次探测成功才回升，冷却期内即使探测成功也不得回升，否则坏出口会反复被切回；十一、重复登记报 PROXY_DUPLICATE_EGRESS，未知出口报 PROXY_UNKNOWN_EGRESS，出口号、地址为空报非法参数，删除出口后健康状态不得残留；十二、snapshot 带 version=1 与 stats，restore 后健康状态、冷却截止时刻与统计都要回来，冷却按绝对时间继续，结构非法报 PROXY_SNAPSHOT_INVALID 且不动原状态，stats 固定返回 parses、probes、failures、degradations、recoveries 五个整数。

硬约束：parse、set_bypass、register_egress、remove_egress、select、probe_result 全程持同一把逻辑锁，并发探测与选择看到一致的健康快照，统计与调用次数严格相等；快照跨进程恢复后健康状态与冷却截止时刻按绝对时间继续，不得重新计时；代理链跳数与解析结果长度受 max_hops 硬顶。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发探测与重启恢复自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 困难
- module: proxy_chain_resolver
- typeName: ProxyChainResolver
