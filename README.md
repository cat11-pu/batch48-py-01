# batch48-py-safety-d-08

## 题面

采集器升级重启之后，流量报表连续几天对不上：导出序列号每次重启都从零开始，收集端把整批流记录当成重复直接丢弃，长连接会话的字节数少了一大截；另一个机房的采集器更糟，模板只要重启就整份重发，收集端模板版本错乱，解码出来的字段全部串位。运维还发现链路抖动时采集进程会被导出队列拖住，丢包计数一路涨；某条持续几个小时的长连接，在报表里只剩最后一分钟的数据。两台采集器的配置完全一样，问题却只在重启过的机器上复现，排查时一度怀疑是收集端解析库的版本差异。

范围是 gsb/batch48-py-safety-d-08 下的 netflow_export_guard.py，类 NetflowExportGuard，负责流表老化、模板管理与导出队列。

对外方法：`NetflowExportGuard(*, active_timeout: int = 30, interim_interval: int = 60, max_queue: int = 512, max_flows: int = 10000, sampling_rate: int = 1, clock=None)`；`add_packet(*, src: str, dst: str, src_port: int, dst_port: int, protocol: str, length: int, tcp_flags: int = 0, now=None) -> dict`；`close_flow(flow_key: str, *, now=None) -> dict`；`set_template(template_id: int, fields: list[str]) -> bool`；`export_due(*, now=None) -> list[dict]`；`flush(*, now=None) -> list[dict]`；`active_flows(*, now=None) -> list[dict]`；`queue_depth() -> int`；`dropped_count() -> int`；`snapshot() -> dict`；`restore(state: dict) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 ExportGuardError(code, detail)。

故障与边界：一、流键由源地址、目的地址、源端口、目的端口和协议组成，格式为源地址:源端口->目的地址:目的端口/协议，同一五元组的包聚合到同一条活跃流，包数与字节数逐包累加，add_packet 返回 flow_key、packets、bytes、first_seen 与 last_seen，active_flows 返回同样的流记录；二、活跃流在 last_seen 之后满 active_timeout 秒没有新包才导出，恰好等于阈值即导出；三、未结束的长流每 interim_interval 秒产生一次中间导出，中间导出不重置流的 first_seen，也不关闭流；四、模板内容变化才重发并递增模板版本，模板 id 相同且字段列表一致时返回 False；五、模板字段为空或重复抛 TEMPLATE_INVALID，尚未登记模板时到期导出抛 TEMPLATE_UNKNOWN 且活跃流保留；六、导出包序列号从 1 开始连续递增，export_due、flush、close_flow 共用同一序列，不跳号不重复，导出包含 sequence、template_id、template_version、record_count 与 records；七、导出队列满时丢弃最旧记录并累加 dropped，采集路径不得阻塞也不得抛错；八、字节数按 sampling_rate 换算成 estimated_bytes，累加超过 64 位无符号上限时饱和在该上限，不回到零；九、flush 导出全部活跃流并关闭它们，第二次调用返回空列表且不占用新序列号；十、close_flow 立即导出指定流并按当前时间结算 last_seen，未知流键抛 FLOW_UNKNOWN；十一、活跃流表上限 max_flows，满后拒绝新流抛 FLOW_FULL，已有流不受影响；十二、快照覆盖模板、序列号与活跃流表，恢复后下一次导出从原序列号续号，结构非法抛 STATE_CORRUPT 且原状态不变。

硬约束：add_packet 与 flush 串行执行，六路并发各提交 50 个包后累计包数必须恰好 300，导出序列号全局唯一且连续；导出队列有界，采集路径不得被导出阻塞；snapshot 必须是深拷贝，恢复后的对象不得与原状态共享可变结构；stats 固定返回 packets、bytes、packets_exported、sequence、dropped 五个计数，reset_stats 只清计数不清序列号与流表。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 地狱
- module: netflow_export_guard
- typeName: NetflowExportGuard
