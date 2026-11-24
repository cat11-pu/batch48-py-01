# batch48-py-safety-b-03

## 题面

下载网关这两周接连出单：客户拿到的签名链接只该读自己那份报表，改几个字符却能拉到邻居租户的文件；同一条一次性链接被脚本并发重放，审计里留下两条成功记录；还有请求带着越界的 Range 头，把不该给的那段内容取了出来。范围是 gsb/batch48-py-safety-b-03 下的 download_grant_guard.py，类 DownloadGrantGuard，负责按资源签发短期授权、判定请求、一次性兑换、撤销与过期清理。

公开接口：DownloadGrantGuard(*, max_grants=1000)；issue(resource_id, subject, ttl, now, *, grant_id=None)；authorize(grant, request_path, method, now)；consume(grant_id, now)；revoke(resource_id, now=0)；purge(now)；inspect(grant_id)；grants()；watermark(resource_id)；check_range(grant, header, size, now)；snapshot()；restore(state)。判定结果用 GrantDecision(allowed, reason, expires_at) 返回，拒绝不抛异常；参数或状态错误统一抛 DownloadError(code, detail)。

故障与边界：一、issue 校验资源号、主体、ttl 与 now，资源号不合规报 DOWNLOAD_INVALID_RESOURCE，主体为空报 DOWNLOAD_INVALID_SUBJECT，ttl 非正整数报 DOWNLOAD_INVALID_TTL，签发总数受 max_grants 硬顶，超限报 DOWNLOAD_GRANT_LIMIT 且不落表，返回值含 grant_id（32 位小写十六进制）、resource_id、subject、root、issued_at、expires_at、consumed；二、authorize 只认签发表里的记录，调用方手里那份副本的字段不参与判定，未知 grant_id 报 DOWNLOAD_UNKNOWN_GRANT，结构缺失报 DOWNLOAD_INVALID_GRANT，判定结果按 allowed、reason、expires_at 三字段返回；三、方法只允许 GET 与 HEAD，其余一律 method-not-allowed；四、路径先做百分号解码再按段归一，点段折叠、双点回退，落到资源根之外给 path-outside-resource；五、空路径、含 NUL、段尾带点或带空格的路径给 path-invalid，资源号按大小写敏感比较，路径里的资源段大小写不同按根外处理；六、now 恰好等于 expires_at 即过期，给 expired，早一秒仍然放行；七、consume 只能成功一次，重复兑换报 DOWNLOAD_TOKEN_REPLAYED，未知授权报 DOWNLOAD_UNKNOWN_GRANT，已过期报 DOWNLOAD_GRANT_EXPIRED，消费之后 authorize 返回 consumed；八、revoke 按资源撤销水位之前的全部在途授权并返回条数，重复撤销返回 0，水位之前签发的授权立即失效，之后重新签发的不受影响；九、purge 只删已过期授权并返回条数，未过期的仍然可判定；十、check_range 只接受 bytes 单段，起始位置不小于文件长度、或结束位置小于起始位置给 range-unsatisfiable，结束位置超过文件末尾按末尾截断，写法不合法给 range-invalid，多段请求里只要有一段越界，整份请求就要拒绝，不能只丢掉越界段把其余内容发出去；十一、百分号编码的大小写混写与重复编码要在同一次归一里收敛，只解一层会留下可绕过路径；十二、快照保存授权表、撤销水位与观测时刻，已消费标记随快照保留，重启后已消费令牌不得复活，非法结构报 DOWNLOAD_SNAPSHOT_INVALID 且原状态不变。

硬约束：同一一次性令牌在并发兑换下只能有一个成功者，其余全部按重放拒绝，判定与落账在同一把锁内完成；快照必须是深拷贝，恢复后不与调用方共享可变结构；签发总量受 max_grants 硬顶，超过后先报错再谈清理。

验收：`python3 -m pytest -q tests` 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；并发兑换与重启后失效场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- module: download_grant_guard
- typeName: DownloadGrantGuard
