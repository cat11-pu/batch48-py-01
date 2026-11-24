# batch48-py-game-b-15

## 题面

角色换装高面数模型后顶点解算超预算，导入表里有的顶点带十二个骨骼影响，权重和只有零点九七，蒙皮后肩部出现塌陷；同一顶点重复登记两根同号骨骼，裁剪后权重凭空翻倍；权重和为零的坏顶点被悄悄换成根骨骼，角色整块面片黏在原点；换装工具每次导入的裁剪顺序都不一样，美术无法对比两次结果。范围是 gsb/batch48-py-game-b-15 下的 skin_influence_pruner.py，类 SkinInfluencePruner，负责影响集裁剪、重复骨骼合并、权重归一化、骨骼重映射与错误表。

公开接口：`SkinInfluencePruner(*, max_bones=65535, max_influences=32, max_vertices=1000000)`；`add_vertex(vertex_id, influences) -> dict`；`prune(vertex_id, max_bones=4) -> list`；`normalize(weights) -> list`；`remap_bones(mapping) -> int`；`vertex(vertex_id) -> dict`；`kept(vertex_id) -> list | None`；`errors() -> dict`；`remove_vertex(vertex_id) -> bool`；`stats() -> dict`；`reset_stats() -> None`；`snapshot() -> dict`；`restore(state) -> None`。拒绝统一抛 SkinInfluenceError(code, detail)。

故障与边界：一、vertex_id 非负且唯一，重复抛 SKIN_DUPLICATE_VERTEX，未登记顶点抛 SKIN_UNKNOWN_VERTEX；influences 为骨骼索引与权重的二元组序列。二、骨骼索引必须在零到 max_bones 之间，权重必须为有限非负数，越界分别抛 SKIN_BONE_RANGE 与 SKIN_WEIGHT_INVALID。三、同一顶点出现重复骨骼索引时先合并权重再裁剪，不得各留一条。四、保留按权重降序、骨骼索引升序；权重完全并列时保留索引小者；裁剪后权重全部相同且索引并列时顺序仍按索引升序可复现。五、prune 的 max_bones 必须是一到 max_influences 之间的整数，非法抛 SKIN_MAX_BONES_INVALID。六、裁剪后按保留集重新归一化，权重和与一的误差不超过 1e-6，normalize 对全零输入抛 SKIN_ZERO_WEIGHT，对负数或空输入抛 SKIN_WEIGHT_INVALID。七、权重和为零的顶点整条拒绝并记入错误表，错误项含 code 为 SKIN_ZERO_WEIGHT，不得用默认骨骼顶替，也不得进入 kept。八、错误表按 vertex_id 索引，重映射之后这些顶点仍留在错误表里且不参与输出。九、prune 不得改动原始影响集，同一输入两次 prune 返回逐项一致的结果，连元组顺序都必须相同。十、remap_bones 返回真正发生索引变化的顶点数，目标骨骼越界抛 SKIN_BONE_RANGE 且原映射不变，已算出的保留列表在重映射后作废。十一、上限：单顶点影响数 max_influences、顶点数 max_vertices，超限抛 SKIN_INFLUENCE_LIMIT 与 SKIN_VERTEX_LIMIT；stats 固定返回 vertices、pruned_influences、affected_vertices、rejected_vertices、prunes 五个整数，reset_stats 只清计数。十二、snapshot 覆盖 version、vertices、kept、errors、stats，必须深拷贝；restore 遇非法结构抛 SKIN_SNAPSHOT_INVALID 且原状态不变。

硬约束：add_vertex、prune、remap_bones 全程持锁，多路并发裁剪与重映射只能看到一致的骨骼映射，不得出现一条影响用旧索引、另一条用新索引的半更新结果；快照恢复后顶点表、保留集与错误表不得与快照共享可变结构，恢复后同一输入裁剪结果必须与恢复前一致。验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: skin_influence_pruner
- typeName: SkinInfluencePruner
