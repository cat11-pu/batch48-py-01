# batch48-py-game-a-34

## 题面

团战指挥链上线后，小队经常收到互相打架的指令：后一条命令把整条队列顶掉，两个单位被塞进同一个编队槽位，单位阵亡后新兵立刻继承它的指令上下文，士气已经归零的队伍还在冲锋，解散的队伍偶尔还会被派单，指挥面板上的队列顺序与单位实际执行顺序经常对不上。问题集中在 gsb/batch48-py-game-a-34 的 squad_order_coordinator.py，类 SquadCoordinator 负责编队、下达指令、领取执行、状态回报与士气维护。

公开接口：SquadCoordinator(*, formations=None, morale_cap: int = 100)；form(squad, members, formation="line") -> dict；disband(squad) -> dict；issue(squad, order, targets=None, *, priority=0) -> dict；next_order(squad) -> dict 或 None；report(squad, unit, status) -> dict；morale(squad, delta) -> dict；slots(squad) -> dict；queue(squad) -> list；roster(squad) -> dict；idle_units(squad) -> list；snapshot() -> dict；restore(state) -> None。拒绝统一抛 SquadError(code, detail)；roster 每项返回 slot、alive、busy 三个字段，slots 覆盖编队全部槽位、空槽取 None；form 返回 squad、formation 与 slots，issue 返回 order_id、kind、targets、priority 与 status，next_order 返回 order_id、kind、targets、priority 与 unit，morale 返回 squad、morale 与 collapsed，disband 返回 squad 与 status。

故障与边界：一、下达指令只追加到队尾，不得覆盖已有指令，队列按优先级降序、同级按下达先后排列，同优先级先下达的先执行；二、编队成员不得重复，一人不得占两处，超出编队槽数抛 SQUAD_FORMATION_FULL，编队名未知抛 SQUAD_UNKNOWN_FORMATION，同一小队重复编队抛 SQUAD_ALREADY_FORMED，成员列表为空抛 SQUAD_INVALID_MEMBERS，form 按成员顺序依次填入编队槽位，领取时按槽位顺序挑空闲单位；三、单位阵亡后其槽位要延迟复用，新单位不能直接继承旧单位的指令上下文；四、士气归零后拒绝新的进攻指令并抛 SQUAD_MORALE_BROKEN，崩溃触发的撤退指令只插到队首、不替换整条队列；五、同一单位已有一条待执行的移动指令时，再来一条移动指令抛 SQUAD_DUPLICATE_ORDER；六、指令目标不在编队或已经阵亡时抛 SQUAD_UNKNOWN_UNIT，空目标列表与未知指令类型抛 SQUAD_INVALID_ORDER；七、解散后该小队不再接受任何指令，查询与再次解散抛 SQUAD_UNKNOWN_SQUAD；八、next_order 一次只把一条指令交给一个空闲单位，没有空闲单位时返回 None 且指令留在队列，被领走的指令立即从队列移除并标记为执行中；九、report 把单位状态改为完成或阵亡后立刻释放占用，阵亡单位不再参与领单；十、士气增减后限制在零到上限之间，归零与回升都要翻转崩溃标记；十一、快照返回 version 为 1 的字典，含 squads 一段与下一个指令序号，结构非法抛 SQUAD_SNAPSHOT_INVALID 且原状态不变。

硬约束：并发下达与领取时同一条指令只能被一个单位领走，不得出现两个单位执行同一条命令，也不得同一单位同时挂两条指令；重启恢复后队列顺序、槽位归属与士气与中断前一致，后续指令序号继续递增，领单与回报交错时单位占用标记必须与队列一致。

验收：python3 -m pytest -q tests 的 15 条测试必须全过，不得改测试、跳过或按固定输入写死；并发领单与重启恢复自行验证；只做本地实现，接口名与错误码保持上面列出的写法，指令序号在恢复后继续递增。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 困难
- module: squad_order_coordinator
- typeName: SquadCoordinator
