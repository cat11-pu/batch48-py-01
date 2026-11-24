# batch48-py-safety-a-16

## 题面

反向代理上线 CONNECT 隧道准入后，安全组连续退回三批工单。有的客户端把目标写成裸 IP 就连进了办公网段；有的域名第一次解析是公网地址，过一小时再解析已经指到内网，隧道却一直转发；同一租户开了四条隧道后第五条直接穿透，租户并发统计和实际连接数对不上；运维凌晨改了一条策略，部分隧道按新策略断开、部分还按旧策略放行，审计里看不出到底按哪一版执行；代理进程重启一次，未结算隧道的上下行字节全部归零，租户当月用量凭空少了一截。落点是 gsb/batch48-py-safety-a-16 的 connect_tunnel_guard.py，类 ConnectTunnelGuard，负责目标解析、准入判定、隧道计量与策略版本管理。

公开接口：`ConnectTunnelGuard(*, default_ports=(443,), max_tunnels_per_tenant=4, max_total_tunnels=64, queue_timeout_ms=500, idle_timeout_ms=30000, resolver=None, clock=None)`；`set_policy(tenant, policy) -> dict`；`policy_of(tenant) -> dict`；`open_tunnel(authority, *, tenant, now=0) -> dict`；`admit_queued(tenant, *, now=0) -> dict`；`meter(tunnel_id, up, down, *, now=0) -> dict`；`close_tunnel(tunnel_id, reason="client_closed", *, now=0) -> dict`；`revalidate(tunnel_id, *, now=0) -> dict`；`list_tunnels(tenant=None, *, state=None) -> list`；`tenant_usage(tenant) -> dict`；`stats() -> dict`；`snapshot() -> dict`；`restore(state) -> None`。拒绝统一抛 TunnelError(code, detail)。策略字段为 ports、deny_private、deny_hosts、max_tunnels、max_bytes，缺省取构造参数，set_policy 返回带 version 的归一化视图，首次为 1，之后每次递增。隧道编号按 tn-0001 递增，排队请求按 q-0001 递增，open_tunnel 返回 tunnel_id、host、port、resolved、policy_version、state、opened_at，排队时返回 status 为 queued 与 expires_at。

故障与边界：一、authority 只接受 host:port，IPv6 必须写成方括号形式，缺端口、端口非数字、端口越界、空主机、裸 IPv6 一律 TUNNEL_AUTHORITY_INVALID；二、字面 IP 先判私网、回环、链路本地、保留、组播与未指定地址，命中即 TUNNEL_PRIVATE_TARGET；三、域名交给注入的 resolver 解析，任一结果落在上述网段即拒绝，结果为空或解析抛异常报 TUNNEL_RESOLVE_FAILED；四、端口不在白名单报 TUNNEL_PORT_DENIED，deny_hosts 按域名标签边界匹配，notinternal.example.com 不得被 internal.example.com 误伤，命中报 TUNNEL_TARGET_DENIED；五、租户并发达到 max_tunnels 时按 queue_timeout_ms 排队，admit_queued 按先来先到补位，排队超时必须计为拒绝并写入该租户拒绝数，不能悄悄放行；六、close_tunnel 重复调用保持首次原因并返回 already_closed 为真，未知编号报 TUNNEL_UNKNOWN；七、meter 累计上下行字节并给出距上次计量的 idle_ms，越过策略 max_bytes 时关闭隧道，reason 为 quota_exceeded，之后继续计量报 TUNNEL_CLOSED；八、空闲超过 idle_timeout_ms 的隧道在下一次计量时以 idle_timeout 关闭；九、策略热更新后，在建隧道若新版本仍允许则该次计量起升级 policy_version，若新版本把目标改为拒绝，必须在字节窗口内以 policy_changed 断开，断开原因与超限、空转要能区分；十、revalidate 重新解析域名，落回私网时必须在本次校验内强制关闭并记原因，不能继续转发；十一、snapshot 覆盖策略、隧道、排队、计数与序号，恢复后未结算隧道按已用字节继续累计并把 idle 接上，非法快照报 TUNNEL_SNAPSHOT_INVALID 且原状态不变。

硬约束：open_tunnel、meter、close_tunnel、revalidate 全程持锁，多路并发开隧道与计量时 opened、closed、rejected 与调用次数完全相等，隧道编号不重复；策略读取原子，判定与计量只能看到完整旧版本或完整新版本，不得读到半新半旧；租户并发、排队长度、隧道总量都是硬顶，超限只拒绝不吞并；admit_queued 的补位必须重新校验总量，不能绕过总上限。stats 固定返回 opened、closed、rejected、revalidations 四个整数。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发准入与重启恢复自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 困难
- module: connect_tunnel_guard
- typeName: ConnectTunnelGuard
