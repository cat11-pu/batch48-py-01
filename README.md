# batch48-py-safety-b-01

## 题面

上周滚动发布时，一批容器被摘掉就绪之后还接了两分钟的新连接，宿主机上留下了容器没带走的子进程；另一批容器因为宽限期被反复重置，一直退不出去，值班只能手工强杀。事后翻监控发现，摘流和发信号是两条各自为政的路径：就绪标记还没落地，终止信号已经发出去；宽限计时读的是墙上时钟，容器重启后从头再算一遍；信号只落在主进程上，同一进程组里的子进程继续跑，宿主机上越积越多。范围是 gsb/batch48-py-safety-b-01 下的 container_drain_orchestrator.py，类 DrainOrchestrator，负责单个容器从登记到排空结束的状态推进与信号编排，策略由 DrainPolicy 承载，字段为 grace_seconds、sigterm_delay、max_children，缺省分别是 30、5、16。

公开接口：DrainOrchestrator()；register(container_id, policy=None)；start(container_id)；begin_drain(container_id, now)；admit_request(container_id)；mark_idle(container_id, now)；add_child(container_id, pid)；note_exit(container_id, pid)；advance(now)；signals(container_id)；inspect(container_id)；backlog_count(container_id)；snapshot()；restore(state)。拒绝统一抛 DrainError(code, detail)。register 与 inspect 都返回容器信息，字段为 state、ready、alive、drain_started、deadline、stopping_at、exited_at、signal_count；start 与 begin_drain 返回推进后的状态字符串。

故障与边界：一、状态只允许 CREATED→RUNNING→DRAINING→STOPPING→EXITED 一路前进，回退或跳级报 DRAIN_INVALID_STATE；二、begin_drain 必须先把 ready 置假再进 DRAINING，重复调用报 DRAIN_ALREADY_DRAINING 且不得重置开始时刻，已退出的容器再排空报 DRAIN_ALREADY_EXITED；三、时间一律取调用方传入的 now，不得读系统时钟，宽限截止等于开始时刻加 grace_seconds；四、进程组按 pid 记账，主进程固定记 0，add_child 超过 max_children 报 DRAIN_CHILD_LIMIT，重复 pid 报 DRAIN_DUPLICATE_PID，未知 pid 退出报 DRAIN_UNKNOWN_PID；五、信号一律投递整组，记录含 signal、scope 为 group、递增 seq 与发出时刻；六、mark_idle 只在 DRAINING 有效，发出 SIGTERM 并进入 STOPPING；七、advance 在宽限截止时补发 SIGTERM，SIGTERM 之后超过 sigterm_delay 仍未全组退出发 SIGKILL，宽限截止加 sigterm_delay 仍不退出一律判 EXITED；八、整组进程退出才可提前判 EXITED，主进程先走但同组子进程或孙进程仍在时排空不算结束，必须按整组存活情况收尾；没有变化的容器 advance 返回空映射，重复推进不得重复发信号；九、已 EXITED 的容器再排空或再 mark_idle 报错，advance 不再产生信号；十、DRAINING 期间到达的新请求必须被拒并计入背压计数，不得静默接受后再拖长排空，被拒请求也不能混进在途统计；十一、策略中 grace_seconds 非正、sigterm_delay 为负、max_children 小于 1 报 DRAIN_INVALID_POLICY；十二、快照保存状态、开始时刻、截止时刻、pid 账本与信号序号，非法结构报 DRAIN_SNAPSHOT_INVALID 且原状态不变，恢复后宽限按绝对截止时刻续算而不是从零重计。

硬约束：begin_drain 与 advance 全程持锁，同一容器的并发排空只能成功一次，状态只前不退，信号序号连续且不重复，后到的调用要么等待要么读到已经推进后的状态；快照必须是深拷贝，恢复后不得与调用方共享可变结构；单个容器的子进程数受 max_children 硬顶，超限的登记不能先落地再报错。

验收：`python3 -m pytest -q tests` 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；并发推进与重启续期场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- module: container_drain_orchestrator
- typeName: DrainOrchestrator
