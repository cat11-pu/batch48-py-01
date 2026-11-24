# batch48-py-safety-d-05

## 题面

客服发现同一张恢复码被两个浏览器先后核销成功，账号在两边都被接管；用户主动重新生成恢复码之后，旧码还能继续登录，等于新批次只是多印了一批纸；还有一批账号在一次口令泄露后被撞库，失败计数却随着服务重启归零，攻击者可以无限次试码。范围是 gsb/batch48-py-safety-d-05 下的 recovery_code_guard.py，类 RecoveryCodeGuard，负责恢复码批签发、一次性核销、纪元失效与快照。

公开接口：RecoveryCodeGuard(*, code_count=10, code_length=10, code_ttl=None, max_failures=5, lock_window=300, history_limit=32)；generate(account, *, now=0) -> dict；regenerate(account, *, now=0) -> dict；redeem(account, code, *, now=0) -> dict；status(account, *, now=0) -> dict；epoch_of(account) -> int；remaining(account) -> int；unlock(account) -> None；normalize(code) -> str；snapshot() -> dict；restore(state) -> None；stats() -> dict。拒绝统一抛 RecoveryCodeError(code, detail)。

故障与边界：

一、generate 生成 code_count 条 code_length 位的大写字母数字恢复码，只保存摘要，明文只在返回值里出现一次；同一账号再次 generate 报 RC_BATCH_EXISTS。

二、code_count 取 1 到 32、code_length 取 8 到 32，越界报 RC_COUNT_INVALID，整批不落地；空账号与非法配置都不得留下半批恢复码。

三、输入先归一化：去掉空白与连字符再转大写，为空或超过 128 个字符报 RC_CODE_INVALID。

四、redeem 成功把该码标记已用，返回 account、status、attempts、locked、epoch 与 remaining；同一码再次核销报 RC_CODE_USED，剩余数量不再变化。

五、批次可带 TTL，now 达到过期时刻报 RC_CODE_EXPIRED，过期批次不消耗任何码，也不增加失败计数；不带 TTL 时 expires_at 返回 None，过期后不允许靠旧批次绕过，必须重新签发一批。

六、失败计数按账号累计，成功核销清零；连续失败达到 max_failures 抛 RC_LOCKED，锁定窗口内即使码正确也抛 RC_LOCKED，now 达到窗口结束自动放行并清零。

七、未知账号核销要走等价比较，返回与已知账号失败同形状的结果而不抛账号不存在，attempts 为 0；status、remaining 与 epoch_of 对未知账号报 RC_ACCOUNT_UNKNOWN。

八、regenerate 递增纪元并把此前所有码置为失效，而不仅是新增一批，返回新一批码并重置失败计数与锁定。

九、并发核销同一恢复码只能有一个成功，其余拿到 RC_CODE_USED，已用状态不得丢失或回滚。

十、快照含 version 与 accounts 等字段，恢复后核销照常进行；结构非法报 RC_SNAPSHOT_INVALID，原状态不得被改动。

十一、恢复码表在快照恢复后仍然只存哈希，不含任何可还原的原文。

十二、status 返回 epoch、remaining、used、failures 与 locked，locked 按传入时刻判断，窗口结束即回到假；已用码、过期码与旧纪元码分别用 RC_CODE_USED、RC_CODE_EXPIRED、RC_CODE_STALE 三个码区分。

硬约束：同一账号的失败计数、锁定状态与码的已用标记必须原子更新，并发核销不得出现两个成功；快照与恢复跨进程后纪元、剩余数量与锁定窗口一致，同一份快照连续恢复两次结果相同；每账号恢复码数量与账号数都有硬顶，失败核销不消耗码也不占用名额。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发核销与纪元失效自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- difficulty: 困难
- module: recovery_code_guard
- file: recovery_code_guard.py
