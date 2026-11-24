# batch48-py-safety-a-20

## 题面

内部解析服务切换到分视图之后，办公网开始出现域名解析抖动：单标签主机名有时补上搜索域能解析，有时直接返回不存在，运维排查发现只有 NXDOMAIN 才应该继续下一个候选，NODATA 也要停住；别名链尾指向外部域时，解析器沿用了内部视图的缓存，把外部答案和内部答案混在一起；SOA 序号已经更新过两轮，负数缓存还按旧序号继续命中，删掉的记录在缓存过期前一直被答复。缓存键里也漏了视图、查询类型与 DNSSEC 状态，同一域名在不同视图之间互相命中。

范围是 gsb/batch48-py-safety-a-20 下的 dns_view_resolver.py，类 DnsViewResolver，负责视图与权威区声明、记录管理、搜索域候选展开、CNAME 链跟随、正负缓存与快照恢复。

公开接口：`DnsViewResolver(*, max_cname_depth=8, default_ttl=300, max_cache_entries=128, clock=None)`；`declare_view(view, *, ndots=1, search=(), external=None) -> dict`；`declare_zone(view, zone, *, serial=1, soa_ttl=300, soa_minimum=60) -> dict`；`add_record(view, zone, name, rtype, value, *, ttl=300) -> dict`；`set_serial(view, zone, serial) -> dict`；`zone_info(view, zone) -> dict`；`resolve(name, qtype, *, view="default", now=0, dnssec=False) -> dict`；`cache_entries() -> list`；`negative_cache() -> list`；`expire(*, now=0) -> dict`；另有 stats、reset_stats、snapshot、restore。拒绝统一抛 DnsError(code, detail)。

故障与边界：一、视图名、ndots（0 到 15）、搜索域都非法报 DNS_INVALID_VIEW，重复声明报 DNS_DUPLICATE_VIEW，区必须挂在已存在视图下，serial 与 SOA 的两个时间都取正整数，重复声明报 DNS_DUPLICATE_ZONE；二、记录类型只接受 A、AAAA、CNAME、TXT，其他报 DNS_UNSUPPORTED_TYPE，名字必须落在区内否则 DNS_OUT_OF_ZONE，值、TTL 非法报 DNS_INVALID_RECORD，完全相同的记录重复写入报 DNS_DUPLICATE_RECORD；三、名字先做小写与去尾点归一化，相对名按 ndots 与搜索域顺序展开候选，命中即停，qname 与 candidates_tried 要能看出用了哪个候选；四、区内存不存在该名字返回 NXDOMAIN，名字存在但查询类型没有记录返回 NOERROR 且 answers 为空、nodata 为真；五、CNAME 链按顺序跟到 max_cname_depth，超过上限返回 SERVFAIL 与 DNS_CNAME_DEPTH，成环返回 SERVFAIL 与 DNS_CNAME_LOOP，失败时 answers 必须为空；六、正缓存按答案里最小 TTL 衰减，remaining_ttl 随 now 递减，过期后重新查权威；七、负缓存 TTL 取 SOA MINIMUM 与 SOA TTL 的较小值，NXDOMAIN 与 NODATA 都要能命中并计入 negative_hits，expire 清掉到期条目并返回数量；八、缓存键必须含视图、查询类型与 DNSSEC 状态，视图之间不得互相命中；九、搜索域展开时只有 NXDOMAIN 才继续下一个候选，NOERROR/NODATA 必须停止；十、CNAME 链尾落在本视图无权限的域时按外部视图解析，不得沿用内部缓存；十一、负缓存条目在 SOA serial 变新后必须失效；十二、snapshot 是深拷贝，restore 结构非法报 DNS_SNAPSHOT_INVALID 且原状态不变。

硬约束：declare_view、declare_zone、add_record、resolve、set_serial、snapshot 全程持锁，四路并发解析不同名字时查询计数必须与调用次数一致，缓存写入不能互相覆盖或丢条目；缓存条目数受 max_cache_entries 硬顶，超限按到期时间淘汰而不是无限增长；TTL 与 SOA 时间全部用整数秒，过期判定用传入的 now，不依赖墙钟；快照恢复后视图、区与记录、正负缓存、SOA serial 与统计都要在，且不得与恢复前的对象共享结构。

验收：`python3 -m pytest -q tests` 的 15 条必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-safety-a-20
- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 困难
- module: dns_view_resolver
- typeName: DnsViewResolver
