# batch48-py-safety-a-15

## 题面

密钥后台三张告警：同租户两把密钥前缀撞车，只认出一把；撤销的密钥换了同值新密钥又被放行；轮换窗口内旧密钥无人统计使用量。落点是 gsb/batch48-py-safety-a-15 的 api_key_lifecycle.py，类 ApiKeyLifecycleManager，管签发与校验。

公开接口：ApiKeyLifecycleManager(*, max_keys_per_tenant=5, max_scopes_per_key=4, prefix_len=8)；issue_key、list_keys、revoke_key、rotate_key、verify_key、touch_last_used、flush_last_used、stats、snapshot、restore。拒绝抛 ApiKeyError，错误码为 APIKEY_ 加后缀：UNKNOWN、SCOPE_DENIED、REVOKED。返回：issue 与 rotate 给 key_id 与 secret，issue 另含 scopes；verify 给 ok 与 key_id；list_keys 项含 key_id、state、last_used。

故障与边界：①明文 ak 开头只回一次，仅存前缀与摘要，列表与快照无明文；②作用域超 max_scopes_per_key 或为空都报错，租户条数超限报错；③校验按前缀取全部候选做常量时间比对，同前缀多把都参与，禁止只取第一条；④scope 不符报 SCOPE_DENIED，来源不在 cidrs 内报错；⑤now 达到 expires_at 即失效；⑥撤销幂等、再校验报 REVOKED，撤销后同值新密钥不被旧撤销记录误伤；⑦轮换继承作用域与到期，旧密钥在重叠期可用、窗口结束失效；⑧重叠期旧密钥使用计入轮换统计；⑨touch_last_used 合并写取最大，多次只落一次盘；⑩list_keys 按 key_id 升序，state 标 active 与 revoked。

硬约束：签发与校验串行，并发签发不超条数上限；密钥数与作用域数硬顶；重启快照恢复摘要、撤销与轮换，非法快照不动原状态。

验收：`python3 -m pytest -q tests` 15 条全过；不改测试、不跳过、不写死；并发与重启自验；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- difficulty: 困难
- module: api_key_lifecycle
- typeName: ApiKeyLifecycleManager
