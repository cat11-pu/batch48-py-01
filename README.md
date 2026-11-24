# batch48-py-game-b-18

## 题面

显存池改成自管子分配后，4K 场景跑十分钟就碎片化到申请不出 2MB 缓冲；帧末回收把还在采样的区块也还了回去，下一帧出现整块花屏；有人把对齐调到 1KB，几个区块的起始地址落进了前一块的尾巴，采样直接越界；整理计划还想把跨帧存活的缓冲区一起搬走，句柄全乱了。范围是 gsb/batch48-py-game-b-18 下的 gpu_memory_suballocator.py，类 GpuMemorySuballocator，负责 arena 内首次适配子分配、帧末回收、碎片整理计划与配额校验。

公开接口：`GpuMemorySuballocator(*, max_arena_bytes=4294967296, max_align=65536, max_alloc_bytes=268435456, max_arenas=8)`；`create_arena(size, alignment=256) -> dict`；`arenas() -> list[dict]`；`alloc(size, tag, lifetime="frame", alignment=1) -> dict`；`free(handle) -> int`；`signal_fence(frame) -> None`；`end_frame(frame) -> dict`；`check(handle) -> dict`；`free_blocks(arena=None) -> list[dict]`；`defrag_plan(arena=None) -> list[dict]`；`apply_defrag(plan) -> int`；`stats(arena=None) -> dict`；`snapshot() -> dict`；`restore(state) -> None`；`reset() -> None`。拒绝统一抛 GpuMemError(code, detail)。

边界与故障：一、create_arena 的尺寸必须在 1 到 4GB 之间，对齐必须是 2 的幂且不超过 64KB，否则分别抛 GPU_ARENA_INVALID 与 GPU_ALIGN_INVALID；arena 标识按 arena-0、arena-1 递增，总数上限 8，超出抛 GPU_ARENA_LIMIT；create_arena 返回 arena、size 与 alignment，arenas 每项给出 arena、size、alignment、free_bytes、blocks。二、alloc 的 size 在 1 到 256MB 之间，超出抛 GPU_ALLOC_TOO_LARGE；alignment 为 2 的幂且不超过 64KB，否则抛 GPU_ALIGN_INVALID；lifetime 只接受 frame 与 persistent，否则抛 GPU_LIFETIME_INVALID；tag 去空白后不得为空，否则抛 GPU_TAG_INVALID；没有任何 arena 时抛 GPU_ARENA_INVALID；成功时返回 handle、arena、offset、size、tag 与 lifetime。三、分配按 arena 创建顺序首次适配，偏移对齐到请求的对齐值；所有空闲块都放不下时抛 GPU_ADDRESS_SPACE，且所有 arena 保持原状。四、对齐值大于某个空闲块起始余量时必须换到后面的空闲块，不得向前侵占相邻存活区块。五、free 返回释放的字节数，相邻空闲块立即合并；重复释放与未知句柄抛 GPU_HANDLE_INVALID，check 同样拒绝，存活时返回 handle、arena、offset、size、tag、lifetime 与 state，state 固定为 live；free_blocks 返回按偏移升序的空闲块，每块含 offset 与 size。六、end_frame 只回收 frame 生命周期的临时分配，persistent 永不自动回收；fence 未到达的临时分配必须保留到下一帧，不能按帧号硬回收；返回 frame、freed 与 retained，后两项是句柄列表。七、end_frame 的帧号必须严格大于当前帧，否则抛 GPU_FRAME_ORDER；signal_fence 只接受非负整数，否则抛 GPU_FENCE_INVALID。八、defrag_plan 只搬移可移动区块，按 from 偏移升序返回 handle、from、to、tag；跨帧存活的 persistent 区块即便能减少碎片也不得搬移，计划必须保持区块相对顺序。九、apply_defrag 搬移后句柄不变并返回搬移条数；计划含未知句柄、目标越界或重叠时抛 GPU_PLAN_INVALID，arena 原状不动。十、stats 返回 arenas、allocated_bytes、free_bytes、largest_free、blocks、fragmentation；碎片率为 1 减去最大连续块除以空闲字节，无空闲字节时为 0，保留 6 位小数；不传 arena 时对全部 arena 汇总。十一、快照恢复后分配表、空闲块、当前帧与已到达的 fence 集合必须与中断前一致。十二、并发的 alloc、free 与 defrag_plan 安全，不允许读到半个块表；单 arena 4GB、对齐 64KB、单次 256MB、arena 数 8 都是硬上限。

硬约束：分配与释放会在渲染线程与流式加载线程同时发生，块表与 fence 集合必须原子更新；整理只在 apply_defrag 里一次性生效，失败不得留下搬了一半的状态；快照与恢复之间不共享可变结构；空闲块合并只允许发生在相邻地址，绝不允许把存活区块挤出。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与快照恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: gpu_memory_suballocator
- typeName: GpuMemorySuballocator
