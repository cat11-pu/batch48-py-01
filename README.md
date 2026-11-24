# batch48-py-safety-a-09

## 题面

重复注册回两个 client_id，换证旧 secret 失效。范围 gsb/batch48-py-safety-a-09 的 oauth_client_registration.py，类 ClientRegistrationAuthority，抛 OAuthRegistrationError。

接口：register_client(tenant, metadata, now)、update_client(tenant, client_id, metadata, now)、rotate_secret(tenant, client_id, now, overlap)、resolve_client(tenant, client_id)、normalize_redirect_uri(uri)、list_clients(tenant, limit, cursor)。

故障：①grant_types 限 authorization_code、client_credentials、refresh_token；authorization_code 需 response_types 含 code 与非空 redirect；client_credentials 不得带 response 与 redirect；②redirect 小写、去默认端口、空路径补 /，否则 OAUTH_REDIRECT_INVALID；③同元数据注册幂等回原记录，secret 只存哈希；④rotate 出新 secret，旧 secret 到 previous_valid_until 可用；⑤update 失败拒绝、版本加一；⑥list 按 client_id 升序、游标末键；⑦revoke 后 resolve 与再撤抛 OAUTH_UNKNOWN_CLIENT；⑧match 回环任意端口，非回环精确匹配禁片段；⑨auth_method none 带 client_secret 拒绝；⑩翻页新增不重不漏。

硬约束：并发重复注册只落一条且 secret 只给一次；snapshot 深拷贝含 clients、stats，restore 可用，非法结构抛 OAUTH_SNAPSHOT_INVALID。

验收：python3 -m pytest -q tests 15 条过；不改测试、不跳过、不写死；并发自验

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- module: oauth_client_registration
- typeName: ClientRegistrationAuthority
