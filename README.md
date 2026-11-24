# batch48-py-game-a-31

## 题面

跨服副本开放后，装备相关工单一下堆到了策划那边：同一件戒指被同时戴在左右两个戒位，换装时旧护甲凭空消失，玩家背包里既没有旧件也没有新件，角色等级不够却能穿着史诗套装进场，还有玩家发现两件同部位的套装居然各算一件凑出了三件套效果，副本结算的攻击力与面板对不上。问题集中在 gsb/batch48-py-game-a-31 的 equipment_slot_resolver.py，类 EquipResolver 负责物品登记、背包记账、穿戴校验、套装计数与派生属性重算，玩家状态按标识隔离。

公开接口：EquipResolver(*, bag_capacity: int = 8, set_bonus_table=None)；register_item(item_id, *, part, level_req=1, two_handed=False, stats=None, enchant=None, set_id=None) -> dict；grant_level(player, level) -> int；add_to_bag(player, item_id, count=1) -> int；set_bag_capacity(player, capacity) -> int；bag(player) -> dict；worn(player) -> dict；validate(player, item_id, slot=None) -> dict；equip(player, item_id, slot=None) -> dict；unequip(player, slot) -> dict；swap(player, slot, item_id) -> dict；stats(player) -> dict；stats_version(player) -> int；snapshot() -> dict；restore(state) -> None。所有拒绝统一抛 EquipError(code, detail)；stats 返回 attack、defense、version 与 set_pieces，set_pieces 只列已穿戴的套装标识；equip 返回 slot、item 与 version，unequip 返回 item，swap 返回 removed 与 equipped，add_to_bag 与 grant_level 分别返回新数量与当前等级，set_bag_capacity 返回新容量。

故障与边界：一、双手武器与副手互斥，先穿双手再穿副手、先穿副手再换双手两个方向都要拒绝并保持原穿戴，槽位已被普通物品占用时抛 EQUIP_SLOT_OCCUPIED；二、左右戒位同属戒指部位，未指定槽位时先补左环再补右环，同一件物品不能同时占两个槽，槽位与部位不符也要拒绝；三、等级不足不得穿上，也不得扣减背包，等级补齐后同一件物品可以正常穿上；四、套装件数按穿戴中的不同部位计数，同一套两件同部位只能算一件，阈值加成按最高档一次性计入；五、替换装备时旧件必须回背包，背包已满则整次失败并保留原装备，不能出现物品消失；六、属性重算要计入附魔，卸下后附魔加成随之下落，每次成功变更让派生属性版本加一；七、同一物品重复穿戴抛 EQUIP_ALREADY_WORN，不在背包里抛 EQUIP_NOT_IN_BAG，换装时找不到新件同样拒绝；八、槽名未知或与部位不符抛 EQUIP_INVALID_SLOT，卸下空槽抛 EQUIP_UNKNOWN_SLOT，未知玩家抛 EQUIP_UNKNOWN_PLAYER，未知物品抛 EQUIP_UNKNOWN_ITEM；九、validate 只返回 ok 与 reasons、不落任何改动，拒绝码与 equip 保持一致；十、背包容量是硬上限，超出抛 EQUIP_BAG_FULL 且不吞物品，缩容不得少于已有数量；十一、快照返回 version 为 1 的字典，含 items 与 players 两段，players 下每位玩家带 level、bag、worn 与 stats_version；结构非法抛 EQUIP_SNAPSHOT_INVALID 且原状态不变。

硬约束：同一玩家的并发 Equip 只允许一件生效，另一路必须拿到明确的槽位占用错误，不能两件都记进槽位，也不能让同一件物品同时出现在背包和槽位；派生属性版本单调递增，快照恢复后重算结果与恢复前逐字段一致，恢复后再穿戴从原版本继续加一；背包与槽位上的物品总数守恒，任何失败路径都不允许物品凭空增加或减少，替换失败时两处数量与版本都不变。

验收：python3 -m pytest -q tests 的 15 条测试必须全过，不得改测试、跳过或按固定输入写死；并发穿戴与重启恢复自行验证；只做本地实现，接口名与错误码保持上面列出的写法。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 困难
- module: equipment_slot_resolver
- typeName: EquipResolver
