# batch48-py-game-b-14

## 题面

开放世界跑图时纹理上传把带宽打满，远景刚升到高 mip 又被下一帧立刻淘汰，画面来回震荡；同一块地表在一帧里被多次请求，上传队列里排了好几条重复项；预算收紧后一部分请求直接消失，远处地形永远停在低清；上传和淘汰落在同一帧的日志里，维护同学分不清哪条是补传、哪条是新请求。范围是 gsb/batch48-py-game-b-14 下的 texture_stream_budget.py，类 TextureStreamBudget，负责按帧预算调度 mip 常驻、合并请求、标记固定纹理与滞后淘汰。

公开接口：`TextureStreamBudget(*, max_textures=50000, max_mip=15, max_frame_bytes=67108864)`；`register(texture_id, base_bytes) -> dict`；`request(texture_id, mip, screen_error) -> dict`；`plan(budget_bytes=None) -> dict`；`resident(texture_id) -> int`；`pin(texture_id) -> bool`；`unpin(texture_id) -> bool`；`pinned() -> list`；`stats() -> dict`；`reset_stats() -> None`；`snapshot() -> dict`；`restore(state) -> None`。拒绝统一抛 TextureStreamError(code, detail)。

故障与边界：一、纹理必须先登记，base_bytes 为正整数，重复登记同一 base 幂等，改 base 抛 TEXTURE_BASE_CONFLICT，非法 base 抛 TEXTURE_BASE_INVALID。二、request 的 mip 必须在零到 max_mip 减一之间，screen_error 为非负有限数，未登记纹理抛 TEXTURE_UNKNOWN_TEXTURE，越界分别抛 TEXTURE_MIP_RANGE 与 TEXTURE_ERROR_INVALID。三、上传优先级按屏幕误差降序、纹理 ID 升序；每级字节数为 base_bytes 右移 mip 位且至少为一字节。四、上传按预算累计，放不下的请求留在待办里等下一帧，不得丢弃，也不得按先进先出重排；被挤到下一帧的请求保留原优先级。五、同一纹理重复请求只保留一条，按更细的 mip 与更高的屏幕误差合并。六、目标 mip 已驻留时计入命中，不产生上传。七、淘汰每次只降一级 mip，被 pin 的、本帧刚上传的、本帧有待处理请求的纹理都不参与；候选按屏幕误差升序、纹理 ID 升序；候选不足时允许暂时超出常驻上限，下一帧继续消化；pin 解除后同一帧内不得立刻被选中，要到下一帧才允许。八、plan 同时把 budget_bytes 当作上传额度与常驻总额上限，返回 uploads、evictions、deferred、used_bytes、resident_bytes；uploads 每项含 texture_id、from_mip、to_mip、bytes，从零级开始驻留时 from_mip 记负一，未指定预算时使用 max_frame_bytes。九、单帧预算不得超过 max_frame_bytes，非正数或非整数预算抛 TEXTURE_BUDGET_INVALID，超上限抛 TEXTURE_BUDGET_EXCEEDED，报错不得改动驻留表。十、纹理数上限 max_textures、单纹理 mip 上限 max_mip，超限分别抛 TEXTURE_TEXTURE_LIMIT 与 TEXTURE_MIP_RANGE。十一、stats 固定返回 requests、merged、hits、uploads、evictions、deferred 六个整数，reset_stats 只清计数不动驻留。十二、snapshot 覆盖 version、textures、resident、pins、pending、stats，必须深拷贝；restore 遇非法结构抛 TEXTURE_SNAPSHOT_INVALID 且原状态不变。

硬约束：request 与 plan 全程持锁，多路并发请求与出帧时每条上传或淘汰记录必须自洽，字节数与目标 mip 对应，不得出现同一条纹理在同一帧既上传又淘汰；快照恢复后驻留 mip 与 pin 集合必须与恢复前一致，且不得与快照共享可变结构。验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: texture_stream_budget
- typeName: TextureStreamBudget
