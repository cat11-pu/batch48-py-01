# batch48-py-safety-a-26

## 题面


会话粘滞层是防会话劫持与流量窃取的边界。接入层改到一致性哈希环之后，值班室连着几天被同类工单占满：机房做一次滚动扩容，几乎全站会话都掉线重登，事后核对只有本该落到新机器上的小部分流量需要迁移；某台机器的权重从 1 调到 3，第二天又被调回 1，同一批客户端就在两个后端之间来回弹，登录态反复失效；健康检查刚失败一次机器就被摘出环，恢复后一次探测成功又马上回环，抖动打断在途请求；摘机器时长连接被切断，事后只看到一个总数，查不出是哪条连接、什么时候被强制断开；进程重启后环和排空进度全部丢失，已经迁移过的 key 按新环又算了一遍，缓存命中率掉两成。范围是 gsb/batch48-py-safety-a-26 下的 lb_drain_guard.py，类 LbDrainGuard，负责虚拟节点哈希环、后端权重、健康状态与排空。

公开接口：LbDrainGuard(*, virtual_nodes: int = 160, failure_threshold: int = 3, success_threshold: int = 2, max_connections: int = 1000)；add_backend(name: str, *, weight: int = 1) -> dict；remove_backend(name: str) -> dict；set_weight(name: str, weight: int) -> dict；drain(name: str, *, deadline_ms: int) -> dict；route(key: str) -> str；accept_connection(backend: str, conn_id: str, *, now_ms: int) -> dict；close_connection(conn_id: str, *, now_ms: int) -> dict；expire_connections(now_ms: int) -> dict；report_health(name: str, ok: bool) -> dict；list_backends() -> list；snapshot() -> dict；restore(state: dict) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 LbError(code)，code 取 LB_INVALID_WEIGHT、LB_BACKEND_EXISTS、LB_UNKNOWN_BACKEND、LB_LAST_BACKEND、LB_NO_BACKEND、LB_BACKEND_DRAINING、LB_DUPLICATE_CONNECTION、LB_UNKNOWN_CONNECTION、LB_CONNECTION_LIMIT、LB_SNAPSHOT_INVALID。

要修的点：一、环由每个后端的 weight 乘以 virtual_nodes 个虚拟节点组成，节点标识按后端名与序号稳定生成，route 对 key 做稳定哈希后取环上顺时针第一个节点，同一个 key 永远落同一后端，环为空抛 LB_NO_BACKEND；二、add_backend 返回 name、weight、vnodes、state，vnodes 等于 weight 乘以 virtual_nodes，同名重复登记抛 LB_BACKEND_EXISTS 且不改动环，weight 为负抛 LB_INVALID_WEIGHT；三、add_backend 只影响落到新后端虚拟节点上的 key，其余 key 的映射原地不动，remove_backend 只迁走落其上的 key，返回 name 与 vnodes_removed，未知后端抛 LB_UNKNOWN_BACKEND；四、环上只剩一个后端时删除它必须抛 LB_LAST_BACKEND，不能把全部 key 映射到空环；五、set_weight 按新权重重建该后端的虚拟节点，weight 为 0 等同摘除，不占虚拟节点、不接新 key、state 为 disabled，但仍留在后端表里，正权重可回到 active，未知后端抛 LB_UNKNOWN_BACKEND；六、权重调整后同一个 key 只允许迁移一次，重建前后的环不能让它在新旧后端之间反复弹，权重不变时重复 set_weight 幂等，映射不变；七、drain 把后端置为 draining 并立刻移出环，新连接抛 LB_BACKEND_DRAINING，重复摘除同一后端要幂等，不抛错也不重复计数，未知后端抛 LB_UNKNOWN_BACKEND；八、accept_connection 返回 conn_id、backend、opened_ms，conn_id 重复抛 LB_DUPLICATE_CONNECTION，未登记后端抛 LB_UNKNOWN_BACKEND，在途连接数达到 max_connections 抛 LB_CONNECTION_LIMIT；九、排空截止前 close_connection 属正常关闭，返回 forced 为 False，排空截止到点仍有在途请求时 expire_connections 必须强制断开并按连接粒度记账，返回 closed 与 forced 两个计数；十、健康检查连续失败达到 failure_threshold 才翻 down 并摘出环，连续成功达到 success_threshold 才翻回 active，单次失败或成功都不翻转，翻转时对应计数清零，report_health 返回 name、state、failures、successes、changed；十一、快照固定带 version=1、backends、ring、connections、stats 五个键，backends 与 connections 以标识为键，必须是深拷贝，restore 后 route 对每个 key 的结果与恢复前逐项相同，不得按默认权重重算环，结构非法抛 LB_SNAPSHOT_INVALID 且原状态不变；十二、stats 固定返回 backends、active、draining、down、disabled、connections、accepted、closed、forced 九个整数，accepted 与 closed 分别按成功接纳与正常关闭次数累加，reset_stats 只把 accepted、closed、forced 归零，其余按当前实际状态重算。

硬约束：add_backend、remove_backend、set_weight、drain、accept_connection、close_connection、report_health 全程持同一把逻辑锁，八路并发接纳与关闭连接时 accepted 必须等于成功调用次数，connections 收尾归零，不得丢计数；快照恢复后排空截止时刻按绝对毫秒继续；连接表受 max_connections 硬顶，禁止无界增长。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现。
## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 地狱
- module: lb_drain_guard
- typeName: LbDrainGuard
