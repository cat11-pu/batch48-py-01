# batch48-py-safety-b-14

## 题面

编排平台这周复盘三次重启风暴：同一容器每次都固定一秒后拉起，指数退避根本没生效；容器只跑了十秒就被判稳定，计数清零后又开始疯狂重启；内存不足与非零退出套用同一策略，内存不足反而拉得更急；重启预算早就用完，控制器仍然继续拉起；时钟跳变时算出了负的等待时间；并发上报同一份退出事件，退避轮次被加了两次；手工重启后预算还是满的，异常退出反而能无限续命；崩溃判定只看次数不看时间窗，凌晨的抖动会拖累早高峰。范围是 gsb/batch48-py-safety-b-14 的 restart_supervisor.py，类 RestartSupervisor，负责容器退出上报、指数退避、稳定期判定、窗口化重启预算与快照恢复，所有拒绝抛 RestartSupervisorError(code, detail)。

公开接口：RestartSupervisor(*, base_delay=1.0, factor=2.0, max_delay=60.0, budget=5, window=600.0, stable_period=120.0, oom_multiplier=2.0, clock=None)；configure(container_id, base_delay=None, factor=None, max_delay=None, budget=None, window=None, stable_period=None, oom_multiplier=None) -> dict；report_exit(container_id, code, at) -> dict；next_delay(container_id, now) -> dict；allow_restart(container_id, now) -> dict；mark_stable(container_id, uptime) -> dict；budget(container_id, now=None) -> dict；reset(container_id) -> dict；get(container_id) -> dict；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。

退避轮次从 1 起，第 n 轮延迟为 base_delay 乘 factor 的 n 减一次方，超过 max_delay 取 max_delay，退出码 137 视为内存不足，再乘 oom_multiplier 后仍取上限；退出码 78 视为配置错误，直接进入终止态，不参与退避。report_exit 返回 container_id、code、at、round、delay、next_at、state 与 duplicate，首次上报自动登记默认策略；next_delay 返回 container_id、delay、round、next_at 与 state，延迟不为负；allow_restart 返回 container_id、allowed、restarts_used、remaining、window_start 与 at；mark_stable 返回 container_id、round、state 与 uptime；budget 返回 container_id、budget、used、remaining、window 与 window_start，window_start 取窗口内最早一次重启时刻；get 返回策略与运行态，未知标识抛 RESTART_UNKNOWN_CONTAINER；容器运行态取 idle、backoff、stable 与 terminated 四种；reset 返回 state、round 与 remaining，重置后状态为 idle；stats 固定返回 containers、exits、restarts、stable_marks、resets、budget_exhausted。

故障与边界：一、退避必须按轮次指数增长，不得每轮从固定基数重来；二、稳定期未达到 stable_period 的标记按未稳定拒绝，轮次与下次时刻保持不变；三、内存不足退出与普通非零退出采用不同延迟，前者按 oom_multiplier 放大；四、窗口内的重启次数达到 budget 后拒绝放行，窗口按严格小于 window 的年龄保留；五、next_delay 在时钟回拨时把延迟压到零，绝不返回负数；六、同一个退出事件被并发上报只记一次，轮次不得加两次；七、同一容器一次放行后必须先上报新的退出或标记稳定，才能再次放行；八、手工 reset 清空退避轮次、窗口内重启记录与终止标记，把预算恢复满额；九、没有退出记录就放行按未武装拒绝，未知容器按未知标识拒绝；十、快照覆盖每个容器的策略、轮次、下次时刻、窗口内重启时刻、放行闸门与终止标记，恢复后继续退避而不是立刻拉起；十一、容器运行超过稳定期后崩溃，退避轮次要清零，但窗口化重启预算仍按窗口保留，两者不能一起清；十二、退出码表示配置错误时直接进入终止态，不再消耗重启预算。

快照返回 version 与 containers，containers 以容器标识为键，内容为深拷贝；恢复后 next_delay 与恢复前一致、未到时刻不得放行，结构非法抛 RESTART_SNAPSHOT_INVALID 且原状态不变。硬约束：report_exit 与 allow_restart 全程持锁，并发放行同一容器只能成功一次，并发上报同一退出事件只能记账一次；预算、窗口与轮次在快照恢复后必须连续，不得回退；容器数量与单容器窗口内重启记录都是硬顶。

拒绝码取 RESTART_CONTAINER_INVALID、RESTART_EXIT_INVALID、RESTART_CLOCK_BACKWARDS、RESTART_UNKNOWN_CONTAINER、RESTART_TERMINATED、RESTART_UNSTABLE、RESTART_TOO_EARLY、RESTART_BUDGET_EXHAUSTED、RESTART_IN_PROGRESS、RESTART_NOT_ARMED、RESTART_SNAPSHOT_INVALID。验收：python3 -m pytest -q tests 的 15 条全部通过，不得修改测试、跳过或按固定输入写死；稳定期与预算交互、并发放行自行验证；只做本地实现，不涉及上游提交。
以上现象均在真实生产环境复现。
## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 地狱
- module: restart_supervisor
- typeName: RestartSupervisor
