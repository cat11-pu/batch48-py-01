# batch48-py-safety-a-14

## 题面

风控周报：同一批账号凌晨集中登录，评分却全在放行区间；同一个失陷信号连报十几遍，分数被反复叠加；白名单合作方偶尔仍被推去做加验。落点是 gsb/batch48-py-safety-a-14 的 login_risk_evaluator.py，类 LoginRiskEvaluator，按信号给分并落到放行、加验或拒绝。

公开接口：LoginRiskEvaluator(*, weights=None, thresholds=None, half_life=3600, window=600, signal_cap=10)；register_signal、score_login、set_list、decide、explain、record_outcome、history_score、decay、stats、snapshot、restore。拒绝抛 LoginRiskError(code, detail)。

故障与边界：①五个信号键 new_device、geo_velocity、failures、odd_hour、source_reputation，未知键抛 LOGIN_RISK_UNKNOWN_SIGNAL，负数或非数值抛 LOGIN_RISK_INVALID；②贡献为权重乘 min(值, signal_cap)/signal_cap，分数为贡献和加历史分；③同一信号重复上报不得重复加权，按键去重取最新值；④信号只在窗口内参与评分，超出失效；⑤低于 allow 放行，allow 与 step_up 之间加验，达到即拒绝；⑥白名单命中决策放行且不加验，但仍记录风险分；⑦黑名单直接拒绝；⑧失败累加、成功扣减，历史分按半衰期衰减且只作用于累计分，不改本次原始信号；⑨explain 返回贡献与命中顺序；⑩并发登录只读完整信号集。

硬约束：评分与信号写入串行；历史分按半衰期单调衰减；信号表按窗口裁剪；重启后快照恢复信号、历史、名单与计数，非法快照 LOGIN_RISK_SNAPSHOT_INVALID 不改原状态。stats 返回 signals、scored、decided、outcomes、rejected。

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
- module: login_risk_evaluator
- typeName: LoginRiskEvaluator
