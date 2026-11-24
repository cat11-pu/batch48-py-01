# batch48-py-game-a-24

## 题面

大世界地图开放后，寻路服务在高峰时段开始拖慢整个战斗场景：同一起点的几十个请求被合并成一个，终点的差异被吞掉；网格热更新后仍然命中旧路径缓存，角色沿着已经不存在的通道跑；一次分帧只给少量节点预算，调用方却把半截路径当成到达并让角色停在半路；请求取消后票据仍占着队列，新请求被队列上限挡住；某块区域改成不可通行后，只有起点或终点正好落在该区域的缓存被清掉，路过该区域的旧路径继续生效；目标被围死时返回的状态和预算用完完全一样，前端无法区分；两个协程同时推进同一张票据，扩展计数互相覆盖。范围是 gsb/batch48-py-game-a-24 下的 path_request_broker.py，类 PathBroker，负责登记网格版本、接收寻路请求、按节点预算分帧推进、维护路径缓存与区域失效。

公开接口：PathBroker(*, max_open: int = 64, cache_limit: int = 128)；set_grid(version, *, width, height, blocked=()) -> dict；request(start, goal, grid_version, *, region=None) -> dict；step(ticket, budget_nodes, *, worker="main") -> dict；cancel(ticket) -> dict；result(ticket) -> dict；invalidate(region) -> int；pending() -> list；ticket_state(ticket) -> dict；cache_info() -> dict；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。所有拒绝抛 PathError(code, detail)。

set_grid 返回 version、width、height、blocked，blocked 为不可通行格数，request 返回 ticket、start、goal、grid_version、status、cache_hit，step 与 result 返回 status、path、expanded，ticket_state 返回 status 与 expanded，cancel 返回 ticket 与 status，cache_info 返回 entries。

故障与边界：一、缓存键必须同时含起点、终点与网格版本，三者任一不同都不得复用；二、同起点不同终点的请求各自持票，不得合并；三、邻接固定按上右下左展开，开放网格的最短路径必须稳定可复现；四、budget_nodes 为非负整数，负数抛 PB_INVALID_BUDGET；五、只有扩展到终点才置 done 并返回 path，队列耗尽仍未到终点置 unreachable；六、result 未完成抛 PB_NOT_READY，不可达抛 PB_UNREACHABLE，取消抛 PB_CANCELLED，未知票据抛 PB_UNKNOWN_TICKET；七、cancel 幂等，取消后票据不再出现在 pending，也不再占用队列名额；八、区域失效按路径实际经过的格子清理缓存并返回条数，无命中返回零，区域结构非法抛 PB_INVALID_REGION；九、set_grid 同版本不同内容抛 PB_GRID_CONFLICT，尺寸非正或阻挡点越界抛 PB_INVALID_GRID，坐标越界抛 PB_INVALID_CELL，未登记版本抛 PB_UNKNOWN_GRID；十、开放票据达到 max_open 时新请求抛 PB_QUEUE_FULL 且不分配票据；十一、快照保存票据进度、网格版本与缓存键，恢复后未完成票据从原扩展位置继续，结构非法抛 PB_SNAPSHOT_INVALID 且原状态不变。

硬约束：票据首次 step 绑定 worker，之后同一票据只允许该 worker 推进，其他 worker 抛 PB_BUSY，扩展计数不能被两次写入；预算耗尽必须返回可识别的未完成状态，调用方不能把它当成不可达；缓存条目数不得超过 cache_limit，超出按最久未使用淘汰；stats 固定返回 requests、steps、cache_hits、cache_misses、completed、cancelled 六个整数，reset_stats 归零。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；区域失效与并发推进自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
- module: path_request_broker
- typeName: PathBroker
