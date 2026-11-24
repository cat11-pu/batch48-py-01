# GSB 任务说明

## 题面

帧同步竞技的排位服近期频繁出现不同步：对局进行到中段，两端画面上同一个英雄的位置开始偏离，技能命中判定一边成立一边不成立，赛后复盘却找不出是哪一帧先出问题；有的对局两端都没有上报某一帧，复盘工具直接判定两端一致，把真正的分歧掩盖过去；还有对局在第一个分歧帧之后又恢复一致，运营看到的报告只剩最后一帧，最早的分歧点反而丢了；客服按帧区间做二分定位时，偶尔会拿不同端上报的摘要比较，定位结果指向了根本不存在的输入。证据包越攒越大，磁盘被写满，旧对局的原始输入也没法按对局清理。

范围是 gsb/batch48-py-game-a-08 的 desync_localizer.py，类 DesyncLocalizer，负责收集各端逐帧摘要、比对分歧、二分定位触发帧、隔离异常端和管理证据包。

公开接口：DesyncLocalizer(*, peer_limit=16, frame_limit=1000, evidence_limit=256, clock=None)；register_peer(peer_id, *, endpoint=None) -> dict；quarantine_peer(peer_id, *, reason="") -> dict；peer_state(peer_id) -> str；report_frame(peer_id, *, match_id, frame, digest, inputs=None) -> dict；compare(*, frame, left, right, match_id="default") -> dict；first_divergence(*, left, right, start=0, end=None, match_id="default") -> dict；bisect_range(*, left, right, start, end, match_id="default") -> dict；evidence(*, match_id=None, limit=None) -> dict；clear_match(match_id) -> int；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 DesyncError(code, detail)。

故障与边界：一、peer 重复注册且 endpoint 相同返回 duplicate，不同按冲突拒绝，注册数超过 peer_limit 拒绝，空 peer 名拒绝；二、帧摘要按实体键排序后规范化为 k=v|k=v，字典顺序不同视为同一摘要；摘要为空、实体名为空、值不是标量一律按格式不符拒绝；三、缺帧与格式不符必须区分原因：格式不符当场拒绝并计入 rejected，缺帧只是查不到该帧，比对时归为数据不足；四、同一 peer 同一对局同一帧重复上报同摘要返回 duplicate，摘要不同按冲突拒绝并保留首次摘要；五、两端都缺同一帧时不能当成一致，必须标记数据不足；六、比对按实体求差集，返回分歧实体列表与两端取值，只有一侧存在该实体时另一侧记为 None；七、首个分歧帧按帧号升序在整个区间内查找，分歧实体在后续帧恢复一致时首个分歧点仍然保留，不能被覆盖；八、二分定位只在给定两端之间进行，区间端点必须来自同一对端，不能混用不同端的摘要，探测次数要有上界而不是退化成线性扫描；九、起始帧已分歧时一次探测直接返回，区间内全一致返回 none；十、隔离某端后该端不得再上报也不得参与比对，重复隔离返回已隔离状态；十一、证据包保留原始输入与规范化摘要，按 evidence_limit 只保留最新条目并累计丢弃数，可按对局清理并返回移除条数；十二、单端单局帧数超过 frame_limit 拒绝新帧；snapshot 深拷贝对局、证据与统计，restore 后比对结果与统计一致，结构非法拒绝且不破坏原状态。

硬约束：上报、比对与隔离全程持锁，多线程同时上报同一 peer 同一帧只能入账一次并保留一份证据；快照跨进程恢复后继续比对不丢对局；peer 数、单端单局帧数与证据条数都有硬顶，超出必须拒绝或按策略丢弃并留痕。

验收：`python3 -m pytest -q tests` 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；并发与重启场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 地狱
