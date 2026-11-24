# batch48-py-safety-c-15

## 题面

供应商每次交付都带一份 SBOM，安全组还在靠人眼比对：哪个组件从 2.1.0 降到 1.8.0 看不出来，开发期依赖混进运行时也没人发现；同一个包写成两种大小写和限定参数顺序，被当成一进一出报了两次；一个组件同时降级又改作用域，报表里出现两条互相矛盾的记录；确认过的漂移下次比对又原样弹出来，白名单到期了反而不再提醒。范围是 gsb/batch48-py-safety-c-15 的 sbom_drift_detector.py，类 SbomDriftDetector，比对基线侧与当前侧的组件图，输出漂移类别并保留判定依据。

公开接口：SbomDriftDetector(*, max_components=100000)；load(side, components, edges) -> dict；diff(at=None) -> dict；explain(component) -> dict；accept(component, until=None, note=None) -> dict；revoke(component) -> bool；whitelist() -> list[dict]；components(side) -> list[dict]；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 SbomDriftError(code, detail)。

load 的 side 只接受 base 与 current，组件含 purl、version、scope，scope 只接受 runtime 与 development。purl 先做大小写与限定参数归一化再比较；版本按 X.Y.Z 语义化版本比较，前导 v 可忽略，无法解析的版本归入 unknown，既不参与升降级判定也不静默丢弃。diff 固定返回 added、removed、upgraded、downgraded、scope_changed、unknown 六组，组内按 purl 升序。

故障与边界：

1. side 非法、purl 不是 pkg: 开头、scope 非法、version 为空都按对应错误拒绝，整次 load 不生效。
2. 同一侧归一化后重复的 purl 抛 DUPLICATE_COMPONENT；单侧组件数超过 max_components 抛 SBOM_CAPACITY_EXCEEDED，旧的一侧数据保持原样。
3. 依赖边端点必须在同侧组件里存在，否则抛 UNKNOWN_COMPONENT；重复边幂等，只计一条；指向自身的边允许存在，不算缺失。
4. diff 在任一侧未载入时抛 SBOM_INCOMPLETE；同一组件多项变化时按 removed、downgraded、scope_changed、upgraded、added 的固定优先级只报一条。
5. 降级与作用域从开发变运行时单独成类，且优先于升级；判定顺序不随字典遍历变化。
6. 版本相同时作用域变化只进 scope_changed；版本与作用域都没变不进任何一组；版本比较只看三段数字，1.10.0 必须高于 1.9.9，不得按字符串比较。
7. 白名单命中只改变提示状态，不得把组件从 diff 结果里删除；accept 未知组件抛 UNKNOWN_COMPONENT。
8. 白名单带到期时间，到期后重新提示；重复 accept 更新到期时间与备注，revoke 返回是否真的移除。
9. explain 给出 purl、category、from、to、scope_from、scope_to，category 取 added、removed、upgraded、downgraded、scope_changed、unchanged、unknown 之一。
10. 快照带版本号，恢复后两侧组件、边、白名单与计数一致；结构非法抛 SBOM_SNAPSHOT_INVALID，原状态不动。

硬约束：并发 diff 与 accept 安全，读方不得看到半份载入的组件表；diff 结果按类别与 purl 排序稳定，同一输入连续两次逐项一致；六组键必须固定存在，某一类为空时给空列表；组件上限是硬顶，拒绝时不留部分结果；返回的组件、白名单与快照都是深拷贝，调用方改动不回流；同一 purl 的归一化结果必须每次相同。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与快照恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 漏洞检测与攻防
- type: Bugfix
- difficulty: 困难
- module: sbom_drift_detector
- typeName: SbomDriftDetector
