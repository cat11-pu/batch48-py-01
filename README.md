# batch48-py-safety-b-06

## 题面

子进程启动器这周被安全评审打回来了：容器里跑起来的进程能看到宿主机传进来的 LD_PRELOAD 和一批内部变量，运维只希望保留 PATH 与 LANG；有人在启动参数里塞了带换行的值，进了子进程环境后变成两条变量；某次升级后启动器把日志管道也一起关了，子进程一写日志就收到 SIGPIPE；工作目录被写成已经删掉的临时目录，新起的进程直接起不来；还有一次启动器顺手改了自身的 umask，守护进程之后上传的文件权限全变成了 077；调用方还发现，拿到启动计划后随手改几个字段，真正拉起进程时没人复核，改过的环境变量照样生效。范围是 gsb/batch48-py-safety-b-06 的 spawn_inheritance_guard.py，类 InheritanceGuard，负责在拉起子进程之前把环境变量、描述符、工作目录和 umask 一次性收敛好。

公开接口：InheritanceGuard(*, max_env=64, max_env_value=4096, max_fds=32, cwd_root="/srv/sandbox", default_umask=0o077, clock=None)；build_env(base, allowlist, overrides=None) -> dict；filter_fds(inherited, keep) -> list；prepare_cwd(path, root=None) -> str；apply_umask(mask) -> int；pending_umask() -> int；plan(spec) -> dict；verify(plan) -> dict；snapshot() -> dict；restore(state) -> None；stats() -> dict；reset_stats() -> None。拒绝统一抛 SpawnGuardError(code, detail)。

返回结构与入参：plan 生成 argv、env、fd_plan、cwd、umask、revision 与 fingerprint，首个计划修订号为 1，env 按变量名字典序；fd_plan 每行含 fd、action（keep、close、drop）与 cloexec，保留行 action 为 keep、cloexec 置 False；verify 返回 ok、revision、env_keys 与 fd_kept；stats 返回 planned、verified 与 plans_retained；snapshot 返回 version、revision、pending_umask、plans 与 stats；spec 含 argv、env_base、env_allowlist、env_overrides、fds、fd_keep、cwd、cwd_root 与 umask，cwd_missing 标记目录丢失，fds 每项含 fd、kind 与 cloexec，kind 取 file、pipe、socket、dir、tty。

故障与边界：一、白名单只按完整变量名匹配，LD_ 这种条目不能放行 LD_PRELOAD，条目含通配符或非法字符一律拒绝；二、净化结果里同名变量只留一条，键按字典序输出，且不得回写调用方传入的 base；三、变量值必须是字符串，含换行、回车或 NUL 一律拒绝，覆盖项的名字也必须落在白名单内；四、变量条数与单值长度都有硬顶，超限整次拒绝而不是截断；五、描述符集合里出现重复或负数要报错，保留集合引用未继承的描述符也要报错，被保留的通道不得出现在关闭名单；六、工作目录按路径段归一化，越出根目录、相对路径与空路径都拒绝，spec 标记目录已丢失时在生成阶段就拒绝；七、umask 必须是整数且只记录到计划中，不得改动调用进程自身的 umask，越界值拒绝；八、plan 的产物与入参不共享可变对象，每次生成递增修订号；九、verify 按计划内容重新核对，字段被改过报篡改，缺字段报结构非法；十、快照覆盖修订号、待用 umask 与统计，恢复后修订号继续上走不得复用；十一、大小写不敏感平台上 PATH 与 Path 同时出现时，净化阶段要归一为唯一键；十二、被保留的描述符指向目录或非常规文件时，要在复核阶段拒绝，不能留给子进程自己处理。

硬约束：plan 会被多个启动协程并发调用，修订号不得重复，也不得共用同一份可变环境字典；快照恢复后继续生成的计划编号要接在恢复点之后；环境条数、单值长度与保留描述符数都是硬顶。

验收：python3 -m pytest -q tests 的 15 条必须全过，不得修改测试、跳过或按固定输入写死；并发生成计划与快照恢复自行验证；拒绝码用 SPAWN_ALLOWLIST_INVALID、SPAWN_ENV_VALUE_INVALID、SPAWN_ENV_NOT_ALLOWED、SPAWN_ENV_NAME_INVALID、SPAWN_ENV_LIMIT、SPAWN_FD_DUPLICATE、SPAWN_FD_UNKNOWN、SPAWN_FD_INVALID、SPAWN_CWD_INVALID、SPAWN_CWD_ESCAPE、SPAWN_UMASK_INVALID、SPAWN_ARGV_INVALID、SPAWN_CWD_MISSING、SPAWN_PLAN_TAMPERED、SPAWN_PLAN_INVALID、SPAWN_SNAPSHOT_INVALID；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 困难
- module: spawn_inheritance_guard
- typeName: InheritanceGuard
