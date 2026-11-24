# batch48-py-safety-d-04

## 题面

设备批量返修之后，同一块主板换壳重新注册拿到了第二张有效证书，旧证书还在签发名单里，运维只能逐台人工核对；客服又发现一张已经过期的注册码还能被兑换，而返修换板的设备换了个指纹，系统直接把新指纹盖在旧绑定上，没有任何重新注册的记录。范围是 gsb/batch48-py-safety-d-04 下的 device_enrollment_guard.py，类 DeviceEnrollmentGuard，负责注册码签发、一次性兑换、设备证书签发、吊销与快照。

公开接口：DeviceEnrollmentGuard(*, cert_ttl=86400, max_codes=1000, max_certs=1000)；issue_code(tenant, device_type, *, code=None, expires_at=None) -> dict；enroll(code, fingerprint, *, device_type=None, now=0) -> dict；lookup(serial) -> dict；verify(serial, *, now=0) -> str；revoke(serial, reason="") -> bool；is_revoked(serial) -> bool；certificates(*, tenant=None) -> list[dict]；codes() -> list[dict]；fingerprint_digest(fingerprint) -> str；snapshot() -> dict；restore(state) -> None；stats() -> dict。拒绝统一抛 EnrollmentError(code, detail)。

故障与边界：

一、注册码是四段四位大写十六进制、段间带连字符；也可显式指定注册码，重复报 ENR_CODE_DUPLICATE，非法格式或空的租户、设备类型报 ENR_INPUT_INVALID；注册码默认不过期，只有显式给出 expires_at 才按时间失效；codes 按签发顺序返回 code、tenant、device_type、expires_at 与 state。

二、enroll 校验注册码归属与设备类型：传入的 device_type 与注册码不一致报 ENR_TYPE_MISMATCH；未知注册码报 ENR_CODE_UNKNOWN；指纹为空或短于 8 个字符报 ENR_FINGERPRINT_INVALID。

三、过期即废：now 达到 expires_at 报 ENR_CODE_EXPIRED，注册码不被消耗，登记表里仍是 pending。

四、注册码用后即废：注册码已被兑换后换一个指纹再兑，报 ENR_CODE_USED。

五、同一注册码加同一指纹重复兑换要幂等，返回同一序列号并标 replayed 为真，证书内容与首次一致，不新增证书。

六、序列号从 DEV-000001 起递增；证书含 tenant、device_type、fingerprint_digest、issued_at、not_after 与 state，issued_at 取实际兑换时刻，not_after 等于兑换时刻加 cert_ttl；指纹只存 sha256 摘要，不存原文。

七、verify 按序列号返回 valid、expired、revoked 三种状态，未知序列号报 ENR_CERT_UNKNOWN。

八、revoke 首次返回真、再次返回假；吊销不可逆，lookup 仍能查到证书状态与吊销原因；is_revoked 对未知序列号返回假。

九、certificates 可按租户过滤，不传租户时返回全部证书，按序列号升序返回深拷贝；证书数达到 max_certs 时报 ENR_LIMIT_EXCEEDED，注册码保持未使用。

十、同一设备用不同注册码重复注册要返回同一序列号，不签发第二张证书。

十一、硬件指纹变化必须走重新注册并作废旧证书，不能直接把新指纹覆盖到旧绑定上。

十二、吊销表在快照恢复后仍拒绝该设备，且不能被新注册码自动解除；快照含 version、codes、certs 与 next_serial，结构非法报 ENR_SNAPSHOT_INVALID 且原状态不动。

硬约束：并发兑换同一注册码只能成功一个，其余拿到 ENR_CODE_USED，登记表里只留一条绑定记录；快照与恢复跨进程后注册码状态、证书与吊销结果一致；注册码数、证书数与序列号都是硬顶，失败兑换不占用序列号，序列号不回退也不复用；注册码表与证书表只保留指纹摘要，不保留指纹原文。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；幂等兑换与吊销后重注册自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- difficulty: 困难
- module: device_enrollment_guard
- file: device_enrollment_guard.py
