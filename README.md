# batch48-py-game-b-05

## 题面

地形批次换成大网格之后，客户端绘制调用的顶点缓存命中率一直上不去，同样的模型在美术工具里却是热的：索引表按建模顺序导出，相邻三角形几乎不复用顶点，硬件后置缓存被来回冲刷。优化工具上线后又出了新问题：重排之后某些子网格的绘制批次串了，蒙皮材质张冠李戴；原本顺时针的三角形被顺手翻成逆时针，背面剔除把整片地形裁掉；把缓存容量调成零做对照实验时，输出顺序每次都不一样，回滚脚本没法比对。

范围是 gsb/batch48-py-game-b-05 下的 vertex_cache_optimizer.py，类 VertexCacheOptimizer，负责网格载入、缓存容量配置、索引重排、命中率测量与顺序回滚。

公开接口：`VertexCacheOptimizer(*, max_cache_size=32, max_vertices=65536, max_triangles=4096, clock=None)`；`load_mesh(vertices, triangles) -> dict`；`set_cache_size(size) -> dict`；`cache_size() -> int`；`mesh_info() -> dict`；`submeshes() -> list`；`original_order() -> list`；`optimized_order() -> list`；`optimize() -> dict`；`measure_hit_rate(order=None) -> dict`；`restore_order() -> dict`；`stats() -> dict`；`reset_stats() -> None`；`snapshot() -> dict`；`restore(state) -> None`。拒绝统一抛 VertexCacheError(code, detail)。

故障与边界：一、vertices 必须是非空且互不重复的非负整数列表，长度超过 max_vertices 报 VCO_TOO_MANY_VERTICES；二、每个三角形要有正整数 id、非空 submesh、恰好三个整数索引与 ccw 或 cw 绕序，结构不对报 VCO_INVALID_MESH，id 重复报 VCO_DUPLICATE_TRIANGLE；三、索引不在顶点集内报 VCO_INDEX_OUT_OF_RANGE，三角形数超过 max_triangles 报 VCO_TOO_MANY_TRIANGLES；四、缓存容量取 0 到 max_cache_size，负值、超限与非整数报 VCO_INVALID_CACHE_SIZE，改容量必须让旧计划失效；五、未载入网格时调用 optimize、measure_hit_rate、restore_order 一律报 VCO_NO_MESH；六、optimize 必须返回全部三角形 id 的一个排列，同一输入重复优化结果逐字节一致，未改容量时第二次调用返回 reused 为真；七、measure_hit_rate 按后置缓存的先进先出窗口统计每个三角形三个索引的命中与未命中，total 是三角形数乘三，order 不是原三角形集合的排列时报 VCO_INVALID_ORDER；八、命中率报告要带 cache_hits、cache_misses、hit_rate、cache_size 与是否复用；九、restore_order 返回载入时的原始顺序并清掉当前计划，stats 的 restores 加一；十、索引重排不得改变每个子网格的索引区间，绘制批次不能串；十一、绕序翻转的三角形要按原绕序输出，优化器不得顺手统一绕序；十二、snapshot 是深拷贝，restore 结构非法报 VCO_SNAPSHOT_INVALID 且原状态不变。

硬约束：load_mesh、set_cache_size、optimize、restore、snapshot 全程持锁，四路并发同时 optimize 只能产生一个计划且返回值完全一致，不能出现两个不同顺序；顶点数、三角形数与缓存容量都是硬顶，超限拒绝而不是截断；同一网格同一容量反复优化的输出必须逐字节一致，排序与并列裁决只能依赖载入顺序，不能依赖字典遍历或时间；快照恢复后顶点表、三角形表、原始顺序、当前计划与统计都要在，且不得与恢复前的对象共享结构。

验收：`python3 -m pytest -q tests` 的 15 条必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-b-05
- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- difficulty: 困难
- module: vertex_cache_optimizer
- typeName: VertexCacheOptimizer
