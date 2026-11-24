# batch48-py-safety-b-13

## 题面

系统调用网关连续两周被容量事故登记：按秒记账的令牌补充让一个本该每秒两次的组实际跑了将近三次；突发额度在空闲窗口里越攒越多，峰值直接把下游打穿；裁决已经写着拒绝，调用方仍然把系统调用发了出去；同一个进程组拆成四个工作进程后各自领一份桶，总速率翻了两番；值班看板上拒绝数被混进通过数，谁也说不清到底拦了多少；低流量的组排了一晚上也没轮到一次；名单之外的调用默认放行；时钟回拨送来旧样本，桶里的令牌凭空变多。范围是 gsb/batch48-py-safety-b-13 的 syscall_rate_governor.py，类 SyscallRateGovernor，负责按进程组维护令牌桶、给出放行裁决、补充令牌、统计与公平轮转，所有拒绝抛 RateGovernorError(code, detail)。

公开接口：SyscallRateGovernor(*, max_groups=64, max_calls=128, clock=None)；configure(group, rate, burst, calls=None, now=0) -> dict；admit(group, call, now, pid=None) -> dict；refill(group, now) -> dict；tokens(group, now=None) -> float；stats(group) -> dict；reset(group, now=0) -> dict；pick(groups, now) -> str；groups() -> list；remove(group) -> dict；snapshot() -> dict；restore(state) -> None。

桶容量为 burst，rate 为每秒补充的令牌数，令牌按浮点累计，不按整秒取整；补充只在每次操作时按距上次补充的秒数一次算清，上限为 burst。admit 返回 allowed、group、call、reason 与 remaining，放行时 reason 为 ok 并扣掉一枚令牌，额度不足时 reason 为 limit，调用不在白名单时 reason 为 unknown_call；stats 返回 group、admitted、denied、tokens、rate、burst 与 last_refill；refill 返回 group、tokens、added 与 last_refill；configure 返回 group、rate、burst、calls 与初始 tokens；reset 保留配置、把桶补满并清零计数，返回 group 与 tokens；remove 返回 group；groups 返回已配置组名列表。

故障与边界：一、rate 非正、burst 小于一、组名为空、调用名不合法、白名单为空都拒绝，组数与白名单条目受构造参数上限约束；二、令牌按浮点秒累计，半秒也要补出半枚，不得向下取整到整秒；三、空闲再久，桶里令牌也不得超过 burst；四、被拒绝的调用不扣令牌、不计入 admitted，也不得被上游当作已执行；五、未配置白名单时任何调用都按令牌裁决，配置了白名单则不在名单内的调用按未知调用拒绝；六、同一组的多枚令牌在并发扣减下不得发给两个调用方；七、now 早于该组上次补充时刻按时钟回拨拒绝，不得用旧样本补令牌；八、refill 在同一时刻重复调用只生效一次，added 第二次为零；九、reset 只清计数并补满桶，不改配置；十、pick 在给定组中选最久未被放行的组，从未放行过的组优先，同刻按组名字典序；十一、同组多个进程共享一个桶，补充只能发生一次，按进程各补一次会让总速率翻倍；十二、拒绝要按额度不足与未知调用分成可区分的计数，审计里能与执行失败分开，不能混成一类。

快照返回 version 与 groups，groups 以组名为键保存配置、剩余令牌、上次补充时刻、上次放行时刻与各项计数，内容为深拷贝；恢复后桶不得凭空补满，统计与上次补充时刻要和恢复前一致，结构非法抛 RATE_SNAPSHOT_INVALID 且原状态不变。硬约束：admit 与 refill 全程持锁，八路并发抢同一枚令牌时只能有一个调用方拿到；补充只按组做一次，不得按进程重复补充；组数、白名单条目与令牌上限都是硬顶。

拒绝码取 RATE_GROUP_INVALID、RATE_CONFIG_INVALID、RATE_GROUP_LIMIT、RATE_UNKNOWN_GROUP、RATE_CALL_INVALID、RATE_CLOCK_BACKWARDS、RATE_PICK_EMPTY、RATE_SNAPSHOT_INVALID。验收：python3 -m pytest -q tests 的 15 条全部通过，不得修改测试、跳过或按固定输入写死；共享桶补充与并发扣减自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 地狱
- module: syscall_rate_governor
- typeName: SyscallRateGovernor
