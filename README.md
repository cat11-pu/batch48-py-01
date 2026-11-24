# batch48-py-game-b-19

## 题面

分屏与左右眼双视图上线后，每个视图各裁一遍把主线程打满，帧率从九十掉到三十五；两视图边界上的物体一帧有一帧没有，玩家看到树叶在闪；有人把左右眼参数调成完全一样，结果只剩一个视图的可见列表，另一个视图整屏空白。范围是 gsb/batch48-py-game-b-19 下的 multiview_cull_planner.py，类 MultiviewCullPlanner，负责一次求出多个视图的可见集合、逐物体视图位掩码、LOD 分级与同参数视图合并。

公开接口：`MultiviewCullPlanner(*, max_views=8, max_objects=200000)`；`add_view(view_id, camera) -> dict`；`remove_view(view_id) -> bool`；`set_object(obj_id, bounds, layers=1) -> dict`；`remove_object(obj_id) -> bool`；`views() -> list[dict]`；`objects() -> list[dict]`；`cull() -> dict`；`lod_for(obj_id, view_id) -> int`；`stable_views() -> list[list[str]]`；`view_key(view_id) -> str`；`stats() -> dict`；`snapshot() -> dict`；`restore(state) -> None`；`reset() -> None`。拒绝统一抛 CullError(code, detail)。

边界与故障：一、相机字典为透视 kind 等于 perspective，需要 position、target、up、fov_deg、aspect、near、far；正交 kind 等于 ortho，用 half_height 代替 fov_deg；两者都可带 layers，缺省 0xFFFF；kind 非法、缺字段、fov 不大于 0 或不小于 180、near 与 far 相等或出现非有限数都抛 CULL_CAMERA_INVALID。二、view_id 去空白后非空且唯一，重复抛 CULL_VIEW_DUPLICATE；视图数上限 max_views，超出抛 CULL_VIEW_LIMIT；view_key 只由相机参数决定，与 view_id 无关；add_view 返回 view_id、kind 与 key，views 返回已登记视图且每项结构与 add_view 返回一致。三、bounds 是 min 与 max 两个三元组，任一分量 min 大于 max 抛 CULL_BOUNDS_INVALID；layers 必须是非负整数；同一 obj_id 再次登记按覆盖处理，返回 obj_id、layers 与 replaced；对象数上限 max_objects，超出抛 CULL_OBJECT_LIMIT。四、视锥用六个平面求交，包围体整体落在任一平面外侧才剔除，正交与透视都要支持；near 大于 far 时按两者之间的实际深度范围裁剪，反向深度不得把相机背后的物体算成可见。五、包围体恰好贴在视锥平面上时算可见，不因浮点误差被丢弃。六、对象 layers 与相机 layers 相与为 0 时不可见，位掩码对应位不得置位。七、cull 返回 views、masks、visible；views 按 view_id 升序，每个视图的 objects 按 obj_id 升序；masks 覆盖每个对象，第 i 个视图按 view_id 升序对应 1 左移 i，不可见为 0；visible 是各视图可见条目数之和。八、参数完全相同的两个视图只计算一次，但可见列表必须按各自 view_id 独立返回，不能只保留一份。九、lod_for 以包围体对角线一半作半径：透视比值为半径除以中心到相机距离乘半视角正切，正交比值为半径除以 half_height；比值不小于 0.5、0.2、0.05 时依次取 0、1、2，否则取 3。十、stable_views 按相机参数的等价键分组，组内 view_id 升序、组间按首元素升序，分组只由相机参数决定。十一、remove_view 与 remove_object 对未知标识分别抛 CULL_VIEW_UNKNOWN 与 CULL_OBJECT_UNKNOWN，删除成功返回真。十二、同一场景连续两次 cull 结果必须完全一致；snapshot 覆盖视图、对象与计数，且是深拷贝，restore 遇非法结构抛 CULL_SNAPSHOT_INVALID 且原状态不变。

硬约束：并发的 set_object 与 cull 只能看到一致快照，返回的 masks 与各视图列表必须互相自洽，不允许列表里有物体而掩码为 0；快照恢复后视图、对象与掩码与中断前一致；视图数与物体数是硬上限，不允许无界增长。stats 固定返回 views、objects、culls、instances 四个整数，instances 是历次 cull 的可见条目累计。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与快照恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: multiview_cull_planner
- typeName: MultiviewCullPlanner
