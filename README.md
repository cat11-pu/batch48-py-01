# batch48-py-game-c-06

## 题面

旁白一开口，背景音乐就整块没了，嘉宾停了两秒音乐才一点点爬回来；两个嘉宾同时说话时音乐被压了两遍，回放里能听出增益一路抖。这套逻辑最早按固定阈值硬切，后来补了计时却没分清触发源，优先级、保持时间与增益回写各自为政，改一处就带出另一处。范围是 `gsb/batch48-py-game-c-06` 下的 `ducking_state_guard.py`，类 `DuckingStateGuard`，负责侧链触发的闪避状态机、多源优先级、量化增益与进程内快照。

公开接口：`DuckingStateGuard(*, sample_rate=48000, attack_ms=20, hold_ms=300, release_ms=800, depth=0.35, quantum=0.01, clock=None)`；`frames_for(ms) -> int`；`add_source(source_id, *, priority=0, depth=None) -> dict`；`remove_source(source_id) -> None`；`feed(source_id, active) -> dict`；`advance(frames) -> float`；`gain() -> float`；`state() -> str`；`dominant_source() -> str | None`；`set_bypass(flag) -> None`；`bypassed() -> bool`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 `DuckingError(code, detail)`，code 取 DUCK_INVALID_TIMING、DUCK_UNKNOWN_SOURCE、DUCK_DUPLICATE_SOURCE、DUCK_INVALID_DEPTH、DUCK_INVALID_PRIORITY、DUCK_SNAPSHOT_INVALID。

故障与边界：一、毫秒换算采样数按 `(ms * sample_rate + 500) // 1000` 四舍五入，负数与非整数毫秒报 DUCK_INVALID_TIMING，零合法。二、源 ID 重复报 DUCK_DUPLICATE_SOURCE；每个源可单独给深度，深度必须严格落在 0 与 1 之间，否则 DUCK_INVALID_DEPTH；优先级为负报 DUCK_INVALID_PRIORITY；未登记源上的任何操作报 DUCK_UNKNOWN_SOURCE；add_source 返回 source_id、priority、depth、active 四个字段。三、多触发源取最深闪避，不把两个源的衰减相乘或相加。四、增益以 0.01 为量化步长，任何时刻最多两位小数，回写不得来回跳变。五、attack 从进入时的增益线性走到 1 减 depth，步数等于 frames_for(attack_ms)，走满后进入 hold 并把增益钉在目标值。六、最后一个源释放后先保持 hold_ms 再按 release_ms 线性回升到 1.0，hold 期间增益不变，release 结束后状态回到 idle。七、重复 feed 同一个已激活源不得重启 attack，阶段进度照旧推进。八、状态只在 idle、attack、hold、release、bypass 之间迁移，无激活源时 dominant_source 返回 None，有多个源时给出当前压得最深的那一个。九、set_bypass(True) 立即返回 1.0 并把状态置为 bypass，状态机冻结不再推进；关闭 bypass 后从冻结前的进度接着走，不得从头再来。十、高优先级源释放后，仍在 hold 的低优先级源继续生效，增益不得提前回升。十一、snapshot 深拷贝增益、阶段进度、源表与 bypass 标志，restore 校验 version、state 与增益范围，非法输入报 DUCK_SNAPSHOT_INVALID 且原状态一律不变。十二、feed 返回 source_id、active 与迁移后的 state 三个字段，供调音台面板直接显示；advance 返回本次推进结束时的增益，值必须与随后 gain() 读到的一致。

硬约束：feed 与 advance 会被多路音频回调并发调用，内部必须用同一把锁串行，四路各二十五次触发与推进后统计恰好相等；任何时刻读到的增益要么是旧值要么是新值，不得半更新，bypass 开关、源表增删与推进共用这把锁，快照也必须在锁内取。hold 与 release 按采样数精确推进，不允许按墙钟时间估算。stats 固定返回 sources_added、sources_removed、feeds、advances、bypass_toggles、rejected 六个整数，reset_stats 只清计数、不动状态机与增益。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与快照恢复自行验证；只做本地实现，不引入第三方音频库，也不依赖真实声卡与系统时钟。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-c-06
- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 困难
- module: ducking_state_guard
- typeName: DuckingStateGuard
