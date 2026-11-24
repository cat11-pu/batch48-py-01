# batch48-py-safety-a-29

## 题面

前置分流网关换到按 ClientHello 选集群之后，一线报了几类问题：某业务把域名挪到子域后，通配没命中，请求被送进兜底集群，证书全部对不上；ECH 已启用的站点，内层名字解析失败时网关当成没有 SNI，把流量送进默认集群；灰度换版本的时候，同一次连接的重试被分到新旧两个集群，会话状态对不上；客户端只给了一个谁都不支持的 ALPN，网关却挑了列表里第一个硬塞过去，对端直接断开；桶映射改版之后无法回滚核对，出问题只能重启；解析器遇到截断的采集记录没有拒绝，把半截字节当成合法 SNI 用了。范围是 gsb/batch48-py-safety-a-29 下的 clienthello_router.py，类 ClientHelloRouter，负责解析采集到的 ClientHello、按 SNI 与 ALPN 选集群、灰度分桶与桶映射版本。

公开接口：ClientHelloRouter(*, bucket_count: int = 64, grayscale_percent: int = 10, supported_alpn: tuple = ("h2", "http/1.1"), salt: bytes = b"", gray_cluster: str = "canary")；parse_hello(data: bytes) -> dict；add_cluster(name: str, *, snis=(), fallback: bool = False) -> dict；list_clusters() -> list；bucket_for(client_ip: str) -> int；set_bucket_map(mapping: dict, *, version: int) -> dict；route(client_ip: str, sni, alpn, *, conn_id=None) -> dict；snapshot() -> dict；restore(state: dict) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 ClientHelloError(code)，code 取 CH_MALFORMED、CH_INVALID_ARGUMENT、CH_DUPLICATE_CLUSTER、CH_FALLBACK_EXISTS、CH_UNKNOWN_CLUSTER、CH_INVALID_BUCKET、CH_INVALID_VERSION、CH_UNKNOWN_SNI、CH_SNAPSHOT_INVALID。

要修的点：一、采集记录格式固定为 3 字节魔数 CH1、1 字节 flags（bit0 表示带 ECH）、u16 SNI 长度加 UTF-8 内容、带 ECH 时再跟 u16 outer 长度加内容与 u16 inner 长度加内容、u8 ALPN 数量与逐个 u8 长度加 ASCII、u8 支持组数量与逐个 u16 组号、最后 u16 legacy 版本，全部大端；魔数不符、长度越界、尾部多余字节都抛 CH_MALFORMED；二、parse_hello 返回 sni、ech、ech_outer_sni、ech_inner_sni、alpn、groups、tls_version 七项，无 ECH 时 outer 与 inner 为 None，有 ECH 且 inner 非空时 sni 取 inner，groups 按原顺序保留，解析成功计入 parsed；三、add_cluster 返回 name、snis、fallback，snis 去重后按字典序排列，重名抛 CH_DUPLICATE_CLUSTER，重复登记兜底集群抛 CH_FALLBACK_EXISTS，空名抛 CH_INVALID_ARGUMENT，list_clusters 按集群名升序；四、bucket_for 对 salt 与客户端地址拼成的字节串做 SHA-256 后对 bucket_count 取模，同一输入结果稳定、落在 0 到 bucket_count 减 1 之间，salt 不同结果不同；五、灰度阈值等于 bucket_count 乘 grayscale_percent 再整除 100，桶号小于阈值为灰度流量，初始桶映射为空、版本为 1，灰度桶默认落 gray_cluster，桶映射里显式指定时用映射值，非灰度桶按 SNI 选集群；六、set_bucket_map 返回 bucket_count、version、gray_cutoff、mapped 四项，version 必须为正整数，桶号越界抛 CH_INVALID_BUCKET，映射值不是已登记集群抛 CH_UNKNOWN_CLUSTER，版本非正抛 CH_INVALID_VERSION，失败时不得改动现有映射；七、route 返回 status、cluster、bucket、bucket_version、alpn、fallback、gray 七项，SNI 精确匹配优先于通配，通配 *.example.com 只匹配子域不匹配裸域，未知 SNI 一律落兜底集群并记 fallback，没有兜底集群抛 CH_UNKNOWN_SNI 并计入 rejected；八、ALPN 按客户端顺序取第一个服务端也支持的，全部不支持时返回 status 为 no_application_protocol，ECH 存在但 inner 解析失败时要按 outer SNI 与 ECH 拒绝策略处理，不能退化成无 SNI 走默认集群；九、route 带 conn_id 时，同一次连接内的重试要固定建连时刻的桶版本，映射换版本后重试仍按旧版本落同一集群；十、快照固定带 version=1、clusters、bucket_map、bucket_version、stats 五个键，clusters 以集群名为键，必须是深拷贝，restore 后集群表、桶映射、版本与统计都回来，结构非法抛 CH_SNAPSHOT_INVALID 且原状态不变，stats 固定返回 parsed、routed、gray、fallback、rejected 五个整数，parsed 计解析成功、routed 计成功路由、gray 计灰度命中、fallback 计兜底命中、rejected 按压错次数累加，reset_stats 全部归零。

硬约束：parse_hello、add_cluster、set_bucket_map、route、snapshot、restore 全程持同一把逻辑锁，六路并发路由时 routed 必须等于成功调用次数，桶映射换版本期间不得出现半新半旧的读；快照跨进程恢复后桶版本与集群表原样继续；灰度阈值与桶映射受 bucket_count 硬约束，桶号越界一律拒绝。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 地狱
- module: clienthello_router
- typeName: ClientHelloRouter
