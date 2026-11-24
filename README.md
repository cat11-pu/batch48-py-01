# batch48-py-safety-a-05

## 题面


同一会话多标签页续期频发：两个标签页同时刷新时一个成功一个报错，管理员强制下线后挂起标签页第二天仍能用旧令牌换新会话。范围 gsb/batch48-py-safety-a-05 的 session_tab_synchronizer.py，类 SessionTabSynchronizer。

公开接口：SessionTabSynchronizer(heartbeat_ttl=30.0, max_tabs_per_session=8)；open_tab、heartbeat、claim_refresh、complete_refresh、await_refresh、publish_session、broadcast_logout、expire_tabs、snapshot/restore/stats。拒绝抛 SessionTabError(code, detail)，code 以 SESSION_ 开头。

故障与边界：①open_tab 能力去重按字典序保存，重复登记幂等、只刷心跳并计 reopened；②tab_id 属于别的会话报 SESSION_TAB_TAKEN，会话不存在报 SESSION_UNKNOWN；③每会话活标签页受 max_tabs_per_session 硬顶，超限报 SESSION_TAB_CAPACITY_EXCEEDED 且不落表；④heartbeat 的 valid 表示心跳前世代是否一致，完成后标签页世代对齐会话；⑤心跳超 heartbeat_ttl 在 heartbeat 与 expire_tabs 时回收并计 expired；⑥同会话同世代只签发一个刷新租约，其余标签页得到 status=wait 和同一 claim_id；⑦同标签页重复 claim 幂等返回同一租约，同 claim_id 的 complete_refresh 只接受一次；⑧complete_refresh 成功世代加一并让所有标签页失效，失败世代不变、计 refresh_failures，失败结果对本世代等待方一致；⑨publish_session 目标世代必须大于当前，否则报 SESSION_GENERATION_REGRESSION；⑩broadcast_logout 返回被通知标签页数并终止会话，重复登出返回 0；⑪挂起标签页恢复后先心跳对齐，用旧世代刷新报 SESSION_STALE；⑫快照覆盖 sessions 与 stats，restore 非法结构报 SESSION_SNAPSHOT_INVALID。

硬约束：claim_refresh、complete_refresh、broadcast_logout 同锁串行，八路并发争同一会话只产生一个 granted，等待方 claim_id 与 owner 一致；登出与刷新完成并发以登出为准；世代号只增不减；标签页表与心跳回收保证有界；快照恢复不与原结构共享。

验收：python3 -m pytest -q tests 15 条全过；不改测试、不跳过、不写死；并发与重启自行验证；只做本地实现。
## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- module: session_tab_synchronizer
- typeName: SessionTabSynchronizer
