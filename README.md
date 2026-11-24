# batch48-py-game-a-27

## 题面

副本里的怪物最近很不讲道理：玩家贴在守卫背后三米处蹲着，守卫照样转身开打；隔着石墙也能被看见；隔壁房间敲一下锣，半张地图的怪物一起冲过来；玩家躲进柜子十分钟，怪物还能直奔藏身点；更奇怪的是两只怪物互相喊话，警报在它们之间来回弹，整层怪物永远停在战斗状态；警报解除后有的怪物直接从战斗跳回巡逻，巡逻路线与搜索路线全乱。负责视野的同学复现了两次，发现视野、听觉和记忆三套判定各写各的，警报是谁传出去的也没有落账。范围是 gsb/batch48-py-game-a-27 下的 perception_alert_guard.py，类 PerceptionGuard，负责视锥判定、听觉事件、记忆衰减、警报传播与状态查询。

公开接口：`PerceptionGuard(*, view_range: float = 15.0, view_angle_deg: float = 90.0, memory_ms: int = 4000, alarm_ttl_ms: int = 6000)`；`Place(npc, position) -> dict`；`SetStealth(target, factor) -> dict`；`Observe(npc, target, position, now) -> dict`；`Hear(npc, event) -> dict`；`Alert(npc, level, now=None) -> dict`；`Propagate(npc, radius, now=None) -> dict`；`State(npc, now) -> dict`；`Forget(npc) -> dict`；`Snapshot() -> dict`；`Restore(state) -> None`；`Stats() -> dict`。拒绝统一抛 PerceptionError(code, detail)，时间一律毫秒整数。

故障与边界：一、视锥同时判距离与角度，观测入参 position 给出 distance、bearing、blocked，bearing 相对朝向、范围 -180 到 180，超过半视角按 angle 拒绝；二、距离超过 view_range 按 range 拒绝，遮挡按 blocked 拒绝，两者在角度判定之后给出；三、潜行系数乘在距离上，distance 乘 factor 超过 view_range 按 stealth 拒绝，系数必须为正，否则抛 PERCEPTION_STEALTH_INVALID；四、可见时等级直接进入 combat，记录 last_seen、target 与 sight 来源，decay_at 为 now + memory_ms；五、听觉事件含 origin、distance、loudness、at，distance 大于 loudness 视为听不见，返回原因 faint，听得见只把等级抬到 search，刷新衰减锚点且不降级；六、等级只允许 patrol、search、alert、combat，非法值抛 PERCEPTION_LEVEL_INVALID，Alert 按 now 刷新衰减锚点；七、警报传播只从 alert 或 combat 发起，半径内已放置的怪物提升到 alert 并记录 alarm 来源，来源已存在时跳过，回传不得成环；八、记忆按 memory_ms 逐级衰减，每满一个区间降一级，不能跳级，同一时刻重复查询也只降一级，降到 patrol 后 decay_at 清空，last_seen 保持不变；九、Forget 清空等级、目标、来源与衰减锚点；十、State 返回 npc、level、last_seen、target、sources、decay_at，sources 按首次出现顺序去重；十一、快照保存怪物状态、潜行系数与计数，必须是深拷贝，恢复后等级与锚点不回退，重启后能接着衰减；十二、恢复结构非法抛 PERCEPTION_SNAPSHOT_INVALID 且原状态不变。Observe 返回 visible、reason、effective_distance、level；Hear 返回 heard、reason、level；SetStealth 返回 target 与 factor；Alert 返回 npc 与 level；Propagate 返回 propagated_to 与 count；Forget 返回 npc 与 level。Stats 返回 observed、heard、propagated、decays 四个整数，Observe 每次调用都计入 observed，Propagate 按被唤醒的怪物数量计入 propagated。

硬约束：Observe 与 Propagate 交错执行时不得复活已经清除的警报，清除之后也不得被在途的传播重新点亮，所有接口共用一把可重入锁；传播需要记录来源，同源重复传播不得再次唤醒同一只怪物；快照恢复后衰减继续按原锚点推进，不能从恢复时刻重新计时。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；传播成环与降级路径自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- module: perception_alert_guard
- typeName: PerceptionGuard
