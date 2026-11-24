# batch48-py-game-a-25

## 题面

竞技场同屏单位数量翻倍后，群体避让开始出现怪异表现：一帧内所有单位两两查询邻居，预算早就爆了却还是全量算完，帧时间直接冲到三十毫秒以上；高优先级的精英怪被一圈低优先级小怪挤得原地后退；两个单位中心距还有一点二米，各自半径零点七米，重叠判定却认为没有接触；卡死检测按帧数计时，站着不动的单位五帧就被判定卡住，绕障碍的单位反而一直不算卡；脱困方向每帧随机，单位在墙角来回抖动；已经注销的单位在下一帧仍然参与计算；时间步传零时整个调度抛除零异常。范围是 gsb/batch48-py-game-a-25 下的 crowd_avoidance_scheduler.py，类 AvoidanceScheduler，负责登记单位、按预算查询邻居、计算分离向量、检测卡死并按固定规则脱困。

公开接口：AvoidanceScheduler(*, neighbor_budget: int = 32, cell_size: float = 2.0, stuck_epsilon: float = 0.01)；register(unit, *, radius=0.5, priority=0, position=(0.0, 0.0), goal=None, max_speed=1.0) -> dict；unregister(unit) -> dict；set_goal(unit, goal) -> dict；tick(*, dt_ms=16, budget=None, units=None) -> dict；neighbors(unit, radius) -> list；stuck(unit, window) -> bool；position(unit) -> tuple；state(unit) -> dict；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。所有拒绝抛 CrowdError(code, detail)。

register 与 state 返回 unit、radius、priority、position、goal、max_speed，unregister 返回 unit 与 removed，tick 报告含从一递增的 frame、moved、pairs_evaluated，moved 把单位映射到本帧位移的两元素列表。

故障与边界：一、radius 为正、位置为两个有限数、priority 为整数，违反抛 AV_INVALID_UNIT，位置非法抛 AV_INVALID_POSITION；二、重复登记抛 AV_DUPLICATE_UNIT，未登记单位在 tick、neighbors、set_goal、state 中抛 AV_UNKNOWN_UNIT；三、邻居查询只返回圆心距不超过查询半径的单位，按距离升序、距离相同按标识升序；四、重叠判定用圆心距与两个半径之和比较，接触后把低优先级单位沿连线推开，高优先级单位位置不变；五、dt_ms 必须为正，零或负数抛 AV_INVALID_DT；六、有目标的单位每帧最多移动 max_speed 乘 dt 秒的距离，无目标单位不移动；七、卡死按窗口内实际位移判断，位移小于 stuck_epsilon 才算卡死，窗口未满返回 False；八、tick 预算限制参与计算的邻居对数，pairs_evaluated 不得超过预算；九、已注销单位不得出现在同一帧的移动结果与邻居结果里；十、快照保存位置、优先级、目标、卡死窗口与帧号，恢复后下一帧继续递增且位置一致，结构非法抛 AV_SNAPSHOT_INVALID 且原状态不变。

硬约束：并发 tick 必须串行，每帧帧号唯一，十次并发 tick 后单位总位移等于十次单帧位移之和，不能丢帧或重复写位置；预算不足时必须按优先级裁剪参与计算的对，而不是按注册顺序截断；互相让路的对称僵局必须用与标识和位置绑定的确定性方向打破，不能靠随机抖动；stats 固定返回 ticks、pairs、separations、unsticks、registrations 五个整数，reset_stats 归零。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；预算裁剪与对称僵局自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
- module: crowd_avoidance_scheduler
- typeName: AvoidanceScheduler
