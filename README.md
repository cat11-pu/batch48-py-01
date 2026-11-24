# batch48-py-game-b-04

## 题面

资源热更新之后，渲染线程报出的句柄错误开始变多：同一张纹理被两处代码同时引用，先释放的一处把资源提前拆了，另一处下一帧就读到已释放对象；换关卡时旧句柄偶尔还能通过校验，命中的却是同名资源新分配的实例。设备丢失之后情况更乱：一部分旧句柄的释放请求反复报错，重建队列里同名资源被塞进好几条，泄漏报表则把所有未销毁资源算成一个数，值班同学分不清哪些还在被引用、哪些只是等着回收。

范围是 gsb/batch48-py-game-b-04 下的 resource_handle_ledger.py，类 ResourceHandleLedger，负责 GPU 资源句柄分配、世代校验、引用计数、延迟销毁、设备丢失重建与泄漏审计。

公开接口：`ResourceHandleLedger(*, max_resources=64, max_open_frames=3, max_rebuild_queue=8, clock=None)`；`create(name, *, kind="buffer", size=0) -> dict`；`resource_info(handle) -> dict`；`retain(handle, *, count=1) -> dict`；`release(handle) -> dict`；`begin_frame() -> dict`；`end_frame() -> dict`；`reclaim() -> dict`；`validate(handle) -> dict`；`leak_report() -> dict`；`rebuild_queue() -> list`；`device_lost() -> dict`；另有 stats、reset_stats、snapshot、restore。拒绝统一抛 LedgerError(code, detail)。

故障与边界：一、name 必须是非空字符串且不超过 64 字符，kind 只接受 buffer、texture、sampler、pipeline，size 取 0 到 2^31-1，越界报 LEDGER_INVALID_RESOURCE；二、活跃资源同名报 LEDGER_DUPLICATE_NAME，同时存在的资源数超过 max_resources 报 LEDGER_RESOURCE_LIMIT 并在 detail 里带 live 与 max_resources；三、句柄由 id 与 generation 组成，槽位复用时 generation 递增，旧世代句柄访问必须报 HANDLE_STALE 而不是命中新资源；四、release 只递减引用计数，归零后进入 pending，begin_frame 与 end_frame 记录帧边界，reclaim 只能销毁释放帧已完成的资源；五、重复释放报 LEDGER_DOUBLE_RELEASE，已释放句柄再 retain 报 LEDGER_RELEASED_HANDLE；六、超过 max_open_frames 个在飞帧报 LEDGER_FRAME_LIMIT，没有在飞帧时 end_frame 报 LEDGER_NO_OPEN_FRAME；七、validate 对未知句柄返回 HANDLE_UNKNOWN，对已释放句柄返回 HANDLE_RELEASED，对设备丢失句柄返回 DEVICE_LOST，都以 valid 与 reason 两个字段表达而不是抛异常；八、device_lost 让全部在用句柄失效，只有仍被引用的资源进入 rebuild_queue，重建队列按名字去重且不超过 max_rebuild_queue；九、泄漏报告按名字排序列出仍在引用与等待销毁的资源，并给出各自计数与总数，三类数字口径不能混在一起；十、snapshot 是深拷贝，restore 结构非法报 LEDGER_SNAPSHOT_INVALID 且原状态不变；十一、同一资源被两帧同时引用时，只有最后一次 release 且两帧都结束之后才允许回收；十二、设备丢失后旧句柄的 release 必须幂等成功，不得把重建队列里的条目再加一遍。

硬约束：create、retain、release、begin_frame、reclaim、device_lost 全程持锁，四路并发对同一资源累计引用再释放时计数必须与调用次数一致，不能出现负引用或提前销毁；帧内释放的资源必须活到该帧结束，跨帧引用未清时 reclaim 不得回收；资源数、在飞帧数与重建队列长度都是硬顶，超限拒绝而不是覆盖；快照恢复后槽位世代、帧号、重建队列与统计都要在，且不得与恢复前的对象共享结构。

验收：`python3 -m pytest -q tests` 的 15 条必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-b-04
- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- difficulty: 困难
- module: resource_handle_ledger
- typeName: ResourceHandleLedger
