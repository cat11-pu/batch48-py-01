# batch48-py-safety-b-11

## 题面

值班平台的提权台账这周被审计挑出一串问题：有人用自己提交的申请单给自己放行；两个人头其实是同一个审批账号在两张表里各记了一次；申请的是运维角色，批下来却能按管理员调用；授权凭据抄给另一个会话照样能用；晚上提交、凌晨才批下来的单子，系统按提交时刻算过期，刚到手就失效；被拒绝的单子过一会儿还能兑换；已经撤销的凭据在缓存里继续放行；理由栏敲一串空格也能通过。范围是 gsb/batch48-py-safety-b-11 的 jit_elevation_broker.py，类 ElevationBroker，负责申请受理、双人审批、按会话发放短期授权、回收与到期清点，所有拒绝抛 ElevationError(code, detail)。

公开接口：ElevationBroker(*, max_ttl=3600, default_ttl=900, required_approvals=2, max_pending=64, clock=None)；register_approver(approver, roles) -> dict；revoke_approver(approver) -> dict；request_access(subject, role, reason, ttl=None, now=None) -> dict；request_status(request_id) -> dict；approve(request_id, approver, now=None) -> dict；deny(request_id, approver, note, now=None) -> dict；redeem(request_id, session_id, now) -> dict；validate(grant_id, session_id, role=None, now=None) -> dict；revoke(grant_id, now=None) -> dict；expire(now) -> int；grants(subject=None) -> list；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。

编号从 1 起，申请单形如 req-1，授权形如 grant-1；角色阶为 viewer、operator、admin、owner，授权覆盖不高于自身阶的角色。request_access 返回 request_id、subject、role、reason、ttl、created_at、state 与 approvals，省略 ttl 时取 default_ttl；approve 返回 request_id、approvals、required 与 state，凑满 required_approvals 置 approved 并保持表态顺序；redeem 返回 grant_id、subject、role、session_id、issued_at、expires_at 与 state；validate 返回 grant_id、subject、role、requested_role、expires_at 与 remaining，remaining 为到期时刻减当前时刻的整秒数；request_status 返回与 request_access 相同的字段；deny 返回 request_id、note 与 state；revoke 返回 grant_id 与 state；stats 固定返回 requests、approvals、denials、grants、revocations。

故障与边界：一、理由去空白后为空、角色未知、ttl 非正或超过 max_ttl 一律拒绝，待批单数达到 max_pending 时新申请拒绝；二、同一主体同一角色的未结申请只允许一条，重复提交拒绝且不占编号；三、审批人不得是申请人；四、同一审批人对同一申请只计一次，重复表态拒绝且计数不变；五、审批人未登记或其可批角色不含被申请角色时拒绝；六、待批单兑换按审批未满足拒绝，已拒绝、已兑换、已过期的单子兑换按状态冲突拒绝；七、到期时刻从兑换时刻加 ttl 起算，不得回溯到申请时刻，到期判定含等于；八、授权绑定兑换时的会话，换会话校验拒绝，空会话在兑换与校验时都拒绝；九、撤销后校验拒绝，重复撤销幂等并返回已撤销状态；十、校验请求的角色高于授权角色时拒绝，等于或低于时放行并回显所请求角色；十一、审批人资格被回收后，其此前表态不得继续支撑尚未兑换的申请；十二、同一主体同时持有多份授权时权限按并集生效，但回收必须逐条独立，不能一撤全撤。

expire(now) 只把到期授权置为过期并返回条数，未到期授权不受影响。快照返回 version、approvers、requests、grants、stats，requests 以 request_id 为键并含 approvals 列表，内容必须是深拷贝；恢复后未到期授权继续有效、已兑换的单子不能二次兑换，结构非法抛 ELEV_SNAPSHOT_INVALID 且原状态不变。硬约束：同一申请单被多路并发兑换时只能发出一份授权，其余按状态冲突失败；同一审批人的表态在并发下也只能记一次；审批人与申请单的集合有上限，统计与快照在并发下不得错账。

拒绝码取 ELEV_REQUEST_DUPLICATE、ELEV_REASON_INVALID、ELEV_ROLE_INVALID、ELEV_TTL_INVALID、ELEV_SELF_APPROVAL、ELEV_APPROVER_DUPLICATE、ELEV_APPROVER_UNAUTHORIZED、ELEV_APPROVALS_PENDING、ELEV_REQUEST_STATE、ELEV_NOTE_INVALID、ELEV_GRANT_EXPIRED、ELEV_GRANT_REVOKED、ELEV_SESSION_MISMATCH、ELEV_SESSION_REQUIRED、ELEV_ROLE_OUT_OF_SCOPE、ELEV_REQUEST_LIMIT、ELEV_SNAPSHOT_INVALID。验收：python3 -m pytest -q tests 的 15 条全部通过，不得修改测试、跳过或按固定输入写死；并发兑换与重启恢复自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 地狱
- module: jit_elevation_broker
- typeName: ElevationBroker
