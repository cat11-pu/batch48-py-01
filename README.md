# batch48-py-safety-d-03

## 题面

网格做季度策略评审时，没人说得清哪些 ALLOW 其实永远命中不了，评审表上把一条宽 DENY 当成全部拦截；有人删掉一条 DENY 之后，哪些请求会从拒绝变成放行更是没人敢判断，最后整批策略只能原样保留。上一次并发加载新 revision 时，还有评审工具读到了半份策略，得出的结论和线上完全对不上。范围是 gsb/batch48-py-safety-d-03 下的 mesh_authz_conflict_analyzer.py，类 MeshAuthzConflictAnalyzer，负责策略集登记、冲突诊断、遮蔽分类、修订影响面与快照。

公开接口：MeshAuthzConflictAnalyzer(*, max_policies=4096)；load(revision, policies) -> dict；revisions() -> list[int]；policies(*, revision=None) -> list[dict]；analyze(*, revision=None) -> dict；diff(from_revision, to_revision) -> dict；explain(policy_id, *, revision=None) -> dict；match(request, *, revision=None) -> str；expand_principal(spec) -> list[str]；path_segments(path) -> list[str]；snapshot() -> dict；restore(state) -> None；stats() -> dict。拒绝统一抛 MeshAuthzError(code, detail)。

故障与边界：

一、策略含 id、effect、principals、methods、paths、ports 六个字段，effect 只取 ALLOW 与 DENY，四个维度各自支持通配符，缺字段、effect 乱写、路径不以斜杠开头、端口不在 1 到 65535 都算结构非法。

二、revision 必须是大于等于 1 的整数且严格递增，重复或回退报 AUTHZ_REVISION_STALE，零与字符串报 AUTHZ_INVALID_REVISION；load 返回 revision 与 policies 两个计数；加载失败时该 revision 不落地，既有 revision 不受影响。

三、同一 revision 内策略 id 重复、单 revision 策略数超过 max_policies 都要报错，前者报 AUTHZ_INVALID_POLICY，后者报 AUTHZ_LIMIT_EXCEEDED。

四、判定口径：任一命中 DENY 即拒绝，否则命中 ALLOW 放行，都不命中返回 default_deny；同一次判定不得因策略遍历顺序不同而改变结果。

五、method 与 port 支持通配，请求缺字段或端口不是整数报 AUTHZ_REQUEST_INVALID；path 不以斜杠开头报 AUTHZ_PATH_INVALID，根路径的分段是空列表。

六、path 按段比较而不是字符串前缀：/api/* 命中 /api 与 /api/v1/items，不命中 /apix/v1；/api/v1 不命中 /api/v1/items。

七、analyze 固定返回 revision、allow_count、deny_count、fully_shadowed、weak_shadowed 与 conflicts 六项，空集给空列表，不随字典遍历顺序变化。

八、ALLOW 被 DENY 完全遮蔽与被更宽 DENY 弱遮蔽必须分开报告，弱遮蔽不能当成永不命中，conflicts 每条要写清是哪条 ALLOW 被哪条 DENY 遮蔽以及理由码。

九、principal 按 trust domain 与服务账号展开比较，不能只比原始字符串，同一 trust domain 下不同命名空间的同名服务账号要能正确区分。

十、explain 返回归一化后的 id、effect、principals、methods、paths 与 ports，methods 按字典序、ports 按数值升序，未知 id 报 AUTHZ_POLICY_UNKNOWN。

十一、diff 返回 from、to、added、removed、changed 与受影响请求类别；未知 revision 报 AUTHZ_REVISION_UNKNOWN，同一 revision 自比三段差异都为空；revision 差异中新增策略导致既有请求由允许变拒绝，必须单独列出受影响请求类别，不能只报策略增删。

十二、快照含 version、revisions 与计数，恢复后判定与冲突结论逐项一致；非法结构报 AUTHZ_SNAPSHOT_INVALID 且原状态不动；stats 返回 revisions、analyses 与 matches 三个计数。

硬约束：加载新 revision 与分析、判定并发时读方只能看到完整旧 revision 或完整新 revision，不得读到半份策略；快照与恢复跨进程后 revision 顺序与计数保持；策略总数受 max_policies 硬顶，失败加载不留下任何部分策略；同一 revision 连续两次 analyze 结果逐项一致。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；遮蔽分类与修订影响面自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 身份认证
- type: Bugfix
- difficulty: 困难
- module: mesh_authz_conflict_analyzer
- file: mesh_authz_conflict_analyzer.py
