# batch48-py-game-b-24

## 题面

技能特效改成曲线驱动后，同一次施法在两次录制里粒子数量差了几十个；阶跃曲线在关键帧边界上会先跳回上一档再跳上去，爆炸环的节奏跟着抖；30Hz 与 60Hz 两档录制出的总发射数对不上，随机种子相同却出现不同的散布；余量累计到后来还会变成负数，一帧放出成百上千个粒子；策划改完插值模式后，低帧率设备上的过渡又被拉到了关键帧区间之外。范围是 gsb/batch48-py-game-b-24 下的 particle_curve_evaluator.py，类 ParticleCurveEvaluator，负责模块曲线登记、求值、发射预算累计、确定性随机流、时间重映射与快照。

公开接口：ParticleCurveEvaluator()；set_curve(module_id, keys) -> None；remove_curve(module_id) -> bool；curve_info(module_id) -> dict；list_modules() -> list[str]；eval(module_id, t) -> float；emission_budget(emitter, rate, dt) -> int；budget_state(emitter) -> dict；reset_budget(emitter) -> None；random_stream(seed, index) -> float；remap_time(t, start, end, mode) -> float；snapshot() -> dict；restore(state) -> None。所有拒绝抛 ParticleError(code)，code 取 PARTICLE_UNKNOWN_MODULE、PARTICLE_KEY_INVALID、PARTICLE_KEY_LIMIT、PARTICLE_MODULE_LIMIT、PARTICLE_RATE_INVALID、PARTICLE_REMAP_MODE、PARTICLE_TIME_INVALID、PARTICLE_SNAPSHOT_INVALID。

故障与边界：一、关键帧支持 linear、step、cubic 三种插值，模式写在段左端的关键帧上，段内取值不得越过两端值，curve_info 返回 module_id、key_count、start 与 end，list_modules 按名字升序；二、时间超出关键帧范围按首尾值钳制，不得外推；三、阶跃插值在关键帧时刻取右侧值，不得取左侧；四、三次插值只有两个关键帧时退化为线性，关键帧不足四点时不得外推；五、关键帧按时间严格递增，时间或值非数值、模式非法都整份拒绝；六、关键帧上限 256、模块上限 64，超限报错且不改动现有曲线，未登记模块的求值与查询报 PARTICLE_UNKNOWN_MODULE，remove_curve 对不存在模块返回假；七、发射预算按每秒速率与毫秒帧长累计，小数余量跨帧保留，本次调用返回整数发射数，budget_state 返回 remainder、emitted 与 dropped，未出现过的发射器返回全零，reset_budget 清零余量与计数，余量只在同一发射器内累计，跨发射器互不影响；八、30Hz 与 60Hz 下同一时长发出总数差不超过 1；九、余量累计上限 100，达到上限后丢弃超出部分并计入丢弃计数，不得溢出成负数；十、随机流按种子与索引可复现，取值落在 0 到 1 之间，不读写进程全局随机状态；十一、remap_time 支持 clamp 与 loop，loop 模式按区间长度取模后落回区间内，start 必须小于 end，模式非法整份拒绝；十二、快照含 version 与 curves，必须深拷贝，恢复后曲线与各发射器预算逐项一致，关键帧顺序与插值模式保持原样，结构非法整份拒绝并保持原状态。

硬约束：多路特效线程同时求值与整份替换曲线时，求值只能看到替换前或替换后的完整曲线，不得读到半份关键帧；进程重启后凭快照恢复，曲线、余量与丢弃计数保持一致；关键帧数、模块数与余量累计都有硬顶，失败调用不产生新条目也不改动旧曲线。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；不同帧率与并发求值场景自行验证；只做本地实现，并附一段改动说明，写清插值模式与预算累计的口径。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- difficulty: 困难
- module: particle_curve_evaluator
- typeName: ParticleCurveEvaluator
