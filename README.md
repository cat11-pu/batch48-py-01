# batch48-py-game-b-02

## 题面

延迟渲染管线扩到多渲染目标之后，美术在编辑器里看到 G-Buffer 的通道颜色整体串位，同一张贴图既被当成法线又被当成粗糙度写进两个批次，开了混合的通道在部分显卡上直接变黑；显存池把两张生命周期完全不重叠的贴图算成两份开销，另一台机器又把一张后处理还要读取的附件提前复用了，读到的内容是别人的中间结果。显存告警也在涨：同样的通道配置在两台机器上算出的总字节数不一致，把声明顺序换一下就变一个计划，别名候选一会儿有一会儿没有。范围是 gsb/batch48-py-game-b-02 下的 mrt_attachment_planner.py，类 MrtAttachmentPlanner，负责附件声明、通道声明、计划输出、兼容性判定与别名候选。

公开接口：`MrtAttachmentPlanner(*, max_color_slots=8, byte_budget=67108864, max_attachments=16, clock=None)`；`declare_attachment(name, *, format, width, height, samples=1) -> dict`；`attachment_info(name) -> dict`；`drop_attachment(name) -> None`；`declare_pass(name, *, order, color=(), depth=None, blend=False, reads=(), samples=1) -> dict`；`pass_info(name) -> dict`；`plan() -> dict`；`compatibility(name_a, name_b) -> dict`；`alias_candidates() -> list`；`stats() -> dict`；`reset_stats() -> None`；`snapshot() -> dict`；`restore(state) -> None`。拒绝统一抛 MrtPlannerError(code, detail)，code 取下面列出的字符串。

故障与边界：一、format 只接受 r8、rg8、rgba8、rgb10a2、rgba16f、rgba32f、depth24、depth32f，宽高取 1 到 16384，采样数取 1 到 8，否则 MRT_INVALID_ATTACHMENT，附件信息要带 bytes_per_texel 与 bytes；二、同名附件同参数重复声明幂等，参数不同报 MRT_DUPLICATE_ATTACHMENT，附件数超过 max_attachments 报 MRT_ATTACHMENT_LIMIT；三、同一通道内同一附件重复出现报 MRT_INVALID_PASS，color 槽数超过 max_color_slots 报 MRT_SLOT_LIMIT 且 detail 带 color_slots；四、通道至少要绑定一个 color 或 depth，否则 MRT_INVALID_PASS，同名通道重复声明报 MRT_DUPLICATE_PASS；五、color 与 depth 绑定的采样数必须与通道一致，否则 MRT_SAMPLE_MISMATCH，depth 槽只接受 depth24 与 depth32f，其他格式报 MRT_FORMAT_UNSUPPORTED；六、需要混合的附件不能用只写着色的格式，冲突要在计划阶段暴露为 MRT_BLEND_UNSUPPORTED，不允许静默降级；七、plan 的通道按 order 升序、同序按名称排，附件按名称升序，total_bytes 只统计被 color 与 depth 绑定过的附件且每张只算一次，每个通道的 bytes 是它自己绑定的附件之和；八、总字节超过 byte_budget 报 MRT_BUDGET_EXCEEDED，detail 带 total_bytes 与 byte_budget，计划失败不计入 plans_computed；九、compatibility 对格式、尺寸、采样数分别给出 format_mismatch、size_mismatch、sample_mismatch，按字典序返回，没有差异时 compatible 为 True；十、alias_candidates 只给出格式尺寸采样一致、且前者最后一次使用早于后者第一次使用的一对，按 earlier 与 later 排序，被任何一次要读取的附件不能被当作已经释放；十一、同一通道里两个附件语义相同但格式不同时不得合并，必须保留各自绑定；十二、drop_attachment 在被任何通道引用时拒绝并报 MRT_IN_USE，重复删除报 MRT_UNKNOWN_ATTACHMENT；十三、snapshot 必须是深拷贝，restore 结构非法报 MRT_SNAPSHOT_INVALID 且原状态不变。

硬约束：declare_attachment、declare_pass、plan、snapshot 全程持锁，八路并发声明不同通道后 plan 必须看到全部通道且没有重名或丢失；附件数、每通道槽数与总字节预算都是硬顶，超限拒绝而不是覆盖旧声明；统计固定返回 attachments_declared、passes_declared、plans_computed、rejected 四个整数，reset_stats 归零；快照恢复后附件、通道、统计与别名候选都要在，且不与恢复前的对象共享结构。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-b-02
- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- difficulty: 困难
- module: mrt_attachment_planner
- typeName: MrtAttachmentPlanner
