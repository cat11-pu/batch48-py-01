# batch48-py-safety-b-08

## 题面

离线集群的 CPU 带宽调度这周连续出了两次事故：一次是多个训练组同时借空闲份额，借出总量超过了全局余量，新上的任务抢不到带宽被打回；另一次是某组归还 50 份配额后，账单把这个组记成了借出方，被借的那组额度反而迟迟不恢复。值班还发现借据到期时间按机器墙钟算，跨时区节点上前后差了整整一个周期；某个组借了份额后进程退出，剩下没用完的部分一直挂到下一个周期；回收队列按借据创建顺序处理，先创建的晚到期借据把早到期的挡在后面；有租户把单组配额配到最小粒度以下也照样通过，统计窗口一跨周期就把同一段用量算了两遍；并发借入时两个协程都看到同一份余量，最后一起把额度借穿。调度器重启后未归还借据全部丢失，累计用量和到期时间也一起归零。范围是 gsb/batch48-py-safety-b-08 的 cpu_bandwidth_broker.py，类 CpuBandwidthBroker，负责每组固定配额、空闲份额借贷、到期回收与用量核算。

公开接口：CpuBandwidthBroker(*, global_shares=800, min_granularity=10, max_loans=32, max_period=1000, max_groups=8, clock=None)；set_quota(group, quota, period) -> dict；borrow(group, shares, now) -> dict；return_loan(loan_id, now=None) -> dict；reclaim(group, now=None) -> list；charge(group, shares, now) -> dict；usage(group, window) -> dict；global_free(now=None) -> int；loan_status(loan_id, now=None) -> dict；snapshot() -> dict；restore(state) -> None；stats() -> dict。拒绝统一抛 CpuBrokerError(code, detail)。

返回结构与入参：set_quota 返回 group、quota、period 与 epoch，epoch 从 1 起每次改配加一；borrow 返回 loan_id、group、shares、start、expire 与 status，编号从 loan-1 起递增，到期时刻落在下一个周期边界；return_loan 返回 loan_id、group、shares 与 status；charge 返回 group、period、used、quota 与 borrowed；usage 的 window 为 (start, end)，按左闭右开统计，返回 group、start、end、used 与 quota；loan_status 返回借据全部字段，过期后状态为 expired；reclaim 返回到期借据列表；snapshot 返回 version、groups、loans、charges、counter 与 last_now；stats 返回 groups、open_loans、borrowed、used、returned 与 expired。

故障与边界：一、借出份额超过全局余量必须拒绝，余量按总额度减去各组基础配额与未还借据计算；二、归还只能归还借据自己那一组，不能按调用方所在组记账；三、周期边界用传入时刻与每组期长对齐，不得读墙钟；四、借入方长期占用不还时，到期借据要自动回收，未还份额不得无限期占用；五、回收按到期时刻排序，不按借据创建顺序；六、单组配额低于最小粒度、不是粒度整数倍、借入份额不是粒度的正整数倍、或期长越界都要拒绝；七、统计窗口跨周期要拒绝，同一段用量不得在两个周期重复计数；八、用量超过配额加未还借据之和要拒绝，超限部分不得入账；九、借入方在周期中途退出时，未用完的借入份额要在当前周期内归还，不能拖到下一周期继续占用；十、全局余量要扣除所有未到期借据，不能只扣除本周期已经用掉的量；十一、并发借入不得超卖，成功借出的份额之和不得超过余量；十二、快照保存每组基础配额、期长、未还借据与累计用量，重启后借据继续按到期回收，额度不因重启而放宽。

硬约束：borrow、return_loan、reclaim 全程持锁，十路并发借入时成功份额合计不得越过全局余量；快照恢复后借据状态与到期时刻逐项一致，已回收的借据不得复活；组数、借据数与单组配额都是硬顶。

验收：python3 -m pytest -q tests 的 15 条必须全过，不得修改测试、跳过或按固定输入写死；超卖与重启计息自行验证；拒绝码用 CPU_QUOTA_TOO_SMALL、CPU_QUOTA_INVALID、CPU_PERIOD_INVALID、CPU_GROUP_INVALID、CPU_GROUP_LIMIT、CPU_QUOTA_OVERCOMMIT、CPU_SHARES_INVALID、CPU_GROUP_UNKNOWN、CPU_LOAN_LIMIT、CPU_GLOBAL_EXHAUSTED、CPU_LOAN_UNKNOWN、CPU_LOAN_CLOSED、CPU_USAGE_EXCEEDED、CPU_WINDOW_INVALID、CPU_SNAPSHOT_INVALID；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 困难
- module: cpu_bandwidth_broker
- typeName: CpuBandwidthBroker
