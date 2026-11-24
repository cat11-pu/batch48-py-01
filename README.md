# batch48-py-game-b-01

## 题面

美术管线把顶点属性换成紧凑整数格式之后，玩家看到的模型在远处开始抖动，法线偏掉的墙面反射出一片噪点，同一份网格在两台机器上导出的量化结果逐字节不同，解码时还混进了上一版布局产出的数据。性能组另外反馈：一个角色把位置精度压到八位后管线没有任何提示就放过了，包围盒只有几厘米的模型看着还行，包围盒上百米的场景整片塌陷，UV 有接缝的布料在贴图边界上留下一条亮线。范围是 gsb/batch48-py-game-b-01 下的 vertex_attribute_quantizer.py，类 VertexAttributeQuantizer，负责布局声明、网格量化、反量化、误差预算与布局版本管理。

公开接口：`VertexAttributeQuantizer(*, max_vertices=4096, max_bits_per_vertex=128, max_semantics=8, clock=None)`；`declare_layout(layout_id, spec, *, version=1) -> dict`；`layout_info(layout_id, *, version=None) -> dict`；`drop_layout(layout_id) -> None`；`error_bound(layout_id, semantic, *, extent=1.0, version=None) -> float`；`quantize_mesh(mesh, layout_id, *, wrap_uv=False) -> dict`；`dequantize_mesh(encoded) -> dict`；`payload_bytes(encoded) -> int`；`stats() -> dict`；`reset_stats() -> None`；`snapshot() -> dict`；`restore(state) -> None`。拒绝统一抛 QuantizerError(code, detail)，code 取下面列出的字符串。

故障与边界：一、spec 里 kind 只接受 position、normal、tangent、uv、color，位宽取 4 到 24 且为 4 的倍数，否则 QUANT_INVALID_LAYOUT；二、语义条数超过 max_semantics 或每顶点总位宽超过 max_bits_per_vertex 报 QUANT_LAYOUT_BUDGET，detail 带实际数值；三、同一 layout_id 同版本同 spec 重复声明幂等且不增加版本，同版本不同 spec 或回退版本报 QUANT_LAYOUT_CONFLICT，更高版本成为当前版本，旧版本仍要能解码已产出的编码；四、error_bound 对 position 与 uv 按传入 extent 折算，对 normal 与 tangent 固定在单位向量区间，公式为跨度除以 2 的位宽次方减一；五、量化按每分量最小最大值归一映射到 0 到 2 的位宽次方减一，分量无跨度时统一落 0；六、mesh 缺语义、行长度不齐、分量数与 kind 不符、出现未声明语义、索引越界或为负，一律 QUANT_INVALID_MESH；七、顶点数超过 max_vertices 报 QUANT_MESH_TOO_LARGE，detail 带 vertex_count 与 max_vertices；八、payload_bytes 按每顶点总位宽向上取整到字节再乘顶点数；九、布局版本参与量化缓存键，缓存命中返回与首次等价但互不共享的副本，调用方改动返回值不得污染缓存；十、解码必须使用编码里记录的布局版本，而不是当前最新版本；十一、法线量化后必须重新归一化，解码端不得假设长度仍为 1；十二、UV 跨越 0 和 1 边界的三角形要按环绕方式处理，不能直接线性量化后产生接缝；十三、位置误差预算按包围盒尺度归一，绝对坐标很大的网格不能整体超差；十四、snapshot 必须是深拷贝，restore 结构非法报 QUANT_SNAPSHOT_INVALID 且原状态不变。

硬约束：declare_layout、quantize_mesh、dequantize_mesh、snapshot 全程持锁，八路并发对同一批网格量化时 meshes_quantized 必须与调用次数完全相等，同一网格的 fingerprint 在所有线程里一致；缓存按条数封顶，超限淘汰最旧条目，禁止无界增长；统计固定返回 layouts_declared、meshes_quantized、meshes_dequantized、cache_hits、rejected 五个整数，reset_stats 归零。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-b-01
- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- difficulty: 困难
- module: vertex_attribute_quantizer
- typeName: VertexAttributeQuantizer
