# batch48-py-game-b-21

## 题面

美术组把角色材质改成分层覆盖后，主美改一次父材质的基础色，几十个子实例纹丝不动；反过来，子实例上调过的粗糙度重开编辑器又变回父材质的数值；打包机重开工程时，同一个实例两次解析出的参数顺序还不一致。上周有人把两个材质互挂成父级，编辑器当场卡死，重启后依赖这条链的实例解析结果全乱，审查面板里也看不出每个参数到底来自哪一层。范围是 gsb/batch48-py-game-b-21 下的 material_inheritance.py，类 MaterialInheritanceResolver，负责材质继承链登记、参数覆盖、解析、来源追溯、断链冻结与快照。

公开接口：MaterialInheritanceResolver()；define(material_id, parent_id, params) -> None；reparent(material_id, parent_id) -> None；override(material_id, name, value) -> None；override_many(material_id, values) -> None；clear_override(material_id, name) -> None；resolve(material_id) -> dict；chain(material_id) -> list[str]；detach(material_id) -> None；coerce(material_id, name, value)；type_of(material_id, name) -> str；descendants(material_id) -> list[str]；snapshot() -> dict；restore(state) -> None。所有拒绝抛 MaterialError(code)，code 取 MATERIAL_UNKNOWN、MATERIAL_DUPLICATE、MATERIAL_PARAM_UNKNOWN、MATERIAL_PARAM_INVALID、MATERIAL_PARAM_NAME_TOO_LONG、MATERIAL_TYPE_MISMATCH、MATERIAL_CYCLE、MATERIAL_DEPTH_EXCEEDED、MATERIAL_TOO_MANY_MATERIALS、MATERIAL_SNAPSHOT_INVALID。

故障与边界：一、参数合并必须子优先，未覆盖的参数取最近祖先值，根节点声明类型，子节点只提供初值与覆盖，不得改动声明宽度；二、resolve 返回 material、params 与 origins 三张表，参数按名字字典序输出，origins 记录每个参数的最终来源，descendants 返回全部后代并按名字升序排列；三、override 到未声明参数名要整份拒绝，不得静默塞进结果；四、override_many 在同一临界区内整批生效或整批不生效，中途一个名字非法要全部回滚，失败后旧值一个都不能被动到；五、clear_override 撤销本层覆盖后回落到最近祖先值，重复撤销保持幂等；六、根声明的标量参数收到单个数时按声明宽度扩展成 2、3、4 分量，类型名用 scalar、vec2、vec3、vec4，向量数值的分量数必须与声明宽度一致，不符整份拒绝，coerce 返回按声明类型归一后的值；七、重复 define 同一材质要报错，不得悄悄覆盖已有节点，对未登记材质的解析、改挂、断链与覆盖一律报 MATERIAL_UNKNOWN；八、reparent 允许改挂父级，但新父链回到自己时必须在定义阶段拒绝，并给出从该材质出发、沿父链回到自身的环路径，环路径首尾是同一个材质，不得把已登记的图改坏；九、detach 断开继承后该材质自成一根，把自己当时的解析结果冻结为自身声明，之后父材质的改动不得再影响它；十、链深上限 32、参数名长度上限 256、材质数上限 20000，超限报错且不落地；十一、单次解析只反映一个一致版本，同一材质两次 resolve 结果必须完全一致；十二、快照覆盖材质图与冻结状态，快照里含 version 与 materials，必须是深拷贝，恢复后链、参数与来源逐项一致，结构非法整份拒绝并保持原状态。

硬约束：override_many 与 resolve 会被多路编辑器线程同时调用，解析只能看到整批覆盖生效前或生效后的值，不得出现半套新旧混合；reparent、detach 与解析交错执行时，链上不得出现自环、悬挂或半断开的中间态；快照跨进程恢复后不得与恢复前的容器共享可变结构；材质图规模受三道上限硬顶，失败调用不改变已有图。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，并附一段改动说明，写清冻结与环检测的判定口径。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- difficulty: 困难
- module: material_inheritance
- typeName: MaterialInheritanceResolver
