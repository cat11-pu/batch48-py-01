# batch48-py-safety-a-28

## 题面

多路径接入切到新调度器之后，运维和业务同时报障：主路径连续丢包已经不可用，备份子流却一直挂在待命状态不接管，连接只能干等超时；有人手工打开备份后，主路径只恢复一次就被立刻切回去，流量又在两条路径之间来回抖；跨子流补发时同一段数据被交付两遍，上层重组直接报错；某条子流被移除后，它上面未确认的数据消失了，重传窗口的序号还往回退了一格；映射表在长连接上跑满一天没有上限，内存一路涨到被系统杀掉；进程重启后子流 RTT、拥塞窗口和已交付区间全部丢失，重连后同一段数据又交付了一次。范围是 gsb/batch48-py-safety-a-28 下的 mptcp_scheduler.py，类 MptcpScheduler，负责子流管理、备份启用、DSS 映射去重与按 RTT 加权调度。

公开接口：MptcpScheduler(*, failure_threshold: int = 3, hysteresis_ms: int = 2000, max_subflows: int = 8, max_mappings: int = 4096)；add_subflow(sid: str, *, path_id: int | None = None, priority: int = 0) -> dict；remove_subflow(sid: str) -> dict；report_path(sid: str, *, ok: bool, rtt_ms: int = 0, loss: float = 0.0, now_ms: int = 0) -> dict；set_cwnd(sid: str, cwnd: int) -> dict；schedule(size: int, *, now_ms: int = 0) -> dict；ack(subflow: str, length: int) -> dict；dss_map(seq: int, length: int, subflow: str) -> dict；enable_backup(sid: str, *, now_ms: int = 0) -> dict；release_backup(sid: str, *, now_ms: int = 0) -> dict；list_subflows() -> list；snapshot() -> dict；restore(state: dict) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 MptcpError(code)，code 取 MPTCP_INVALID_ARGUMENT、MPTCP_DUPLICATE_SUBFLOW、MPTCP_UNKNOWN_SUBFLOW、MPTCP_SUBFLOW_LIMIT、MPTCP_INVALID_ROLE、MPTCP_BACKUP_NOT_ELIGIBLE、MPTCP_HYSTERESIS_PENDING、MPTCP_NO_SUBFLOW、MPTCP_CWND_EXHAUSTED、MPTCP_INVALID_MAPPING、MPTCP_MAPPING_LIMIT、MPTCP_SNAPSHOT_INVALID。

要修的点：一、add_subflow 返回 sid、path_id、role、state，第一个加入的是 primary 且 state 为 active，其余是 backup 且 state 为 standby，path_id 缺省为 None；sid 为空抛 MPTCP_INVALID_ARGUMENT，重复抛 MPTCP_DUPLICATE_SUBFLOW，超过 max_subflows 抛 MPTCP_SUBFLOW_LIMIT；每个子流默认 cwnd 为 1000、rtt_ms 为 100；二、list_subflows 按 sid 升序返回 sid、path_id、role、state、cwnd、rtt_ms、inflight 七项，数值与快照里保存的一致；三、report_path 成功时清零连续失败并刷新 rtt_ms 与 loss，失败时累加 fails，fails 达到 failure_threshold 后 state 变 failed 并退出调度，成功上报可把 failed 拉回 active，返回 sid、rtt_ms、loss、fails、state，未知子流抛 MPTCP_UNKNOWN_SUBFLOW；四、schedule 在 state 为 active 或 backup_active 的子流里选 rtt_ms 最小的一个，相同 RTT 按 sid 升序，选中后按 size 增加该子流 inflight，返回 size、sid、inflight，size 非正抛 MPTCP_INVALID_ARGUMENT；五、可用子流都放不下 size 时抛 MPTCP_CWND_EXHAUSTED，完全没有可用子流时抛 MPTCP_NO_SUBFLOW，set_cwnd 返回 sid 与 cwnd、非正抛 MPTCP_INVALID_ARGUMENT，ack 返回 subflow、inflight、acked 三项，按子流扣减 inflight 并累计 acked_bytes，扣减不得出现负值；六、enable_backup 只对 standby 备份子流生效，且必须主路径连续失败已达阈值，否则抛 MPTCP_BACKUP_NOT_ELIGIBLE，对 primary 调用抛 MPTCP_INVALID_ROLE，启用成功返回 sid 与 state，state 为 backup_active 并参与调度；七、备份启用后主路径恢复，回归必须按滞回窗口计算，主路径健康持续时间不足 hysteresis_ms 时 release_backup 抛 MPTCP_HYSTERESIS_PENDING，不得立刻切回；八、dss_map 维护已交付字节区间，只要求子流已登记、与 state 无关，重叠部分只交付一次，delivered 为本次新增字节数，ranges 返回合并后的左闭右开有序区间、每项为两元素列表，映射要能表示跨子流不连续的序号，返回 seq、length、subflow、delivered、ranges，length 非正抛 MPTCP_INVALID_MAPPING，未知子流抛 MPTCP_UNKNOWN_SUBFLOW；九、映射条目数受 max_mappings 封顶，超限抛 MPTCP_MAPPING_LIMIT，remove_subflow 返回 sid 与 inflight_released，未知子流抛 MPTCP_UNKNOWN_SUBFLOW，移除时未确认数据按原数据序号重新调度，已确认序号不得回退；十、快照固定带 version=1、subflows、mappings、ranges、stats 五个键，subflows 以 sid 为键，必须是深拷贝，restore 后 RTT、拥塞窗口、inflight、已交付区间与统计都回来，结构非法抛 MPTCP_SNAPSHOT_INVALID 且原状态不变；十一、stats 固定返回 subflows、scheduled、acked_bytes、delivered_bytes、mappings、removed 六个整数，scheduled 按成功调度次数、acked_bytes 与 delivered_bytes 按累计字节、mappings 与 removed 按成功映射与移除次数累加，reset_stats 除 subflows 按当前子流数重算外其余归零。

硬约束：add_subflow、remove_subflow、report_path、set_cwnd、schedule、ack、dss_map、enable_backup 全程持同一把逻辑锁，八路并发写入互不重叠的 DSS 区间时 delivered_bytes 必须等于各次新增之和、区间不得重复交付；快照跨进程恢复后已交付区间与 inflight 原样继续；映射条目与子流数量分别受 max_mappings、max_subflows 硬顶，禁止无界增长。

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
- module: mptcp_scheduler
- typeName: MptcpScheduler
