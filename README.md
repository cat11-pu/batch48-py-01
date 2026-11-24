# GSB 任务说明

## 题面

赛事回放平台把一场对局压成统一的全量事件流，各客户端再自行裁剪成单个玩家的视角。上线后客服收到一批互相矛盾的截图：A 队选手的画面里出现了对手在迷雾中的走位，B 队选手却看不到自己队伍的关键技能；同一个人反复拖动进度条，同一次交火的技能顺序会前后颠倒；观战端导出的片段里混进了队伍语音的文字转写；还有玩家反馈实体在进入视野之前发生的事件也被算进了自己的视角。回放服务重启后，部分视角的可见事件数对不上，个别视角的裁剪结果和重启前完全不同。

范围是 gsb/batch48-py-game-a-07 的 replay_view_projector.py，类 ReplayViewProjector，负责视角注册、实体注册、视野采样、事件投影、区间投影与快照恢复。

公开接口：ReplayViewProjector(*, view_limit=8, entity_limit=64, event_limit=2000, clock=None)；register_view(view_id, *, kind="player", team=None, owner=None, version=1) -> dict；register_entity(entity_id, *, team, category="unit") -> dict；record_vision(view_id, *, at, visible) -> dict；project_event(view_id, event) -> dict；project_range(view_id, *, start=0, end=None, limit=None) -> dict；seek(view_id, at) -> dict；verify_view(view_id) -> dict；event_count(view_id) -> int；content_of(view_id, seq) -> dict；stats() -> dict；reset_stats() -> None；snapshot() -> dict；restore(state) -> None。拒绝统一抛 ReplayError(code, detail)。

故障与边界：一、view_id 相同但 kind、team、owner 或 version 任一不同要按冲突拒绝，完全一致则返回 duplicate，视角总数超过 view_limit 拒绝；二、实体以 entity_id 为键，同 id 不同队伍或类别按冲突拒绝，类别只允许 unit、projectile、structure；三、视野采样必须按 at 非递减上报，时间回退直接拒绝，同一 at 上提交不同可见集合也按冲突拒绝，采样里出现未注册实体拒绝且不落库；四、投影按事件时刻之前最近一次采样判定可见性，事件发生后才进入视野的实体，其更早的事件不得出现在该玩家视角；五、不可见事件只标记为 hidden 并保留在流水里，不删除也不清空序号；六、聊天按 scope 处理：all 全量可见，team 仅同队选手可见，private 仅收件人可见，被裁剪的内容必须清空正文；七、观战视角能看到双方单位，但队伍语音与私聊必须始终裁剪；八、同一视角同一 seq 重复投影返回 duplicate 且不重复入账，同 seq 内容不同按冲突拒绝；九、单视角事件条数有硬顶，超过后拒绝新事件而不是淘汰旧事件；十、区间投影按 seq 升序返回、按事件时刻的闭开区间过滤、limit 截断并标记 truncated，重复投影同一区间不得因缓存命中而改变事件顺序；十一、seek 记录游标并给出游标前事件数，verify_view 校验流水一致性与各类计数；十二、snapshot 深拷贝全部视角、采样、流水与统计，restore 后行为与统计必须一致，结构非法拒绝且不破坏原状态。

硬约束：视角注册、采样与投影全程持锁，多线程同时对同一 seq 投影只能入账一次；快照跨进程恢复后继续投影不重复也不丢事件；视角、实体、单视角事件与采样都有上限，不允许无限增长。

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
