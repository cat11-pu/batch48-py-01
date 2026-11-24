# batch48-py-game-a-18

## 题面

视野系统改造后王城集市的客户端开始莫名闪人：一队玩家从相邻格子连续走过，屏幕上先跳出一个人影又立刻消失，第二帧才重新出现；同一屏里怪多的时候，靠近的守卫和远处的旗帜被一起裁掉，等玩家走开一点，被裁的旗帜才补进来，可补进来的那次又和最初那次重复，头像列表里出现两个同样的条目；观察者自己跨格传送后，界面把原本就看得见的摊位全部重新弹了一遍入场动画；掉线重连恢复存档时事件序号从头开始，客户端把旧事件当新事件又播一次；订阅表随在线人数线性增长，始终没有上限。范围是 gsb/batch48-py-game-a-18 的 aoi_subscription_guard.py，类 AoiSubscriptionGuard，负责按格维护订阅、可见性预算裁剪与事件序号。

公开接口：AoiSubscriptionGuard(*, radius=1, leave_lag=1, budget=4, cell_size=10.0)；cell_of(x, y) -> tuple；add_observer(observer_id, *, x, y, budget=None) -> dict；move_observer(observer_id, *, x, y) -> dict；add_entity(entity_id, *, x, y, kind="npc", priority=0) -> dict；move_entity(entity_id, *, x, y) -> dict；remove_entity(entity_id) -> dict；visible(observer_id) -> list；subscribers(entity_id) -> list；budget(observer_id) -> int；set_budget(observer_id, limit) -> dict；end_tick() -> dict；next_seq() -> int；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 AoiError(code)。

要修的点：一、格子由坐标对 cell_size 取下整得到，负数也按下整；二、观察者注册返回 observer、cell、radius、budget，实体注册返回 entity、cell、kind、priority，重复标识抛 AOI_DUPLICATE，空标识抛 AOI_INVALID_ARGUMENT；三、可见判定用切比雪夫格距，新建订阅只要求距离不超过 radius，已经订阅的实体要超过 radius 加 leave_lag 才退订；四、move_entity 返回 entity、from、to、entered、left，entered 与 left 是本次新增与移除的观察者个数，迁移先加新格再减旧格；五、每个观察者的可见实体数受预算硬顶，超限时按优先级降序、再按格距升序、再按实体名升序保留，其余不订阅；六、set_budget 放宽预算后按同一次序重新计算，被裁剪的实体恢复订阅，limit 小于 1 抛 AOI_BUDGET_INVALID；七、move_observer 重算该观察者的可见集合，返回 observer、from、to、visible，visible 是重算后的条数，已经订阅且仍可见的实体不重发 enter；八、remove_entity 解除全部订阅并返回 entity 与 state removed，之后再查或再删抛 AOI_UNKNOWN_ENTITY；九、end_tick 返回 tick 与 events，事件形如 seq、kind、observer、entity，kind 取 enter 或 leave，按观察者、实体、种类升序排列，序号从 1 开始全局递增，同一 tick 内的净变化只产出一次事件；十、同一 tick 内实体连续跨两格要合并成一次 enter，以起点到终点算一次迁移，不产生中间格的 enter 与 leave 抖动；十一、可见集合与事件流分开记账，因预算被裁剪的实体在预算释放后补发 enter，且不与先前的 enter 重复；十二、stats 固定返回 ticks、enters、leaves、trimmed、events 五个整数，reset_stats 归零但不清订阅表与序号；十三、snapshot 返回 version=1 的深拷贝，restore 后订阅表、tick、序号与统计一致，恢复后事件流接着原有编号，结构非法抛 AOI_SNAPSHOT_INVALID 且不破坏当前状态。

硬约束：add、move、remove、set_budget、end_tick 与快照读写全程持同一把锁，八路并发移动下 visible 与 subscribers 互为反向且不出现半订阅；快照跨进程恢复后不得与恢复前的对象共享可变结构；每个观察者的订阅数受预算硬顶，订阅表不随 tick 数增长。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发移动与恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- module: aoi_subscription_guard
- typeName: AoiSubscriptionGuard
