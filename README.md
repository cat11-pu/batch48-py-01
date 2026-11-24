# batch48-py-game-a-03

## 题面

新赛季上线角色队列后，组队投诉集中在几件事：明明五个人的首选都是输出，系统却凑不出一支有坦有奶的队伍；被自动补位到治疗的人下一把还是被补位，而同队主动选冷门角色的人反倒先拿到首选；队伍成团后两个玩家换角色，换完出现了两个治疗、没有坦克；有人取消排队重新进队，等待时间没有清零，补位次数却跟着丢了；还有队伍在开局前把角色换来换去，最后阵容和配额对不上。客服按角色维度拉了三天数据，发现补位人数与补位次数对不上，保护权重也没有真正落到账号上。

范围是 gsb/batch48-py-game-a-03 的 role_queue_allocator.py，类 RoleQueueAllocator，负责角色偏好票据、按配额组队、补位保护权重与开局角色锁定。

公开接口：RoleQueueAllocator(*, roles=("tank", "healer", "dps"), quotas=None, team_size=5, queue_limit=200, protection_cap=5, clock=None)；enqueue(player: str, *, primary: str, secondary: str | None = None, at: int = 0) -> dict；cancel(player: str) -> dict；queue_state(now: int = 0) -> list；autofill_protection(player: str) -> dict；form_team(team_id: str, *, quotas=None) -> dict；team_state(team_id: str) -> dict；swap_role(team_id: str, player_a: str, player_b: str) -> dict；change_role(team_id: str, player: str, new_role: str) -> dict；另有 leave_team、lock_team、stats、reset_stats、snapshot、restore。拒绝统一抛 RoleQueueError(code, detail)。

故障与边界：一、每个角色配额之和必须等于 team_size，配额缺角色、含未知角色或非正整数都报 ROLE_INVALID_QUOTA；二、同一玩家同时只允许一张票据，重复入队报 ROLE_ALREADY_QUEUED，队列有硬顶报 ROLE_QUEUE_FULL；三、首选与次选必须不同，次选非法或与首选相同分别报 ROLE_INVALID_ROLE 与 ROLE_PREFERENCE_INVALID；四、组队先按首选填满各角色，首选不足再用次选，仍不足才自动补位，未被选中的票据留在原队列且顺序不变；五、自动补位要计入补位次数与保护权重，权重有上限，主动选冷门角色不消耗保护；六、队伍成团后 swap_role 只在两名成员之间交换角色，同一玩家或同一角色都拒绝；七、change_role 只在目标角色未满时生效，超出配额报 ROLE_QUOTA_EXCEEDED，空出来的角色槽位允许暂时为空；八、lock_team 之后换角色与离队都被拒绝，重复锁定报 ROLE_ALREADY_LOCKED；九、取消后重新入队不继承旧的等待时间，但补位保护按账号保留；十、队伍成团后互换角色必须保持每角色配额，不能出现两个同角色；十一、票据与队伍的等待时间按传入的 now 计算，不得依赖系统时钟；十二、快照恢复后队列、队伍、保护权重与计数都要在。

硬约束：enqueue、cancel、form_team 全程持锁，十二路并发对同一玩家入队只能有一张票据，其余返回 ROLE_ALREADY_QUEUED，queued 与 rejected 计数必须精确；组队只能消费 team_size 张票据，多余票据留在原位置；每队每角色配额是硬顶，任何路径都不得突破；任何一次组队失败都不得消耗票据或改动配额；snapshot 必须深拷贝，restore 结构非法报 ROLE_SNAPSHOT_INVALID 且原状态不变。

验收：`python3 -m pytest -q tests` 的 15 条必须全过；不得修改测试、跳过或按固定输入写死；并发入队与快照恢复自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-a-03
- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 困难
- module: role_queue_allocator
- typeName: RoleQueueAllocator
