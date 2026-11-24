# batch48-py-safety-d-09

## 题面

分析团队拿到线上抓包之后，安全审计直接亮了红灯：Authorization 头原样躺在 pcap 文件里，Cookie 和 Redis 的 AUTH 口令也能直接读出来；更糟的是仓促上线的脱敏脚本把包长字段改小了，从第一个被替换的包之后全部解析错位，分析同事花了两天才把包对齐。访问侧同样有问题：谁在什么时候读了哪个区间没有留痕，保留期一到脚本直接删文件，删完才发现摘要对不上，已经没法追溯。运维还发现轮转策略形同虚设，单个文件涨到十几吉字节，出问题只能整份拷贝，审计要求每个文件都能说清来源和去向。

范围是 gsb/batch48-py-safety-d-09 下的 pcap_redaction_guard.py，类 PcapRedactionGuard，负责抓包文件落盘前脱敏、长度字段一致性、文件轮转、保留期清理与读取审计。

对外方法：`PcapRedactionGuard(*, rotation_seconds: int = 3600, rotation_bytes: int = 64 * 1024 * 1024, retention_seconds: int = 7 * 24 * 3600, max_sessions: int = 2048, clock=None)`；`authorize_reader(requester: str) -> None`；`begin_file(path: str, *, now=None) -> dict`；`append_packet(file_id: str, packet: dict, *, now=None) -> dict`；`redact_packet(packet: dict) -> dict`；`detect_credentials(payload: bytes, *, protocol: str) -> list[dict]`；`close_file(file_id: str, *, now=None) -> dict`；`maybe_rotate(*, now=None) -> dict | None`；`rotate(*, now=None) -> dict`；`open_for_read(file_id: str, *, requester: str, start: int = 0, end: int | None = None, now=None) -> dict`；`audit_log() -> list[dict]`；`purge_expired(*, now=None) -> list[str]`；`snapshot() -> dict`；`restore(state: dict) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 PcapGuardError(code, detail)。

故障与边界：一、协议识别只覆盖 http、redis、smtp，其余协议原样放行；HTTP 识别 authorization、proxy-authorization、cookie 三类头部，Redis 识别 AUTH 命令记为 auth，SMTP 识别 AUTH PLAIN 记为 auth_plain；二、凭据替换必须等长，替换后帧长、IP 总长、TCP 载荷长度三个字段必须与替换前一致，出现偏差抛 PCAP_LENGTH_MISMATCH；三、重算校验和只覆盖传输层，链路层校验和与原始帧保持一致；四、detect_credentials 返回字段名与精确覆盖凭据值的 start、end，不含字段名和认证方案，多条命中按在载荷中出现的先后顺序返回；五、没有凭据的载荷原样返回、redactions 为 0，未知协议不产生任何改写；六、脱敏结果与原包解耦，改动返回值不得影响调用方传入的包；七、文件关闭时计算 sha256 摘要、包数与字节区间，字节区间按帧长累加，关闭后继续追加抛 PCAP_FILE_CLOSED；八、未知文件标识抛 PCAP_UNKNOWN_FILE，读取区间超出文件范围抛 PCAP_REQUEST_OUT_OF_RANGE；九、读取必须由已授权请求者发起，未授权抛 PCAP_UNAUTHORIZED 且不写审计；十、每次授权读取写一条审计，记录请求者、文件与读取区间，保留期删除前先校验摘要，对不上拒绝删除；十一、文件按大小或时长轮转，未到阈值 maybe_rotate 返回 None，轮转后旧文件关闭、新文件从零计数；十二、快照覆盖文件索引、会话与授权名单，恢复后仍可读取，结构非法抛 PCAP_SNAPSHOT_INVALID 且原状态不变。

接口返回约定：file_id 形如 cap-1，按文件创建顺序递增；begin_file 与 close_file 返回 file_id、path、packet_count、byte_range、digest、closed 与 redactions，未关闭时 digest 为 None；append_packet 返回 file_id、index 与 redactions；redact_packet 返回 packet 与 redactions；open_for_read 返回 file_id、bytes_read 与 range；maybe_rotate 未到阈值返回 None，轮转时返回新文件记录。

硬约束：追加与读取全程持锁，同一文件的并发读写不得出现半包或错位；会话表与文件索引有上限，禁止无界增长；snapshot 必须是深拷贝，恢复后的对象不得与原状态共享可变结构；stats 固定返回 files、packets、redactions、bytes 四个整数，reset_stats 只清计数不清文件索引。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 地狱
- module: pcap_redaction_guard
- typeName: PcapRedactionGuard
