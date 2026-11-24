# batch48-py-game-a-32

## 题面

团本换装后治疗量忽高忽低，同样的法术打在同一名坦克身上，有时被护甲吃满、有时直接扣到零血；真实伤害技能反而被抗性挡掉一部分，护盾数字对不上，删掉一层减伤后结算结果没有变化，改成两层百分比减伤时又出现了负伤害，团队副本的伤害日志与面板长期对不上，策划要求每次结算都能按同一顺序复现。问题集中在 gsb/batch48-py-game-a-32 的 mitigation_pipeline.py，类 MitigationPipeline 负责登记承伤目标、维护减伤层与护盾、按固定次序结算单次伤害。

公开接口：MitigationPipeline(*, max_layers: int = 32)；register_target(target, *, hp=100) -> dict；set_hp(target, hp) -> dict；add_layer(target, layer) -> dict；remove_layer(target, layer_id) -> dict；layers(target) -> list；add_shield(target, shield_type, amount) -> dict；shield_absorb(target, amount, damage_type="physical") -> int；resolve(target, incoming, damage_type="physical") -> dict；effective_hp(target) -> int；snapshot() -> dict；restore(state) -> None。拒绝统一抛 MitigationError(code, detail)；resolve 返回 incoming、final、absorbed、shield_remaining、capped 与按结算次序排列的 layers_applied；register_target 与 set_hp 返回 target 与 hp，add_layer 返回该层，remove_layer 返回 removed 与剩余层，add_shield 返回 shield_type 与 shield_remaining。

故障与边界：一、结算次序固定为穿透、护甲、抗性、乘法减伤、上限、护盾，穿透只削护甲、不得放到抗性之后；二、同类型抗性按加法叠加并封顶百分之百，任意组合都不得算出负伤害，每一步结算结果向下取整；三、乘法减伤逐层相乘，两层五成只剩四分之一；四、减伤上限决定最终伤害下限，护甲再高也不能低于入场伤害乘剩余比例，上限只约束减免总量；五、真实伤害绕过全部减免与护盾，只受最终上限约束；六、护盾只按剩余伤害封顶吸收，余额不能被后续其它类型的伤害复用；七、层级移除后必须立刻按剩余层重算，移除不存在的层抛 MITIGATION_UNKNOWN_LAYER，重复移除同样报错；八、层标识重复、类型未知、比例为负或大于一，分别抛 MITIGATION_DUPLICATE_LAYER 与 MITIGATION_INVALID_LAYER；九、目标未知抛 MITIGATION_UNKNOWN_TARGET，伤害或护盾数额为负抛 MITIGATION_INVALID_AMOUNT，未注册目标不能加层；十、effective_hp 返回生命与各类型护盾余额之和，护盾吸收后余额同步下降，多类型护盾分开记账；十一、快照返回 version 为 1 的字典，含 targets 一段，每个目标带 hp、layers 与 shields，结构非法抛 MITIGATION_SNAPSHOT_INVALID 且原状态不变。

硬约束：同一目标的并发结算与层级移除不得读到半更新的层表，任一结算结果必须与它自己报告的 layers_applied 自洽，不能出现算了某层却没记入列表；快照恢复后同一输入必须算出同一结果，护盾余额一致；层数与护盾条目受上限约束，超出上限直接拒绝，不允许无界增长，每次结算都要按层表当前内容完整走一遍，不得跳过未命中的层。

验收：python3 -m pytest -q tests 的 15 条测试必须全过，不得改测试、跳过或按固定输入写死；并发结算与重启恢复自行验证；只做本地实现，接口名与错误码保持上面列出的写法，结算顺序以层表与快照为准。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 困难
- module: mitigation_pipeline
- typeName: MitigationPipeline
