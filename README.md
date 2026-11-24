# batch48-py-safety-c-02

## 题面

特征库搬迁之后，样本检测平台出现两类互相矛盾的投诉：跨读取块切开的样本开始漏报，带掩码字节的旧特征又把正常文件打成告警；同一偏移被多条特征命中时，告警列表的顺序每次都不一样，取流统计也对不上账，值班同学只能人工对账，一次全量扫描要花掉半天。范围是 gsb/batch48-py-safety-c-02 下的 signature_matcher.py，类 SignatureMatcher，负责把字节特征编译成匹配自动机，对流式数据分块投喂并给出稳定命中。

上传链路会把大文件切成不定长的小块交给匹配器，块边界完全由读取缓冲决定，既不保证落在特征内部，也不保证不会把一条特征切开。特征文本按空格分隔字节，每个字节写成两位十六进制；?? 表示整字节通配，形如 a? 或 ?b 的半字节掩码也允许，十六进制大小写不敏感。单个特征长度为 4 到 4096 字节，特征总数上限 20000，单次 feed 的输入上限 8MB。公开接口：SignatureMatcher(*, max_patterns=20000, max_chunk=8388608)；build(patterns)；build_errors()；pattern_ids()；pattern_count()；feed(chunk, eof=False)；reset()；stats()；hits_for(pattern_id)；state()；restore(state)；pending_bytes()。build 返回 compiled 与 rejected 两个计数；feed 返回本块新增命中，条目固定含 pattern_id、offset、length，offset 是整条流的绝对字节位置；所有拒绝统一抛 SignatureError(code, detail)。build_errors 只保留最近一次构建的拒绝条目，成功构建后为空；空块返回空列表且不改变流状态；超出上限的模式按输入顺序拒绝；pattern_ids 按字典序返回；pending_bytes 返回当前保留的尾巴字节数，最多为最长模式字节数减一；state 含 version、patterns、counts、offset、pending 五个键。

故障与边界：一、模式过短、过长、含非法字符或数量超限时只登记到 build_errors，条目含 id、code、detail，code 取 sig.pattern_too_short、sig.pattern_too_long、sig.pattern_syntax、sig.pattern_limit；二、构建失败后旧的自动机与已统计命中必须原样保留，不能被半成品替换；三、跨块命中只能在数据补齐的那一次 feed 里报一次，尾巴保留最长模式字节数减一；四、同一偏移被多个模式命中时按模式 ID 升序全部返回，命中列表整体按偏移升序；五、掩码位按通配处理但照常占匹配长度，偏移以模式首字节位置为准；六、eof 为真时不足一个完整模式长度的流尾部仍要参与一次匹配，避免尾部漏报；七、超过 8MB 的 chunk 抛 sig.chunk_too_large，已建立的自动机与流失量位置不得前移；八、尚未成功构建就调用 feed 抛 sig.not_built，空特征集同样视为未就绪；九、reset 只清空流状态与尾巴，模式集合和累计命中数保留；十、state 必须是可 JSON 序列化的深拷贝，restore 到新对象后能接着匹配且计数延续，结构非法抛 sig.state_invalid 且原对象不受影响；十一、重建自动机不得丢失已统计命中数；十二、hits_for 查询未知模式抛 sig.unknown_pattern，stats 返回每个模式的命中数，包含尚未命中的零值。

硬约束：不同流对象的并发 feed 互不影响，每个流各自维护偏移与尾巴，不能共用全局计数，同一流上的并发调用必须串行化，命中计数不得丢失或重复；模式集合、尾巴、偏移与计数必须能整体导出再恢复，恢复前后的命中逐条一致。验收：python3 -m pytest -q tests 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；流式与并发场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 漏洞检测与攻防
- type: Bugfix
- difficulty: 地狱
- module: signature_matcher
- typeName: SignatureMatcher
