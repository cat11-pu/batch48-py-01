# batch48-py-game-b-10

## 题面

相机链路升级后，人像模式和夜景合成在部分机型上出现偏色，换到另一台设备又正常；关掉某个美颜节点后下游磨皮和锐化还在跑，画面甚至比全开更慢；同一帧里连续两次切换节点开关，链路里多插了两级格式转换，纹理来回拷贝；显存日志显示每加一个节点就多一张全尺寸纹理，乒乓缓冲完全没复用起来。范围是 gsb/batch48-py-game-b-10 下的 postprocess_chain_guard.py，类 PostProcessChainGuard，负责后处理链编排、格式与颜色空间协商、开关失效传播、缓冲归属与耗时预算。

公开接口：`PostProcessChainGuard(*, frame_budget_ms=8.0)`；`set_source(name, *, fmt="rgba8", color_space="srgb") -> dict`；`add_pass(name, *, deps=(), reads=(), writes=(), fmt="rgba8", color_space="srgb", cost_ms=1.0, optional=False) -> dict`；`set_enabled(name, enabled) -> bool`；`build() -> dict`；`last_plan() -> dict`；`run() -> dict`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。冲突一律抛 ChainError(code, detail)。

故障与边界：一、add_pass 要求 name 非空且唯一，重复抛 CHAIN_DUPLICATE_PASS，空名或 writes 不是恰好一张抛 CHAIN_PASS_INVALID；reads 与 writes 出现同名纹理抛 CHAIN_READ_WRITE_HAZARD。二、fmt 只接受 rgba8、rgba16f、rgba32f，color_space 只接受 srgb、linear，其他取值抛 CHAIN_FORMAT_UNSUPPORTED；set_source 注册外部纹理，重名抛 CHAIN_DUPLICATE_TEXTURE。三、build 解析依赖：deps 指向不存在的 pass 抛 CHAIN_UNKNOWN_DEP；reads 既不是 source 也没有唯一写者抛 CHAIN_UNKNOWN_TEXTURE；两个 pass 写同一张纹理抛 CHAIN_TEXTURE_CONFLICT；读取某 pass 的产物却没有把该 pass 写进 deps 抛 CHAIN_DEPENDENCY_MISSING。四、依赖声明成环必须报 CHAIN_CYCLE，不能按注册顺序硬排；无环时按依赖拓扑排序，同层保持注册顺序。五、相邻 pass 的格式或颜色空间不一致时在消费者前插入转换步骤，只支持 rgba8 与 rgba16f、rgba16f 与 rgba32f、srgb 与 linear 的直接互转；跨两级格式视为不可转换并抛 CHAIN_CONVERSION_UNSUPPORTED；格式与颜色空间都不同时插两级，先格式后颜色空间。六、转换产物命名为源纹理加波浪号加目标格式竖线目标颜色空间，步骤名为 convert 加冒号加产物名；转换步骤参与缓冲交替，消费者必须改读产物。七、计划中每个写出步骤的缓冲号按执行位置 0 与 1 交替，source 纹理缓冲号记 -1；转换插入后缓冲归属要跟着改，不能让下游继续读旧缓冲。八、set_enabled 遇到未知 pass 抛 CHAIN_UNKNOWN_PASS；禁用某个 pass 时它的下游传递性跳过，上游结果保留复用，缓存按输入本轮是否重新执行判定。九、同帧内两次启停同一 pass 必须收敛到最后状态，不能重复插入转换步骤。十、optional 的 pass 输出不得被其他 pass 读取，否则抛 CHAIN_TRIM_CONFLICT；可选 pass 按执行顺序在 frame_budget_ms 内贪心纳入，放不下就跳过继续尝试后面的，未纳入的记入 trimmed，强制 pass 已超预算时 over_budget 为 True。十一、build 返回 steps、buffers、cost_ms、conversions、trimmed、disabled、over_budget；run 返回 executed、cached、skipped、trimmed、cost_ms、conversions，last_plan 返回最近一次计划。十二、snapshot 覆盖 passes、sources、开关、缓存标记、缓冲归属与 stats，必须是深拷贝；restore 遇非法结构抛 CHAIN_SNAPSHOT_INVALID 且原状态不变。十三、stats 固定六个整数：runs、executed、cached、skipped、trimmed、conversions，reset_stats 只清计数。

硬约束：每个 pass 只写一张纹理，同一 pass 不得读到自己本帧写入的纹理；耗时预算与转换步数是硬上限，不允许无界插入；快照跨进程恢复后不得与恢复前的 pass 表或缓冲表共享可变结构。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；开关与预算场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: postprocess_chain_guard
- typeName: PostProcessChainGuard
