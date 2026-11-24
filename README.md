# batch48-py-safety-d-14

## 题面

特权助手被普通用户通过本地 socket 调起来改了系统代理，审计里只留下一条成功记录，看不到被拒绝的尝试；同一请求重放三次，助手执行了三遍，代理配置被写乱。这类调用本来只在维护窗口出现，现在随时都可能被触发。事后复盘还发现有人用带首尾空白、重复分隔符和上跳段的参数绕过了白名单，把上游地址指到了内网。范围是 gsb/batch48-py-safety-d-14 下的 privileged_helper_guard.py，类 PrivilegedHelperGuard，负责请求准入、参数规范化、白名单匹配与调用审计。

公开接口：`PrivilegedHelperGuard(*, max_audit: int = 256, max_args: int = 16, clock=None)`；`allow_peer(uid, gid, exe_prefix) -> None`；`revoke_peer(uid, gid) -> None`；`register_command(command, *, argv_prefix=(), max_args=8) -> dict`；`normalize(args) -> list`；`handle(request: dict, *, now=None) -> dict`；`audit_entries() -> list`；`whitelist_version() -> int`；`snapshot() -> dict`；`restore(state: dict) -> None`；`stats() -> dict`；`reset_stats() -> None`。请求字段为 request_id、peer（含 uid、gid、exe）、command 与 args；参数条数上限取命令自身上限与全局 max_args 的较小值；对端凭据以本地 socket 传递的 uid、gid 与可执行文件路径为准，助手不回查系统账号。

故障与边界：一、请求缺字段或类型不符（request_id 不是非空字符串、peer 缺 uid/gid、args 不是字符串列表）抛 HELPER_REQUEST_INVALID；二、对端 uid 与 gid 未同时登记抛 HELPER_PEER_DENIED，原因明细要能区分是哪一项不匹配；三、对端 exe 不是绝对路径、或不在登记前缀之下抛 HELPER_EXE_DENIED；四、命令未登记或为空串抛 HELPER_COMMAND_DENIED，命令名区分大小写；五、参数条数超过上限抛 HELPER_ARGC_EXCEEDED，超限请求不得被截断后再执行；六、规范化后的参数不以命令前缀开头抛 HELPER_ARG_DENIED；七、同一命令以不同前缀重复登记抛 HELPER_COMMAND_CONFLICT，登记成功返回 command、argv_prefix 与版本，全局白名单版本单调加一；八、审计写满抛 HELPER_AUDIT_FULL，且本次不执行；九、handle 成功返回 status、request_id、command、argv 与 whitelist_version，argv 是规范化后的字符串副本，调用方修改它不影响审计记录；十、快照包含对端规则、命令表、白名单版本与计数，restore 缺任一部分抛 HELPER_SNAPSHOT_INVALID 且原状态不变；十一、stats 固定返回 executed、denied、audited 三个整数，拒绝同样让 denied 增长，统计与审计条数要对得上，reset_stats 全部归零。

硬约束：参数必须先完成规范化再进入前缀比对，首尾空白、重复分隔符、单点段与上跳段都不得绕过白名单；每次拒绝同样写入审计并带原因码，拒绝不能让审计静默丢条；白名单与对端规则在快照恢复后必须能直接投入判断，不能要求重新登记；同一 request_id 重放只执行一次，直接返回首次结果，不得重复改动系统；handle 与审计写入全程持锁，十路并发处理不同请求后 executed 与 audited 都必须等于 10，审计里的 request_id 不得重复或丢失。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重放场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 地狱
- module: privileged_helper_guard
- typeName: PrivilegedHelperGuard
