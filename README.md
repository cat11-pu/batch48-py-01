# batch48-py-safety-c-05

## 题面

漏洞工单改成自动排序以后，安全运营发现同一个 CVE 两次刷新会排到不同位置，已经被在野利用的旧漏洞反而排在新披露之后，缺失 EPSS 的条目忽高忽低，批量导入后排序又整体漂移；重复关联让计数虚高，几个大资产把全部漏洞都关联进来后进程内存被打爆，重启恢复后顺序还与之前对不上，运营同学现在不敢直接用自动排序的结果派单，只能人工二次确认。范围是 gsb/batch48-py-safety-c-05 下的 vuln_priority_ranker.py，类 VulnPriorityRanker，负责维护资产与漏洞两本账、记录关联并给出稳定排序。

公开接口：VulnPriorityRanker(*, max_links_per_asset=20000, max_links=200000, half_life_days=180)；upsert_asset(asset_id, *, criticality, exposure)；upsert_vuln(vuln_id, *, cvss=None, epss=None, kev=None, published_at=None)；link(asset_id, vuln_id)；unlink(asset_id, vuln_id)；rank(now=None, *, limit=None)；score(vuln_id, asset_id, now=None)；explain(vuln_id, asset_id, now=None)；state()；restore(state)；stats()。时间参数都是 ISO-8601 字符串。计分公式为 score = (cvss×6 + epss×25 + kev 提升 30) × 资产权重 × 衰减，资产权重等于 criticality（1 到 5）乘以暴露系数，internal 为 1.0、partner 为 1.2、internet 为 1.5；衰减为 0.5 的（年龄天数除以半衰期 180）次方，年龄取 now 与 published_at 之差，负值按 0；资产权重与最终分数都保留六位小数。关联关系是独立的第三本账，同一漏洞可以关联多个资产，同一资产也可以关联多个漏洞，重复导入同一批数据不得产生重复关联。缺失的 cvss、epss、kev、published_at 分别按 10.0、1.0、False、当前时间计分，并在 missing 里列出名称。所有拒绝统一抛 RankerError(code, detail)，code 取 ranker.invalid_asset、ranker.invalid_signal、ranker.invalid_limit、ranker.unknown_asset、ranker.unknown_vuln、ranker.asset_limit、ranker.global_limit、ranker.state_invalid。upsert_asset 返回 asset_id、criticality、exposure、weight；upsert_vuln 返回 vuln_id、cvss、epss、kev、published_at、missing，missing 按字典序；rank 条目含 vuln_id、asset_id、score、cvss、epss、kev；state 含 version、assets、vulns、links 四个键；explain 返回 vuln_id、asset_id、score、components、missing，components 里的 cvss 与 epss 是加权后的贡献值；cvss 超出 0 到 10、epss 超出 0 到 1、kev 非布尔或时间无法解析抛 ranker.invalid_signal，criticality 越界或 exposure 未知抛 ranker.invalid_asset。

故障与边界：一、分数必须严格按上述公式计算并保留六位小数，输入相同则结果逐位相同；二、在野利用标记与过期 EPSS 同时存在时以标记提升为准，EPSS 仍照常参与加权，不能归零；三、published_at 缺失的漏洞按当前时间计算衰减，不能当成零点排到最前面；四、同分排序先比 vuln_id 再比 asset_id，顺序不得随插入顺序或字典遍历顺序变化；五、单资产关联上限 20000、全局上限 200000，超限拒绝新增且不改动已有关系；六、link 幂等，重复关联返回 False 且计数不变，unlink 未关联也返回 False；七、upsert_vuln 覆盖已有信号但保留全部关联，upsert_asset 同理；八、rank 按分数降序、同分按并列键，limit 只截断不改变顺序，非正 limit 抛 ranker.invalid_limit；九、explain 拆出 components 里的 cvss、epss、kev、asset_weight、decay 与 score，缺失信号用默认值参与计分并列入 missing；十、state 返回可 JSON 序列化的深拷贝，restore 后分数与排序逐项一致，结构非法抛 ranker.state_invalid 且原状态不变；十一、并发 rank 与 upsert 只能看到一致快照，关联计数不得丢失或重复；十二、stats 汇总 assets、vulns、links、rank_calls 四个整数。

硬约束：关联写入与计数更新必须原子，十路并发下 links 必须等于真实去重后的关联数；rank 只读当前快照，不能观察到写了一半的资产或漏洞；快照恢复后限额仍然按恢复前的数量生效；重复导入同一批数据必须幂等，不得产生重复关联或虚高的 rank_calls，rank_calls 按公开调用次数统计，内部遍历不计。验收：python3 -m pytest -q tests 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；并发与恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 漏洞检测与攻防
- type: Bugfix
- difficulty: 地狱
- module: vuln_priority_ranker
- typeName: VulnPriorityRanker
