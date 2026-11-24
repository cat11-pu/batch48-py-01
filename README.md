# batch48-py-safety-c-07

## 题面

扫描队列迁到多 worker 之后，运维一天报了三类问题：同一目标同时被两台机器扫，两边结果互相覆盖，谁也说不清哪份是最新的；worker 中途崩掉，任务永远停在运行中，重启服务后这些任务既没人回收也没人敢重派；一批大目标排在最前面，critical 任务在后面等了六小时还没轮上；还有任务失败重试后任务号变了，下游按任务号取结果全部取空；死信只记了条数，失败原因丢了，复盘时只能靠猜。范围是 gsb/batch48-py-safety-c-07 的 scan_job_scheduler.py，类 ScanJobScheduler，按目标与画像去重派发任务，用租约、心跳和过期回收维持调度状态，并把退避重试与死信分开记账。

公开接口：ScanJobScheduler(*, per_target_limit=2, global_limit=256, worker_limit=32, max_attempts=5)；submit(job) -> int；lease(worker, now, ttl=60.0) -> list[dict]；heartbeat(job_id, now, ttl=60.0) -> float；complete(job_id, now) -> dict；fail(job_id, now, reason) -> dict；requeue_expired(now) -> list[int]；job_state(job_id) -> dict；dead_letters() -> list[dict]；pending() -> dict；snapshot() -> dict；restore(state) -> None；stats() -> dict。拒绝统一抛 SchedulerError(code, detail)。

故障与边界：①job 结构为 target、profile、priority，priority 只接受 critical、normal、low，缺省 normal，参数非法抛 SCAN_INVALID_JOB；任务号自 1 单调递增；②同一 (target, profile) 的在途任务重复提交抛 SCAN_DUPLICATE_JOB，且不消耗任务号，已完成或已进死信的同组合可以再次提交；③lease 先取 critical 再 normal 再 low，同级按入队序号先进先出，返回的每个任务带 lease_until 与 attempt；④每目标在租数受 per_target_limit、全局在租数受 global_limit、单 worker 在租数受 worker_limit 三重约束，任一项到顶就不再派，被目标上限挡住的任务不得插到别的目标前面；⑤heartbeat 把租约续到 now 加 ttl 并返回新的 lease_until，非在租任务抛 SCAN_LEASE_NOT_HELD，未知任务号抛 SCAN_JOB_NOT_FOUND；⑥requeue_expired 把过期租约与到点的退避任务放回队列，返回升序任务号，重复调用返回空且不再计额；⑦complete 只接受在租任务，结束归还全部额度；已完成、未租或已死任务抛 SCAN_LEASE_NOT_HELD，未知任务号抛 SCAN_JOB_NOT_FOUND；⑧fail 按 2 的尝试次方退避，next_at 等于 now 加 2 的 attempt 次方，退避期间保持原任务号与原优先级，达到 max_attempts 进死信并保留原因；⑨同一任务无论重试多少次，任务号不得改变，job_state 里的 attempt 随失败次数递增；⑩租约过期并被重新派发后，旧 worker 迟到的 complete 必须被拒绝且不归还额度；⑪低优先级任务等待超过 3 个调度周期后提升一档，避免被大目标饿死；⑫死信任务再次提交同一 (target, profile) 时以新任务号接收，死信记录保留，快照结构非法抛 SCAN_SNAPSHOT_INVALID 且原状态不动。

硬约束：并发 submit 与 lease 不得把同一任务派给两个 worker，同一任务号在任意两次租约里只能出现一次；快照导出后恢复，在途租约、退避时间、死信与下一个任务号一致；三个并发上限都是硬顶，不得先派后补，超限时整批不派而不是部分派发。lease 的选择只依赖任务表当前状态与入队序号，不依赖字典遍历顺序。stats 固定返回 submitted、leased、completed、failed、dead、requeues 六个整数，pending 按 priority 三档返回在队数量。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 漏洞检测与攻防
- type: Bugfix
- difficulty: 困难
- module: scan_job_scheduler
- typeName: ScanJobScheduler
