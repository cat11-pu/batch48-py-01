# batch48-py-game-b-07

## 题面

远景地形在镜头推近时整片跳档，影子跟着闪一下；贴近的物件反而迟迟不细化，玩家在同一个位置来回移动时模型在两个档位之间反复横跳；团战里十几个对象同时换档，过渡期多出来的绘制把预算吃穿，帧率掉到 40；更奇怪的是手工锁定画质档之后，物体卡在低模不再恢复，关掉锁定也不按当前视口大小重新判定。范围是 gsb/batch48-py-game-b-07 下的 lod_transition_guard.py，类 LodTransitionGuard，负责档位选择、滞回、交叉淡入、阴影档位与过渡预算。

公开接口：`LodTransitionGuard(*, levels=4, thresholds=(0.5, 0.2, 0.05), hysteresis=0.1, transition_frames=4, transition_budget=2, shadow_offset=1)`；`select_lod(entity, screen_ratio) -> int`；`advance(entity, frames=1) -> dict`；`draw_state(entity) -> list[dict]`；`transition_of(entity) -> dict`；`force_lod(entity, level) -> int`；`clear_force(entity) -> None`；`budget_left() -> int`；`reset_budget(amount=None) -> int`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。冲突一律抛 LodError(code, detail)。

故障与边界：一、档位按屏幕占比与 thresholds 从细到粗划分，占比大于等于 0.5 为 0 档，其余依次落到更粗档；稳定期 transition_of 的 phase 为 stable、from 与 to 为 None、alpha 为 1.0，draw_state 返回 main 与 shadow 两条。二、换档带 hysteresis 滞回：往粗档切要求占比严格小于当前档阈值减滞回值，往细档切要求占比大于等于目标档阈值加滞回值，带内维持原档。三、接受换档且仍有过渡预算时，在 transition_frames 帧内两档并行绘制，新档 alpha 从 0 线性升到 1，旧档 alpha 为 1 减新档，两条 alpha 之和恒为 1。四、过渡期 draw_state 返回四条，先 main（旧档在前、新档在后）再 shadow（同序）；阴影档位为主档加 shadow_offset，超过最粗档按最粗档封顶。五、每次过渡消耗一份 transition_budget，预算耗尽后换档立即生效、不再产生过渡绘制；预算整场景共享，不按对象重置。六、过渡期内占比回到原档要取消这次过渡并回到单档绘制，不能三档叠加。七、预算耗尽直接切换时淡入淡出 alpha 必须归零，不能停在半透明。八、force_lod 跳过滞回直接指定档位但保留过渡，非法档位抛 LOD_LEVEL_INVALID；clear_force 对没有覆盖的对象抛 LOD_NOT_FORCED，解除后按滞回带重新判定，不能继承覆盖时的档位。九、未知对象的查询、推进与覆盖抛 LOD_UNKNOWN_ENTITY；占比不是 0 到 1 的数值抛 LOD_RATIO_INVALID；advance 帧数小于 1 抛 LOD_FRAMES_INVALID；构造参数非法抛 LOD_CONFIG_INVALID。十、snapshot 覆盖每个对象的档位、过渡进度、强制标记、剩余预算与统计，深拷贝；restore 遇到非法结构抛 LOD_SNAPSHOT_INVALID 且原状态不变，合法时半程过渡能继续推进。十一、stats 固定六个整数：selections、transitions、instant_switches、completed、cancelled、forced；reset_stats 只清计数；reset_budget 不带参数回到构造预算，负数抛 LOD_BUDGET_INVALID。

硬约束：过渡预算与档位数量都有上限，禁止无界绘制；快照跨进程恢复后不得与恢复前的对象共享可变结构，恢复后的过渡必须能从半程继续；同一对象同一帧多次选择档位时状态必须收敛到最后一次结果，不能叠加绘制。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；过渡与恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: lod_transition_guard
- typeName: LodTransitionGuard
