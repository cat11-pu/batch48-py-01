# batch48-py-game-b-13

## 题面

材质图编辑器把节点求值挪到渲染线程后，改一个常量整张图重算，拖拽节点时画面卡成幻灯片；两个节点互相引用直接把编辑器卡死，只能强退；同一个节点被多处引用时求值次数翻倍；折叠常量后改动一个被折叠的节点，预览仍显示折叠前的旧值，两个材质实例的结果还会串。范围是 gsb/batch48-py-game-b-13 下的 material_graph_evaluator.py，类 MaterialGraphEvaluator，负责节点图拓扑求值、常量子图折叠、版本缓存与环检测。

公开接口：`MaterialGraphEvaluator(*, max_nodes=4096, max_inputs=16, max_depth=512)`；`add_node(node_id, op, inputs=None) -> dict`；`connect(node_id, port, source_node, source_port="out") -> dict`；`set_input(node_id, name, value) -> dict`；`evaluate(node_id) -> float | list`；`evaluate_all() -> dict`；`fold_constants() -> int`；`invalidate(node_id) -> int`；`stats() -> dict`；`reset_stats() -> None`；`snapshot() -> dict`；`restore(state) -> None`。拒绝统一抛 MaterialGraphError(code, detail)。

故障与边界：一、节点 ID 唯一且非空，只允许字母、数字与下划线，重复抛 MATERIAL_DUPLICATE_NODE；未注册节点抛 MATERIAL_UNKNOWN_NODE；未知端口抛 MATERIAL_UNKNOWN_PORT；未知算子抛 MATERIAL_OP_UNSUPPORTED。二、add 与 mul 逐分量运算，dot 收敛为标量，scale 为向量乘标量，mix 按 t 线性插值，clamp 逐分量夹取，neg 取负，vec2 到 vec4 组装向量，length 求模；算子名之外一律按未知算子处理。三、输入维度必须一致，标量不能与向量混算，维度不符抛 MATERIAL_DIM_MISMATCH，detail 必须带 node 与 port。四、接边即检测环，自引用与多跳回边都抛 MATERIAL_CYCLE 并给环路径，不得等到求值才报。五、节点上限 max_nodes、单节点输入上限 max_inputs、求值深度上限 max_depth，超限分别抛 MATERIAL_NODE_LIMIT、MATERIAL_INPUT_LIMIT、MATERIAL_DEPTH_LIMIT，深度按输入链最长路径计算。六、每一步按 float32 舍入，折叠前后同一节点结果差不超过 1e-6。七、未连接的输入按端口默认值参与求值，不得静默按零处理。八、set_input 与 invalidate 使该节点及全部下游结果失效，下一次求值必须拿到新值。九、节点无变更时按版本号复用结果，evaluate 重复调用只增加缓存命中，不增加求值次数；同一节点被多个下游引用时只求值一次，次数计入求值计数而非折叠统计。十、fold_constants 折叠全常量输入的子图并返回折叠节点数，被折叠节点仍保留在图里，失效后可恢复原值。十一、evaluate_all 按节点 ID 升序返回全部可求值节点的结果；stats 固定返回 evaluations、cache_hits、folds、invalidations 四个整数，reset_stats 只清计数，不动图与缓存。十二、snapshot 覆盖 version、nodes、stats，必须深拷贝；restore 遇非法结构抛 MATERIAL_SNAPSHOT_INVALID 且原状态不变。

硬约束：evaluate、set_input、fold_constants 全程持锁，多路并发求值与改值只看到完整版本，不允许出现上游已更新、下游仍返回旧值的半新半旧图；快照恢复后图结构与缓存版本不得与快照共享可变结构，恢复后重新求值结果必须与恢复前一致。验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: material_graph_evaluator
- typeName: MaterialGraphEvaluator
