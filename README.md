# batch48-py-game-b-08

## 题面

主城场景接入遮挡剔除后本该省下三成绘制，实际却出现物体在拐角处突然消失、过一帧又冒出来；远处的塔楼明明只占几个像素却一直霸占遮挡队列，真正贴近镜头的大墙体反而被剔除；美术改完建筑尺寸后代理没有跟着更新，旧的包围盒继续参与测试；关卡里物件一多，代理三角形总量直接翻倍，帧时间抖动明显。范围是 gsb/batch48-py-game-b-08 下的 occluder_proxy_guard.py，类 OccluderProxyGuard，负责代理生成、像素覆盖率打分、过期重建与预算裁剪。

公开接口：`OccluderProxyGuard(*, max_proxies=16, tri_budget=400, min_coverage=0.002)`；`build(mesh) -> dict`；`proxy_of(mesh_id) -> dict`；`invalidate(mesh_id, *, bounds=None) -> None`；`rebuild(mesh_id) -> dict`；`rebuild_pending() -> int`；`score(mesh_id, camera) -> float`；`coverage(mesh_id) -> float`；`occluders(camera, limit=8) -> list[str]`；`last_frame() -> dict`；`begin_frame() -> None`；`budget() -> dict`；`remove(mesh_id) -> None`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。冲突一律抛 OccluderError(code, detail)。

故障与边界：一、mesh 为字典，含 id、shape、bounds 六元组；shape 为 box 时代理三角形固定 12，为 hull 时必须给出 4 到 512 的 hull_triangles；字段缺失、bounds 上下界颠倒、shape 未知都抛 PROXY_MESH_INVALID。二、重复 id 抛 PROXY_DUPLICATE_MESH；代理数量达到 max_proxies 再建抛 PROXY_LIMIT；单个代理三角形数超过 tri_budget 抛 PROXY_BUDGET，两种情况都不落账。三、camera 含 position 三元组、正数 focal 与正数 viewport 两元组，结构不对抛 PROXY_CAMERA_INVALID；limit 小于 1 抛 PROXY_LIMIT_INVALID。四、覆盖率按投影像素面积算：取 bounds 最大边长的一半，距离取相机到包围盒中心的欧氏距离，像素半径等于半边长乘 focal 除距离，像素面积开方后除以视口面积，超过 1 按 1 封顶。五、score 为覆盖率乘 64 除以 64 加距离；occluders 按 score 降序、同分按 mesh_id 升序，只返回前 limit 个。六、覆盖率低于 min_coverage 的代理丢弃并计入 dropped_coverage；候选三角形累计超过 tri_budget 时从覆盖率最低的开始丢弃并计入 dropped_budget，返回集合永远不超预算。七、网格缩得很小但贴近相机时必须按当前像素面积重算覆盖率，不能沿用旧尺寸把代理错误淘汰。八、代理已过期而新代理还没完成时必须继续用旧代理，不能让物体临时变成非遮挡体。九、同一代理同帧被多个视锥请求时只重建一次，generation 只加一。十、invalidate 只把代理标脏并可顺带替换 bounds；rebuild 只对脏代理生效并让 generation 加一，重建后覆盖率缓存清零，非脏代理重复重建不加代也不计数；rebuild_pending 返回本轮重建数量。十一、begin_frame 增加帧计数，occluders 记录本帧 kept、dropped_budget、dropped_coverage 到 last_frame；remove 释放代理与三角形预算。十二、snapshot 覆盖 proxies、stats 与版本，深拷贝；restore 遇非法结构抛 PROXY_SNAPSHOT_INVALID 且原状态不变，合法时覆盖率缓存一并恢复。十三、stats 固定七个整数：built、rebuilt、removed、occluder_queries、dropped_budget、dropped_coverage、frames，reset_stats 只清计数。

硬约束：代理数量与三角形预算都是硬上限，任何一次查询都不得越过；快照跨进程恢复后不得与恢复前的代理共享可变结构，恢复后重建与查询必须继续可用；同一帧内重复查询同一批代理不得重复重建或重复计费。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；重建与预算场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: occluder_proxy_guard
- typeName: OccluderProxyGuard
