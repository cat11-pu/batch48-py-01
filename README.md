# batch48-py-safety-c-04

## 题面

补丁核对从人工比对改成自动判定之后，同一个提交在两台机器上得出了相反结论：行尾空白稍有不同就被判成失败，反向补丁也被当成已经合入，hunk 乱序时前半段被应用、后半段被忽略，上下文已经缺失的补丁仍然显示可应用，换个目标文件后缓存也没有失效。补丁来自多个安全修复分支，路径前缀与行号经常对不齐，人工核对一天要看几十份。范围是 gsb/batch48-py-safety-c-04 下的 patch_diff_analyzer.py，类 PatchDiffAnalyzer，负责解析统一差分、按上下文模糊定位，并判定补丁相对目标文件是已合入、可应用还是冲突。

统一差分由旧路径、新路径与若干 hunk 组成，hunk 头部形如 @@ -a,b +c,d @@，正文用空格、减号、加号标记上下文、删除与新增行。公开接口：PatchDiffAnalyzer(*, max_sections=512, max_hunk_lines=4096, context_tolerance=2, whitespace_tolerance=1)；limits()；parse(text)；locate(file_text, hunks)；classify(file_text, patch_text)；explain(result)；invert(patch_text)；cache_info()；clear_cache()；stats()；reset_stats()。parse 返回文件段列表，每段含 old_path、new_path、hunks；hunk 含 index、old_start、old_count、new_start、new_count、header、lines，lines 的每条含 kind 与 text；locate 返回每个 hunk 的 index 与 0 基 position。所有拒绝统一抛 PatchError(code, detail)，code 取 diff.syntax、diff.count_mismatch、diff.hunk_order、diff.hunk_overlap、diff.section_limit、diff.hunk_limit、diff.locate_conflict。classify 的 sections 每段含 old_path、new_path、verdict、hunks，hunks 每条含 index、verdict、code；detail 含 line、reason 或 hunk，行数不符时行号指向该 hunk 的头部行；reset_stats 把三个计数归零；cache_info 返回 entries、hits、misses，每次 classify 记一次命中或未命中；stats 返回 classify_calls、parse_calls、locate_calls。

故障与边界：一、hunk 必须按新文件行号严格升序且区间不重叠，乱序或重叠时整份拒绝，detail 给出出错行号与原因；二、每个 hunk 的旧侧、新侧行数必须与头部计数一致，不符抛 diff.count_mismatch；三、单补丁文件段上限 512、单个 hunk 行数上限 4096，超限报错且不改动已有缓存；四、locate 用上下文做模糊匹配算出位置，后一个 hunk 从前一个位置之后继续搜索，定位不到抛 diff.locate_conflict 并在 detail 给出 hunk 索引；五、同一 hunk 内不匹配的上下文或删除行超过 context_tolerance 行即判冲突，行尾空白相差一个字符以内视为匹配，超过容差计入不匹配行数；六、already_applied 只看删除行在目标文件中是否还存在，新增行缺失本身不足以单独判为已合入；七、反向补丁的删除行正是原补丁的新增行，这些行存在时必须判 applicable 而不是 already_applied；八、任一 hunk 冲突则整份 conflict，不得部分应用或只报告成功的一半；九、classify 结果含 verdict、sections、file_digest、patch_digest，摘要用 sha256 十六进制，每个 hunk 也带 verdict 与依据码，结论可以直接回填到工单；十、同一输入两次 classify 结论与摘要完全一致，缓存键同时含文件摘要与补丁摘要，clear_cache 清空条目与命中计数；十一、explain 逐 hunk 列出 section、hunk、verdict 与依据码，依据码取 patch.removed_present、patch.removed_absent、patch.context_missing；十二、invert 交换新旧路径与增删行，供人工复核反向补丁。

硬约束：缓存读写必须原子，并发 classify 只能看到完整的缓存条目，命中、未命中计数不得丢失或重复；解析与定位不得依赖全局可变状态，同一份输入任意并发调用结论一致；解释信息必须能由返回结果直接推导，不能另开内部查询。验收：python3 -m pytest -q tests 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；并发场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 漏洞检测与攻防
- type: Bugfix
- difficulty: 地狱
- module: patch_diff_analyzer
- typeName: PatchDiffAnalyzer
