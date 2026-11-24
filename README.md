# batch48-py-safety-b-05

## 题面

批处理节点上调用外部转换器，子进程一多就整批卡死，日志停在半行不动；值班强杀了任务，宿主机上还挂着转换器拉起来的孙进程；下游反馈库里出现半个汉字，长度对不上原文。这些转换器有的先写错误流再写结果，有的把整段文本一次性打出来，两条管道谁先写满谁先卡；执行记录里只看得到主进程号，看不出进程组里还剩下谁。范围是 gsb/batch48-py-safety-b-05 下的 subprocess_io_guard.py，类 SubprocessIoGuard，负责为受控子进程隔离标准输入输出、限制缓冲、按字节读取并在超时后回收整个进程组。

公开接口：SubprocessIoGuard(*, max_handles=32)；spawn(argv, limits=None)；write_stdin(handle, data)；close_stdin(handle)；read_stdout(handle, max_bytes)；read_stderr(handle, max_bytes)；read_lines(handle)；drain(handle)；wait(handle, timeout)；kill_tree(handle)；close(handle)；handles()；snapshot()；restore(state)。上限用 IoLimits(max_buffer, line_limit, graceful_seconds) 承载，缺省为 65536、4096、1.0；拒绝统一抛 IoGuardError(code, detail)。句柄号以 proc- 开头，handles 按升序返回仍在使用的句柄。

故障与边界：一、spawn 校验 argv 是非空字符串列表、max_buffer 与 line_limit 为正、graceful_seconds 非负，非法分别报 IO_INVALID_ARGV 与 IO_INVALID_LIMITS；二、stdout 与 stderr 各自独立读取，任一管道没人读都不能让子进程写阻塞；三、read_stdout 与 read_stderr 的上限按字节计，允许把多字节字符切开，读取上限非正报 IO_INVALID_LIMIT，进程未退出时最多等待 graceful_seconds 到有数据或管道结束，进程已退出时返回缓冲里剩余的内容；四、write_stdin 返回写入字节数，关闭后再写报 IO_STDIN_CLOSED，积压超过 max_buffer 报 IO_STDIN_BACKPRESSURE；五、wait 超时抛 IO_WAIT_TIMEOUT 且不杀进程，wait 归零前要把两个管道读到结尾并落入缓冲，返回退出码，被信号杀死返回负信号号，两者不得混淆；六、kill_tree 对运行中的进程组发信号并返回 killed，进程已退出返回 already-exited 且不抛异常，重复调用同样幂等；七、超时终止要先关闭 stdin 让子进程有机会自行退出，再升级到对整个进程组发信号；八、read_lines 只交付完整行，行缓冲超过 line_limit 报 IO_LINE_TOO_LONG，无换行输出不得把缓冲撑爆；九、stderr 合并进同一管道时，行切分不能把半行当成完整行交付；十、close 幂等，关闭后 read_stdout、drain、write_stdin 报 IO_HANDLE_CLOSED，未知句柄报 IO_UNKNOWN_HANDLE；十一、多个读者并发读取同一句柄不得重复交付同一段输出，关闭中的读取也要有确定结果；十二、快照保存句柄状态、进程号、已读偏移与剩余超时，非法结构报 IO_SNAPSHOT_INVALID 且原状态不变，重启后遗留的进程组必须被回收。

硬约束：读取、关闭与进程组终止全程持锁，同一段输出只能交付一次，关闭之后不能再出现半条数据；快照必须是深拷贝，恢复后不与调用方共享可变结构；单次读取不超过调用方给的字节上限，缓冲与行长都受硬顶，超限先报错再谈清理。

验收：`python3 -m pytest -q tests` 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；管道背压、超时终止与重启回收场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- module: subprocess_io_guard
- typeName: SubprocessIoGuard
