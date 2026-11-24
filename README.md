# batch48-py-safety-a-03

## 题面

令牌端点出事：轮换密钥后旧 kid 请求全挂；同一断言期内重放两次都过；超长有效期也放行；aud 写错端点仍验过；jti 台账无上限，重启后已消费 jti 全丢。在 gsb/batch48-py-safety-a-03 的 client_assertion_guard.py，类 ClientAssertionGuard。

接口：ClientAssertionGuard(endpoint, max_ttl, jti_ttl, max_jti)；register_key(client_id, kid, secret, active)；verify_assertion(assertion, now)；consume_jti(client_id, jti, now)；purge_jti(now)；snapshot/restore/stats。拒绝抛 ClientAssertionError；断言 header.payload.signature 三段、HS256。

故障边界（前缀 ASSERTION_）：①三段解码或 JSON 非法 MALFORMED，iss≠sub 报 ISS_MISMATCH；②未知客户端 UNKNOWN_CLIENT，未知/退役 kid 报 UNKNOWN_KID/KID_RETIRED，同 kid 换密钥 KEY_CONFLICT，同密钥重复登记幂等；③签名不符 SIGNATURE_INVALID；④aud 串等于本端点、数组含本端点；⑤exp≤now 报 EXPIRED，iat>now 报 NOT_YET_VALID；⑥exp-iat 超 max_ttl 必须拒绝不得截断；⑦重叠期两把密钥都能验，只认活跃 kid；⑧consume_jti 一次性，重复 JTI_REPLAY；⑨台账按 jti_ttl 回收，超 max_jti 报 JTI_LEDGER_FULL；⑩快照深拷贝，非法结构 SNAPSHOT_INVALID。

约束：同一 jti 并发消费仅一次；台账容量硬顶按 TTL 回收；重启后密钥可验、已消费 jti 仍拒绝。stats 返回 keys、retired、verified、rejected、jti、purged。

验收：`python3 -m pytest -q tests` 15 条全过；不改测试、不跳过、不写死；仅本地。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- module: client_assertion_guard
- typeName: ClientAssertionGuard
