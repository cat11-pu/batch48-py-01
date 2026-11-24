# batch48-py-safety-b-07

## 题面

节点内存调解器上线两周，复盘发现三类事故：一个用量平稳但增长很快的控制组直到被内核 OOM 才报警，而另一个刚过高水位、用量已经停止变化的组却被反复扩缩容；压力指标打到 80 的组在绝对用量还不到高水位时始终被判定正常；真正要杀进程时，值班脚本把占用最大、但身负保护记分的进程选中，业务主进程被清掉。跨组排查还发现候选列表里混进了别的租户的进程，回收之后同一批候选仍被再次挑中；嵌套控制组用量在父组里被逐层累加，60G 被算成 120G，触发误告警；采样窗口配置成零时调解器直接除零崩掉，时钟回拨送来的旧样本也被照收；调解器重启后高水位阈值被改回默认，节流标记和已选候选集全部清空。范围是 gsb/batch48-py-safety-b-07 的 memory_pressure_mediator.py，类 MemoryPressureMediator，负责按控制组采集用量与压力指标、分级判定、节流、回收与 OOM 候选仲裁。

公开接口：MemoryPressureMediator(*, window=3, high_watermark_ratio=0.9, max_groups=16, clock=None)；register_group(group, tenant, limit, high=None) -> dict；list_groups() -> list；observe(group, sample) -> dict；classify(group) -> str；pressure(group) -> dict；throttle(group, target) -> dict；clear_throttle(group) -> dict；throttle_state(group) -> dict；pick_oom_victim(group) -> dict；reclaim(group, nbytes) -> dict；group_usage(group) -> int；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 MemoryPressureError(code, detail)。

返回结构与入参：sample 含 usage、tick、pressure（some10 与 full，取值 0 到 100）与 procs（每项含 pid、rss、score、tenant），可选 limit；分级取 normal、elevated、critical，增速为当前用量减窗口内最旧用量，外推值为当前用量加正的增速，压力 full 达到 60 且仍增长时升一级，候选同分先比常驻内存再比进程号小的优先；register_group 返回 group、tenant、limit 与 high，high 省略时按 limit 乘 high_watermark_ratio 取整；pressure 返回 level、usage、limit、high、growth、projected 与 samples；throttle 与 throttle_state 返回 group、throttled、target 与 revision；clear_throttle 返回 group、throttled 与 target；pick_oom_victim 返回 pid、group、tenant、rss 与 score；reclaim 返回 group、freed、usage 与 throttled；snapshot 返回 version、groups 与 stats；stats 返回 groups、samples、picks 与 reclaimed；reset_stats 只清零 samples、picks 与 reclaimed。

故障与边界：一、分级要看窗口内的增速并按当前用量外推，不能只看绝对用量；二、用量达到上限、或外推后触顶必须判最高一级，已超高水位不得判正常；三、OOM 候选先按记分排序再比常驻内存，不能只按常驻内存；四、候选只在本组租户范围内挑，跨租户的进程不得入选；五、节流之后回收要复查水位，降到高水位以下自动解除，仍高于高水位不得自说自话解除；六、子控制组用量已经汇总进父组，逐层相加会重复计费并误判超限；七、采样窗口为零时不能进入除法，构造阶段就拒绝；八、时间戳回退或重复的样本要拒收，不能覆盖最新窗口；九、用量为负、上限非正、重复进程号、超范围压力值、缺字段的样本一律拒绝；十、已经交出的受害者不得重复选中，候选耗尽要明确报错；十一、记分调整为负的保护进程即使常驻内存最大也不能被选为受害者；十二、快照要保存每组阈值、最近采样窗口、节流标记与已选候选集，重启后阈值不放宽、候选集不丢失。

硬约束：observe 与 pick_oom_victim 交错时不得重复交出同一进程，八路并发采集下窗口计数与统计必须精确；快照恢复后分级结果与节流目标要和恢复前逐项一致；控制组数量、窗口长度与候选集合都是硬顶。

验收：python3 -m pytest -q tests 的 15 条必须全过，不得修改测试、跳过或按固定输入写死；嵌套用量与并发仲裁自行验证；拒绝码用 MEMORY_GROUP_INVALID、MEMORY_GROUP_DUPLICATE、MEMORY_GROUP_LIMIT、MEMORY_THRESHOLD_INVALID、MEMORY_SAMPLE_INVALID、MEMORY_SAMPLE_STALE、MEMORY_UNKNOWN_GROUP、MEMORY_NO_SAMPLE、MEMORY_TARGET_INVALID、MEMORY_RECLAIM_INVALID、MEMORY_NO_VICTIM、MEMORY_SNAPSHOT_INVALID；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 困难
- module: memory_pressure_mediator
- typeName: MemoryPressureMediator
