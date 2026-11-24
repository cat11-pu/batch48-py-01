# batch48-py-safety-a-04

## 题面

身份中心发用户信息出事：客户端换个配置就拿到全部字段；组展开上万条 claim 撑爆响应体；手机号没按分级裁掉；未声明字段被原样带出；两份策略同时生效，令牌时新时旧；重启后策略与组映射全丢。在 gsb/batch48-py-safety-a-04 的 claim_release_policy.py，类 ClaimReleasePolicy。

接口：ClaimReleasePolicy(*, 三个上限)；define_claim(name, pii_tier, multivalued)；define_group(name, claims, parent)；set_client_policy(client_id, required, optional, forbidden, max_pii_tier)；release_claims(client_id, subject, groups)；explain。拒绝抛 ClaimPolicyError。

故障边界（前缀 CLAIM_）：①claim 去重排序，版本自 1；②未定义 claim 报 UNDECLARED；③三集合总数超 max_claims_per_client 报 CAPACITY_EXCEEDED；④未声明字段拒绝进 dropped；⑤forbidden 永不发布，与 required 冲突按 forbidden 并记冲突；⑥required 缺失报 REQUIRED_MISSING 计 denied；⑦PII 高于 max_pii_tier 裁掉进 dropped；⑧组展开沿 parent 至多 max_group_depth 层；⑨超 max_expanded_claims 只截断可选并置 truncated，必需项不得丢；⑩版本加一，签发记版本；回滚后旧记录版本保留；⑪并发读只见旧版或新版完整策略；⑫快照深拷贝，非法结构 SNAPSHOT_INVALID 状态不变。

约束：展开与发布同锁，计数与 dropped 一致；上限硬顶；快照恢复后策略、组映射、计数都在且不共享。stats 返回 claims、groups、policies、released、denied、truncated。

验收：`python3 -m pytest -q tests` 15 条全过；不改测试、不跳过、不写死；仅本地。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- module: claim_release_policy
- typeName: ClaimReleasePolicy
