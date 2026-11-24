# batch48-py-game-b-11

## 题面

夜战关卡上线后，制作人反馈帧率从九十掉到四十：光源只挪了半米，整张集群光照网格却全量重算；来回切镜头时同一批光源算出的页码每次都不一样，缓存完全命中不了；把光源拖到网格边界外，调试面板里还会冒出负页码和空页；美术同学把上百个点光塞进同一个格子后，页里只剩最后写入的几条，前面的光源凭空消失，夜里整片街区忽明忽暗。问题落在 gsb/batch48-py-game-b-11 下的 cluster_light_page_cache.py，类 ClusterLightPageCache，负责集群光照分页、脏区重建、页码推导与快照恢复。

公开接口：`ClusterLightPageCache(*, max_pages=65536, page_capacity=64)`；`set_grid(x, y, z) -> dict`；`set_page_size(size) -> int`；`upsert_light(light) -> int`；`remove_light(index) -> int`；`page(ix, iy, iz) -> list`；`page_code(ix, iy, iz) -> str`；`rebuild(dirty_only=True) -> int`；`stats() -> dict`；`reset_stats() -> None`；`snapshot() -> dict`；`restore(state) -> None`。拒绝统一抛 ClusterLightError(code, detail)。

故障与边界：一、网格三维与页尺寸必须为正整数，非法抛 CLUSTER_GRID_INVALID 或 CLUSTER_PAGE_SIZE_INVALID，原网格保持不变。二、页表规模按网格除以页尺寸向上取整的乘积计算，超过 max_pages 抛 CLUSTER_PAGE_LIMIT，不得覆盖已有页；更换网格或页尺寸时旧页整体作废，页码相同也不得复用旧内容。三、光源含 index、position、radius、intensity；index 非负且唯一，字段缺失、半径或强度为负抛 CLUSTER_LIGHT_INVALID；同 index 再次 upsert 视为更新。四、包围球与页轴对齐盒相交的每一页都要收录该 index 且只出现一次，完全在网格外的光源不进入任何页；upsert_light 返回本次命中的页数；包围球退化成点时仍按点所在页收录。五、页内按强度降序、index 升序排列。六、单页容量为 page_capacity，超出部分只累加溢出页计数，不覆盖已收录索引。七、rebuild 只重算脏页并返回重建页数，dirty_only 为假时重算全部页；同一页两次重建之间被多次标脏也只重算一次；page 返回最近一次重建后的已提交内容，脏页未重算前不得混入新光源。八、页码由网格、页尺寸与页坐标推导，形如 g<gx>x<gy>x<gz>-s<size>-p<ix>.<iy>.<iz>，同样输入两次调用必须相同。九、页坐标越界抛 CLUSTER_PAGE_RANGE，禁止返回空列表。十、remove_light 返回该光源上次重建时所在页数并标脏这些页，未知 index 抛 CLUSTER_LIGHT_UNKNOWN，重复删除按零页处理且不报错。十一、stats 固定返回 lights、dirty_pages、hot_pages、rebuilds、overflow_pages 五个整数，重建计数按实际重建调用累计，不得按标脏次数累加，reset_stats 只清 rebuilds 与 overflow_pages，不动页与光源。十二、snapshot 覆盖 version、grid、page_size、lights、pages、stats，必须是深拷贝；restore 遇非法结构抛 CLUSTER_SNAPSHOT_INVALID 且原状态不变。

硬约束：upsert_light、remove_light、rebuild 全程持锁，八路并发写入与 page 读取时只能看到完整索引列表，不得读到半更新的页；快照恢复后光源表与页表不得与快照共享可变结构，恢复后全量重建的页内容必须与恢复前逐项一致。验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: cluster_light_page_cache
- typeName: ClusterLightPageCache
