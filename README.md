# batch48-py-safety-d-01

## 题面

租户凭据保险库上线三周，KEK 轮换当天六个租户的历史数据库口令突然解不开，紧急回滚密钥版本才恢复；同一周安全评审又发现一批凭据在 AAD 填错时照样返回明文，事后审计里还查不出是哪一次取用漏了校验。范围是 gsb/batch48-py-safety-d-01 下的 credential_seal_vault.py，类 CredentialSealVault，负责租户凭据入库、取用、版本保留与 KEK 轮换。

公开接口：CredentialSealVault(*, audit_limit=64, keep_versions=3, batch_limit=32)；add_kek(version, key) -> dict；active_kek_version() -> int；revoke_kek(version) -> bool；rotate_kek(version, key, *, rewrap_limit=None) -> dict；put_secret(tenant, name, plaintext, *, aad="") -> dict；get_secret(tenant, name, *, aad="", version=None) -> bytes；versions(tenant, name) -> list[int]；export_record(tenant, name, *, version=None) -> dict；import_record(record, *, replace=False) -> dict；rewrap_batch(tenant, batch_size) -> int；audit_log() -> list[dict]；snapshot() -> dict；restore(state) -> None；stats() -> dict。拒绝统一抛 CredentialVaultError(code, detail)。

故障与边界：

一、每条凭据用独立 DEK 加密，落库只保存包裹后的 DEK 与密文；put_secret 返回 tenant、name、version、kek_version 与 ciphertext，密文长度与明文一致且不得等于明文。

二、首次登记的 KEK 成为生效版本；同一版本重复登记报 SEAL_KEK_DUPLICATE；版本不是正整数、密钥短于 16 字节报 SEAL_INPUT_INVALID；没有生效 KEK 时写入报 SEAL_KEK_MISSING，登记表里缺失或已撤销的 KEK 用于解封时报同一个码；撤销已登记版本返回真，重复撤销返回假，撤销从未登记的版本同样报 SEAL_KEK_MISSING。

三、解封失败分三类：AAD 摘要不符报 SEAL_AAD_MISMATCH，密文被篡改报 SEAL_TAMPERED，密钥版本缺失报 SEAL_KEK_MISSING；判定顺序先比 AAD 摘要，再验密文完整性与解密。

四、rotate_kek 只重包裹 DEK，密文一个字节都不许动，返回 kek_version 与 rewrapped；rewrap_limit 限定本次迁移条数，None 表示全部，迁移按 tenant、name、版本升序推进。

五、rewrap_batch 只迁一个租户，每批条数在 1 到 batch_limit 之间，越界报 SEAL_BATCH_INVALID，没有待迁移记录返回 0。

六、同名凭据重复写入递增版本，get_secret 默认取最新版本，指定不存在或已被淘汰的版本报 SEAL_VERSION_GONE；versions 对未知凭据返回空列表。

七、stats 返回 secrets（逻辑凭据数）、versions（版本总数）、kek_versions 与 audit 四个计数。

八、审计按条数上限淘汰最旧的一条，取用失败也要记一条并把错误码写进 code，条目形如 op、tenant、name、code，op 取 put、get、rotate、rewrap、revoke 之一。

九、KEK 轮换有双活窗口，旧版本凭据在窗口内仍可解封，新写入只能落生效版本 KEK，不得回落到旧版本；轮换中途失败要能整体退回到轮换前的登记表。

十、同名凭据覆盖超出 keep_versions 时淘汰最旧版本，被淘汰版本解封必须拿到明确的不可用码。

十一、快照包含 version、active_kek、keks、secrets 与 audit；恢复后审计上限与版本保留数仍要生效，快照与运行状态里不得出现明文或裸 DEK，已淘汰的版本不得因恢复而复活。

十二、export_record 与 import_record 用于迁移，结构缺字段报 SEAL_RECORD_INVALID，导入时只校验结构不做解密，replace=False 且版本已存在也报同一码。

硬约束：put_secret、get_secret 与 rotate_kek 并发调用必须串行一致，读方不得看到半包裹的 DEK；快照与恢复跨进程后仍能解封，同一份快照连续恢复两次结果一致，且恢复失败不得破坏现有租户数据；批大小、审计条数与版本保留数都是硬顶，失败调用不留下任何状态变化。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- difficulty: 困难
- module: credential_seal_vault
- file: credential_seal_vault.py
