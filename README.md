# batch48-py-game-b-16

## 题面

雨夜关卡合并批次上线后，同屏八万粒子的 draw call 从上千降到几十，画面却出了问题：本该压在地面雾气上面的雨丝换了层，几片整批消失，日志里批次数量每次重跑都不一样；同一处爆裂被重复触发时旧火星不消失，越打越厚；性能组把一大群雨滴手动切成几段之后，下一次合批又把它们揉回一批。范围是 gsb/batch48-py-game-b-16 下的 particle_batch_packer.py，类 ParticleBatchPacker，负责按材质与混合模式归并粒子、按上限分块、手动切分以及实例偏移记账。

公开接口：`ParticleBatchPacker(*, max_materials=4096, max_burst_particles=100000, max_batches=1024)`；`add_burst(emitter_id, particles) -> dict`；`build(max_instances=65536) -> list[dict]`；`split(batch_id, parts) -> list[dict]`；`order_key(particle) -> tuple`；`instances(batch_id) -> list[dict]`；`batches() -> list[dict]`；`remove_burst(emitter_id) -> bool`；`stats() -> dict`；`snapshot() -> dict`；`restore(state) -> None`；`reset() -> None`。拒绝统一抛 ParticleError(code, detail)。

边界与故障：一、粒子是带 material、blend 与可选 seq 的字典，material 必须非空，blend 只接受 alpha、additive、multiply，缺字段或取值非法抛 PARTICLE_PARTICLE_INVALID；发射器标识去空白后不得为空，否则抛 PARTICLE_EMITTER_INVALID，粒子列表为空抛 PARTICLE_BURST_INVALID。add_burst 返回 emitter_id、count 与升序去重的 materials，build 每个批次返回 batch_id、material、blend、count、first_instance 与 emitters，instances 每项返回 emitter_id 与 seq，batches 返回当前批次表的深拷贝。二、单次爆发粒子上限 max_burst_particles，超出抛 PARTICLE_BURST_LIMIT；材质键总数上限 max_materials，超出抛 PARTICLE_MATERIAL_LIMIT；两类拒绝都不得改动已有爆发与当前批次。三、order_key 返回 (material, blend, seq)，seq 缺失取 -1，同一粒子两次调用结果一致。四、build 按 (material, blend) 升序成批，混合模式不同的粒子即使材质相同也不得进同一批次。五、同一发射器内实例顺序按入参顺序保持；不同发射器的同材质同混合模式粒子按发射器 ID 升序拼接，批次的 emitters 字段按该顺序列出。六、单批实例数硬顶 min(max_instances, 65536)，超出就连续分块而不是截断，batch_id 由材质、混合模式与组内块序号用竖线连接；max_instances 不在 1 到 65536 之间抛 PARTICLE_MAX_INSTANCES_INVALID。七、批次首实例偏移在整次 build 内全局连续累计，前一批用掉的槽位不能被后面的批次重复占用；同一发射器重复爆发时新批次整体替换旧批次，不与旧粒子拼接。八、分块产生的尾批即使不足上限，首实例偏移也按前序实例累计，不得从零重算。九、split 把一批按 parts 均分，实例数除以 parts 的余数份各多一个实例，子批次 ID 为父 ID 加井号加序号，返回子批次列表并替换父批次；parts 小于 2 或大于该批实例数抛 PARTICLE_SPLIT_INVALID，未知批次抛 PARTICLE_BATCH_UNKNOWN。十、手动切分后的子批次在下一次 build 中保持切分状态，不再被合并回父批次。十一、单次 build 批次数上限 max_batches，超出上限的粒子计入 stats 的 overflow，而不是无限分块。十二、snapshot 覆盖爆发表、当前批次、切分结果与溢出计数，必须是深拷贝；restore 遇非法结构抛 PARTICLE_SNAPSHOT_INVALID 且原状态不变。

硬约束：add_burst 与 build 会被渲染线程与加载线程并发调用，不得丢爆发或读到半更新的批次表；快照恢复后批次划分、实例偏移与发射器顺序必须与中断前一致；材质表、单次爆发与批次总数都有硬上限，不允许无界增长。stats 固定返回 batches、instances、materials、overflow 四个整数，instances 只统计当前已入批实例，materials 为当前材质键数。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与快照恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: particle_batch_packer
- typeName: ParticleBatchPacker
