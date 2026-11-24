# batch48-py-game-a-17

## 题面

跨平台联机灰度上线后版本协商开始出问题：上一季的客户端连新集群时，两端明明有一段重叠的协议版本，握手却在第一轮就被判失败；有的客户端把不认识的能力位混进能力集，服务端只把认识的那几位启用后就握了手，进游戏才发现回滚预测对不上，人物动作在本地和服务器各跑一套；灰度期运维把兼容下限放宽了两个版本，一部分客户端刚好卡在旧下限上仍然被挡在门外，客服只能让他们整个包重下；同一连接断线重连后，服务端直接拿上一轮的协商结果复用，而客户端这次报的版本更低，字段按高版本编码，进图后输入直接乱序；连接表在高峰时无限增长，进程内存一路爬到告警线。范围是 gsb/batch48-py-game-a-17 的 netcode_negotiation_guard.py，类 NetcodeNegotiator，负责版本区间交集、能力位交集与灰度窗口。

公开接口：NetcodeNegotiator(*, server_min, server_max, supported_caps, profile_limit=8)；compat_window() -> dict；set_grace_window(low) -> dict；clear_grace_window() -> dict；negotiate(connection_id, *, client_min, client_max, client_caps) -> dict；reconnect(connection_id, *, client_min, client_max, client_caps) -> dict；drop(connection_id) -> None；profile(connection_id) -> dict；profiles() -> int；capability_names(mask) -> list；is_compatible(version) -> bool；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 NetcodeError(code)。

要修的点：一、服务端声明版本区间与能力位，构造时 server_min 大于 server_max、server_min 小于 1、能力位越过第 4 位、profile_limit 小于 1 都抛 NET_INVALID_ARGUMENT；二、协商取双方版本区间的交集并选择其中最高版本，交集为空抛 NET_VERSION_UNREACHABLE，detail 里带 client 与 server 两个区间；三、能力集取双方交集，交集为零抛 NET_CAP_NONE_SHARED，客户端能力位出现未知或保留位时整条连接拒绝，不能只启用认识的部分；四、协商结果返回 connection、version、caps、profile、degraded、reused，profile 形如 profile-1 并按协商次数单调编号，version 低于服务端最高版本时 degraded 为真；五、客户端区间倒挂、版本小于 1、能力位为负、连接标识为空都抛 NET_INVALID_ARGUMENT；六、灰度期由 set_grace_window 声明更低的兼容下限，取值必须在 1 到 server_min 之间，否则抛 NET_GRACE_INVALID，compat_window 返回生效后的 min 与 max，clear_grace_window 恢复服务端原区间；七、连接档案按连接缓存并有条数上限，超出淘汰最早建立的档案，profile 查询未知连接抛 NET_UNKNOWN_CONNECTION；八、reconnect 按当前窗口重新协商，版本与能力集都没变时复用原档案并置 reused，客户端降版本时不得复用旧档案，必须重新协商；九、drop 释放档案并计入 dropped，重复 drop 抛 NET_UNKNOWN_CONNECTION；十、capability_names 按位从低到高返回名称，is_compatible 按生效窗口判断，版本小于 1 抛 NET_INVALID_ARGUMENT；十一、stats 固定返回 negotiations、reconnects、reused、rejected、degraded、dropped 六个整数，degraded 只统计 negotiate 产出低于服务端最高版本的次数，reset_stats 归零但不清档案；十二、snapshot 返回 version=1 的深拷贝，restore 后档案、灰度窗口、下一个档案编号与统计一致，结构非法抛 NET_SNAPSHOT_INVALID 且不破坏当前状态。

硬约束：negotiate、reconnect、drop 与快照读写全程持同一把锁，八路并发协商拿到的档案编号两两不同且连续；快照恢复后不得与恢复前的对象共享可变结构；连接档案条数受 profile_limit 硬顶，内存不随连接总数增长。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发协商与恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- module: netcode_negotiation_guard
- typeName: NetcodeNegotiator
