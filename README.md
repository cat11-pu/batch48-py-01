# batch48-py-game-a-04

## 题面

跨服匹配上线后，队列分片的运维值班表排满了：高峰扩容把同一个评分段的票据拆到两片，结果两片各自都凑不满一场，玩家等二十分钟也匹配不上；低峰缩容把两片合回去，等待时间被按合并时刻重算，先来的人反而排到后面；有票据在迁移途中从两个分片都能查到，匹配器拿同一张票开了两场；缩容时没等在途匹配结束，正在配的队列被拆散；迁移失败后票据在两个分片各留了一份索引，客服只能手工对账。还有分片号复用之后，两天的票据挂在同一片下，复盘时根本分不清批次。

范围是 gsb/batch48-py-game-a-04 的 queue_shard_rebalancer.py，类 QueueShardRebalancer，负责分片扩容缩容、票据迁移与归属索引。

公开接口：QueueShardRebalancer(*, max_shards=6, shard_capacity=4, clock=None)；add_ticket(player: str, *, score: int, at: int = 0) -> dict；remove_ticket(player: str) -> dict；shard_of(player: str) -> str；ticket_info(player: str) -> dict；locate(player: str) -> dict；shard_state(shard_id: str) -> dict；split_shard(shard_id: str) -> dict；merge_shards(left_id: str, right_id: str) -> dict；migrate_ticket(player: str, to_shard: str) -> dict；另有 begin_match、end_match、rebalance、stats、reset_stats、snapshot、restore。拒绝统一抛 ShardError(code, detail)。

故障与边界：一、新票据进最空且未满的分片，所有分片都满且分片数到顶报 SHARD_QUEUE_FULL；二、同一玩家只能有一张票据，重复入队报 SHARD_DUPLICATE_TICKET，分数与时间必须是非负整数；三、单分片票据数是硬顶，扩容不能超过 max_shards，缩容前合并后的数量也不得超过单分片容量，否则报 SHARD_FULL；四、split_shard 按评分中位切开，两侧各自按入队序号排序，返回值给出 left、right 与迁移条数；五、拆分边界上的同分票据要按稳定次序落到同一侧，不能把一组同分票拆散；六、merge_shards 把两片票据按序号合并，吸收方消失，两边票据的等待时间保留原值；七、迁移期间票据只属于一个分片，locate 的 copies 必须恒为 1，匹配器读取不能看到两份；八、在途匹配期间 split、merge、migrate 都报 SHARD_BUSY，重复 begin 报 SHARD_ALREADY_IN_FLIGHT，未 begin 就 end 报 SHARD_NOT_IN_FLIGHT；九、迁移中途失败要回滚票据归属，不能留下两份索引；十、rebalance 在总量装得下时把多片合并成一片，装不下时从最大片搬一张到最小片，动作列表按顺序返回，结束后各分片条数差不超过一；十一、评分、时间、分片号非法要分别报 SHARD_INVALID_SCORE、SHARD_INVALID_TIME、SHARD_UNKNOWN_SHARD；十二、快照恢复后分片布局、在途标记、序号与计数都要在。

硬约束：add_ticket、split_shard、merge_shards、migrate_ticket、rebalance 全程持锁，迁移与 locate 并发时每张票据只能被一片持有，六路并发来回迁移一百八十次后 locate 的 copies 必须仍是 1；统计计数必须与实际动作一一对应，失败的调用只增加 rejected；分片数与单分片容量都是硬顶，任何路径不得突破；拆分与合并都要保持剩余票据的相对顺序，任何一次拆分或合并失败都不得移动票据；snapshot 必须深拷贝，restore 结构非法报 SHARD_SNAPSHOT_INVALID 且原布局不变。

验收：`python3 -m pytest -q tests` 的 15 条必须全过；不得修改测试、跳过或按固定输入写死；并发迁移与快照恢复自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-a-04
- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 困难
- module: queue_shard_rebalancer
- typeName: QueueShardRebalancer
