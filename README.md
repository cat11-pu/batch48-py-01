# batch48-py-safety-c-03

## 题面

对象存储的上传网关连续被压缩炸弹打穿：有人上传一个不到两兆的压缩包，解压后写满临时盘；嵌套压缩包只看外层体积就放行；条目数超过配额后网关静默截断，前端的清单与实际落盘对不上；带 .. 的条目路径能写到工作目录之外；解压中途磁盘写满时留下半成品，失败的上传也没有关闭输入。安全组复盘时发现，网关只在整包上传完成后才检查体积，流式解压期间完全没有可用的进度账本。范围是 gsb/batch48-py-safety-c-03 下的 archive_bomb_guard.py，类 ArchiveBombGuard，负责在流式解压过程中逐条目审计体积、层数、条数与路径。

上传链路一边解压一边把事件交给守卫，申报条目时给出的压缩大小来自包头，守卫只负责审计，不负责真正的解压与落盘：open(stream_id) 建立会话，declare_entry(stream_id, entry) 申报条目，条目是含 name、compressed_size、nesting 的映射；consume(stream_id, entry_name, chunk) 投喂该条目已解出的字节。公开接口还有 limits()、nesting_depth(stream_id)、ratio(stream_id)、verdict(stream_id)、close(stream_id)、snapshot()、restore(state)、stats()。limits 支持 max_entries、max_nesting、max_entry_expanded、max_total_expanded、max_ratio、ratio_floor_bytes 六个键，构造时可收窄默认值。所有拒绝统一抛 ArchiveBombError(code, detail)，code 取 bomb.limits_invalid、bomb.unknown_stream、bomb.duplicate_stream、bomb.session_closed、bomb.entry_limit、bomb.entry_expanded、bomb.total_expanded、bomb.ratio、bomb.nesting、bomb.path、bomb.duplicate_entry、bomb.entry_unknown 与 bomb.snapshot_invalid。open 返回 stream_id 与本次生效的 limits 拷贝；declare_entry 返回 allowed、name、entries、nesting、compressed；verdict 返回 status、code、entries、expanded、compressed，status 取 open、ok、rejected；limits() 返回合并后的完整限制表；declare_entry 里的 entries 与 compressed 是累计值；consume 返回 entry、entry_expanded、entry_compressed、total_expanded、total_compressed、ratio，其中 entry_compressed 是该条目申报的压缩大小，total_compressed 是所有已申报条目的累计；模块常量 DEFAULT_LIMITS 给出六个键的默认值；stats 的 entries 与 compressed 按已申报条目累计。

故障与边界：一、条目名为空、绝对路径、带盘符或含 .. 段时必须在读取内容前拒绝，且不消耗条目配额；二、嵌套层数按申报值累计，超过 max_nesting 立即拒绝；三、单条目展开字节超过 max_entry_expanded 时中止，超限的这块不计入统计；四、所有条目展开字节之和超过 max_total_expanded 时中止；五、膨胀比按累计展开字节除以累计申报压缩字节计算，压缩量低于 ratio_floor_bytes 时不判，内层解压出的字节同样计入总膨胀与总膨胀比，不能只统计外层；六、条目数达到 max_entries 后再申报必须报错，不允许静默截断；七、同名条目重复申报抛 bomb.duplicate_entry；八、任何拒绝（含路径、层数、条数、重复名与各类膨胀超限）都要失败关闭：会话转为 rejected、记录错误码、关闭输入，失败后不得再接收后续块，后续 consume 或 declare 抛 bomb.session_closed；九、close 幂等并返回最终结论，未知会话抛 bomb.unknown_stream，同一标识重复 open 抛 bomb.duplicate_stream；十、snapshot 返回可 JSON 序列化的深拷贝，restore 后计数与限制继续生效，结构非法抛 bomb.snapshot_invalid 且当前状态不变；十一、并发检查不同会话时计数与限额互不串账，每个会话各自累计；十二、stats 汇总 streams、entries、expanded、compressed、rejected，其中 rejected 统计被拒绝的会话数。

硬约束：任何超限必须立即中止而不是事后补判，已接收的字节不得回滚成静默通过；快照跨进程恢复后限额仍然按恢复前的计数生效；并发场景下同一会话的申报与消费串行化且计数以提交顺序为准，不同会话之间互不影响。验收：python3 -m pytest -q tests 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；嵌套与并发场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 漏洞检测与攻防
- type: Bugfix
- difficulty: 地狱
- module: archive_bomb_guard
- typeName: ArchiveBombGuard
