# batch48-py-safety-d-02

## 题面

账号服务迁移后，老用户登录一次就被强制改密，短口令反而先被放行；服务重启后，前一天已经锁定的账号又能继续试口令；安全组压测还发现用不存在的账号登录比用真实账号失败快一个数量级，等于把账号清单暴露出去。范围是 gsb/batch48-py-safety-d-02 下的 password_credential_guard.py，类 PasswordCredentialGuard，负责口令登记、校验、参数升级、pepper 轮换与按账号锁定。

公开接口：PasswordCredentialGuard(*, max_failures=5, lock_window=300, history_limit=32)；policy() -> dict；set_policy(*, algorithm=None, iterations=None, min_length=None, max_length=None) -> dict；rotate_pepper(pepper_id, secret) -> dict；revoke_pepper(pepper_id) -> bool；active_pepper() -> str；register(account, password, *, now=0.0) -> dict；verify(account, password, *, now=0.0) -> dict；change_password(account, old_password, new_password, *, now=0.0) -> dict；params(account) -> dict；lock_state(account, *, now=0.0) -> dict；unlock(account) -> None；history(account) -> list[dict]；snapshot() -> dict；restore(state) -> None；stats() -> dict。拒绝统一抛 PasswordGuardError(code, detail)。

故障与边界：

一、哈希记录带 algorithm、iterations、salt、pepper_id，salt 是十六进制字符串且长度不少于 16，口令原文与哈希都不在任何返回值里回显；verify 必须用记录里的参数重算，返回 account、status、attempts、locked 与 upgraded 五项。

二、口令入口校验：长度 10 到 128，必须同时含字母与数字，首尾不得是空白，违规报 PWD_WEAK；默认策略是 sha256、iterations 2000，只接受 sha256 与 sha512，迭代次数下限 1000，set_policy 参数非法报 PWD_POLICY_INVALID 且策略保持不变。

三、重复登记同一账号报 PWD_ACCOUNT_EXISTS，原记录不受影响。

四、未知账号与已知账号校验失败要返回同一组键、同一 status，且都要走一次等价哈希，不能提前返回省掉哈希；未知账号的 attempts 记为 0，locked 记为假。

五、失败计数按账号累计，成功校验清零；第 max_failures 次失败进入锁定窗口并返回 locked，until 等于失败时刻加 lock_window，窗口内即使口令正确也返回 locked，now 达到 until 后自动放行并清零；lock_state 返回 account、attempts、locked 与 until。

六、参数低于当前策略时 verify 就地升级记录并返回 upgraded=True，第二次校验返回 False；升级不得改变口令语义。

七、rotate_pepper 登记新 pepper 并设为生效，旧 pepper 仍可校验，新登记账号一律落新 pepper；显式撤销 pepper 后，绑定该 pepper 的记录校验报 PWD_PEPPER_UNKNOWN，撤销已撤销的 pepper 返回 False。

八、change_password 先校验旧口令，不符报 PWD_OLD_MISMATCH；新口令按入口规则校验，成功后旧口令立即失效。

九、pepper 轮换后旧 pepper 校验成功的记录要立即迁移到新 pepper，迁移不改变口令语义。

十、并发登录同一账号只产生一次重哈希写入，另一次读到升级后的记录。

十一、history 返回 at、status、upgraded 三个字段并按时间顺序排列，超出 history_limit 淘汰最旧；stats 返回 accounts、verifications 与 failures，其中 verifications 只计成功校验，failures 只计已知账号的失败尝试。

十二、快照含 version、policy、peppers、accounts 与 stats，恢复后哈希记录、pepper 表与锁定窗口都还在，恢复后的记录继续按当前策略判断是否升级；结构非法报 PWD_SNAPSHOT_INVALID 且原状态不动，快照里不得出现口令原文。

硬约束：verify 与 set_policy 并发时不得读到半套参数；同一账号的失败计数与锁定状态更新必须原子；快照与恢复跨进程后锁定窗口仍按绝对时刻判定，同一份快照连续恢复两次结果一致；未知账号分支的耗时与已知账号失败同阶。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；参数升级与重启后的锁定状态自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- difficulty: 困难
- module: password_credential_guard
- file: password_credential_guard.py
