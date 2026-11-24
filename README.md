# batch48-py-safety-c-09

## 题面

固件比对从整文件哈希改成函数级匹配后，误报和漏报一起爆发：同一份源码换一个编译器，所有函数指纹全部失配；几行长的短函数到处命中，一次扫描能报出几百个无关固件；把带版本信息的已知脆弱函数加进索引后，同一函数的多个库版本命中顺序每次都不一样，值班同学照着第一条去升级，升完发现还是旧版本；索引跑一小时内存涨到十几 GB，进程被系统杀掉，重启后索引要整份重建；临时删掉几条低价值特征，结果把真正要用的长函数一起删了。范围是 gsb/batch48-py-safety-c-09 的 binary_fingerprint_index.py，类 BinaryFingerprintIndex，负责从二进制里抽取函数特征、建索引、对未知输入做相似度匹配并维护已知脆弱函数标注。

公开接口：BinaryFingerprintIndex(*, max_functions=50000, max_function_bytes=1048576, max_input_bytes=67108864)；add_function(name, code, strings) -> dict；match(blob, threshold=0.5) -> list[dict]；explain(match_id) -> dict；prune(threshold) -> int；annotate(name, library, version) -> dict；versions(name) -> list[dict]；features(name) -> dict；functions() -> list[str]；stats() -> dict；snapshot() -> dict；restore(state) -> None。拒绝统一抛 FingerprintError(code, detail)。

故障与边界：①特征由 n 取 4 到 8 的字节 n-gram 集合、长度不小于 4 的字符串常量集合与代码长度分桶组成，features 返回 n-gram 数、升序字符串、长度与长度分桶，重名抛 FP_DUPLICATE_FUNCTION，参数类型不对或代码为空抛 FP_INVALID_INPUT；②单函数字节超过 max_function_bytes、索引函数数超过 max_functions、单次 match 输入超过 max_input_bytes 分别抛 FP_FUNCTION_TOO_LARGE、FP_FUNCTION_LIMIT、FP_INPUT_TOO_LARGE，且不改索引；③相似度为加权 Jaccard，n-gram 交集除以并集占八成，字符串集合的 Jaccard 占两成，两边字符串集合完全一致且非空时再加成并按 1.0 封顶；④长度差超过 30% 的函数即便 n-gram 重合很高也必须排除；⑤match 结果带 match_id、function、similarity、shared_strings，按相似度降序、函数名升序稳定排列，threshold 只接受 (0, 1]，越界或非数值抛 FP_INVALID_THRESHOLD；⑥explain 按 match_id 返回函数名、共享字符串、共享 n-gram 数与长度比，未知 id 抛 FP_UNKNOWN_MATCH，空索引 match 返回空列表；⑦字符串常量完全一致但字节相似度略低于 threshold 时仍要进入候选；⑧同一函数多版本命中时高版本排在前面，同版本按库名排序；⑨annotate 给已知脆弱函数登记库名与版本，版本必须是三段数字，未知函数抛 FP_UNKNOWN_FUNCTION，versions 按库名升序返回；⑩prune 删除特征总数低于 threshold 的函数并返回删除个数，特征总数是 n-gram 集合大小加字符串常量个数，threshold 为负抛 FP_INVALID_THRESHOLD；⑪快照序列化后恢复，match 结果、标注与特征完全一致，结构非法抛 FP_SNAPSHOT_INVALID 且原索引不动；⑫同一 name 只允许登记一次，标注与特征绑定在同一条记录上，删除特征时不得留下孤立标注。

硬约束：并发 add_function 与 match 不得看到半更新索引，读方拿到的结果要么是旧索引要么是新索引；匹配只对通过长度过滤的函数打分，排序不依赖字典遍历顺序；索引与标注可整体导出，恢复后同一输入的匹配结果逐项相同，导出结构不得与内部结构共享可变容器。stats 固定返回 functions、total_features、matches、pruned、annotations 五个整数。

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
- module: binary_fingerprint_index
- typeName: BinaryFingerprintIndex
