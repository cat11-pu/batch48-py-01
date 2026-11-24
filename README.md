# batch48-py-safety-a-10

## 题面

出站 mTLS 跨租户复用，换证仍用旧连接。范围 gsb/batch48-py-safety-a-10 的 outbound_mtls_pool.py，类 OutboundMtlsPool，抛 MtlsPoolError(code, detail)。

接口：register_identity(identity_id, tenant, fingerprint, not_after)、waiters(target)、acquire(tenant, target, sni, identity_id, expected_subject, now, wait_ms)、release(lease_id, now, close)、rotate_identity(identity_id, new_fingerprint, now, overlap)、drain_target(target, now)。

故障：①身份按租户，码 MTLS_ 加 UNKNOWN_IDENTITY、IDENTITY_TENANT_MISMATCH、IDENTITY_CONFLICT、IDENTITY_EXPIRED；②键含 tenant、target、sni、指纹，跨租户与 SNI 不复用；③set_target_limit 拒非正，每目标硬顶，超限 MTLS_TARGET_SATURATED；④wait_ms 排队回 ticket 与 deadline，release(close) FIFO 提升，expire_waiters 清超时；⑤release 首次 True、重复 False，空闲复用计 reused；⑥rotate 换指纹，旧连接 draining，末次释放 closed，新请求用新指纹；⑦drain_target 关空闲、在途转 draining，返回条数，重复 0；⑧register_peer 登记主体，不符即废弃不入池；⑨同租户灰度两套证书时键区分指纹；⑩排空期旧连接失败不重投新连接。

硬约束：并发 acquire 同键只建一条；snapshot 深拷贝含 connections、stats，restore 可用，非法结构抛 MTLS_SNAPSHOT_INVALID。

验收：python3 -m pytest -q tests 15 条过；不改测试、不跳过、不写死；并发自验。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- module: outbound_mtls_pool
- typeName: OutboundMtlsPool
