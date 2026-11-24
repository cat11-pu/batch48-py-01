# batch48-py-safety-a-19

## 题面

接入层限速上线后，运营反馈了几条异常：把策略版本从一档改到另一档，原本已经被打到黄色的流瞬间又能满速突发，改一次策略就白送一整个桶；单速率与双速率模式来回切换时，两个桶各算各的，承诺速率内的流量反而被丢；长时间没有流量之后，下一次请求拿到的补充量明显超过桶容量，成批突发穿过限速器。还有按流的隔离问题：一条流被打红后，同策略下的其他流也跟着丢包；彩色报文被重新标成绿色，上游已经降级的标记被洗掉。

范围是 gsb/batch48-py-safety-a-19 下的 traffic_policer_guard.py，类 TrafficPolicerGuard，负责策略声明与升级、按流挂载、三色标记、令牌桶补充与彩色感知动作。

公开接口：`TrafficPolicerGuard(*, max_flows=16, clock=None)`；`declare_policy(name, *, mode="single", committed_rate=1000, peak_rate=None, burst=1500, peak_burst=None, version=1) -> dict`；`policy_info(name) -> dict`；`upgrade_policy(name, *, mode=None, committed_rate=None, peak_rate=None, burst=None, peak_burst=None) -> dict`；`set_mode(name, mode) -> dict`；`attach(flow, policy) -> dict`；`detach(flow) -> dict`；`flow_state(flow, *, now_ms=0) -> dict`；`classify(flow, size, *, now_ms=0, color=None) -> dict`；另有 stats、reset_stats、snapshot、restore。拒绝统一抛 PolicerError(code, detail)。

故障与边界：一、策略名必须是非空字符串且不超过 64 字符，mode 只接受 single 与 dual，承诺速率、峰值速率、两个桶容量与版本都取正整数，非法报 TP_INVALID_POLICY，dual 模式必须同时给出 peak_rate 与 peak_burst，缺一个也报 TP_INVALID_POLICY；二、同名策略同参数重复声明幂等，参数不同报 TP_DUPLICATE_POLICY；三、流名非法报 TP_INVALID_FLOW，策略不存在报 TP_UNKNOWN_POLICY，流数超过 max_flows 报 TP_FLOW_LIMIT，重复挂载同一策略幂等；四、classify 的字节数必须是正整数，否则 TP_INVALID_BYTES，时间倒退报 TP_TIME_REGRESSION，未知流报 TP_UNKNOWN_FLOW；五、单速率模式下桶内有足够令牌标绿并转发并扣令牌，令牌不足但不超过桶容量标黄并打标转发，单包超过桶容量直接标红丢弃且不扣令牌；六、双速率模式先看峰值桶，峰值桶不足标红丢弃，承诺桶不足标黄并只扣峰值桶，两个桶都够标绿并同时扣两个桶；七、每流桶状态独立，一条流超速只影响自己，不能波及同策略的其他流；八、桶补充按整数毫秒累加，速率换算成字节每秒，补充量向下取整到字节，余数保留在桶内累计，长时间空闲后一次补充不得越过桶容量；九、策略换版本或重新下发时已消耗的令牌不得被重置，否则改一次策略就能刷满突发额度；十、同一流在单速率与双速率模式之间切换时，已存在的桶状态要按更严格的一侧折算，不能两个桶各算各的；十一、预着色为黄或红的报文不得被升级成绿，红色报文直接丢弃且不消耗令牌；十二、snapshot 是深拷贝，restore 结构非法报 TP_SNAPSHOT_INVALID 且原状态不变。

硬约束：declare_policy、upgrade_policy、set_mode、attach、classify、snapshot 全程持锁，四路并发各打自己的流时，总分类计数与绿黄红分项必须与调用次数一致，不能出现桶状态互相覆盖；每个桶的令牌上限就是对应桶容量，补充不得越过上限，令牌与余数都用整数保存，不出现浮点漂移；流数、策略参数与桶容量都是硬顶，超限拒绝而不是截断或排队；快照恢复后策略表、每流桶状态、挂载关系与统计都要在，且不得与恢复前的对象共享结构。

验收：`python3 -m pytest -q tests` 的 15 条必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-safety-a-19
- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 困难
- module: traffic_policer_guard
- typeName: TrafficPolicerGuard
