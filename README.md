# batch48-py-game-b-20

## 题面

换到大场景后柱子和地面同时出现阴影痤疮，美术把偏移调大又冒出悬浮阴影；同一个光源和接收面连续求两次，第二次数值和第一次对不上，切到高精度纹素后缓存里还是旧尺寸的值；有人把偏移上限临时调高一档，越界夹紧之后悬浮标记全部消失，美术以为问题修好了。范围是 gsb/batch48-py-game-b-20 下的 shadow_bias_tuner.py，类 ShadowBiasTuner，负责斜率偏移、法线偏移、接收面深度偏差与联合求解。

公开接口：`ShadowBiasTuner(*, max_offset=0.05, texel_min=0.0001, texel_max=10.0)`；`slope_bias(light_dir, normal, texel_world) -> float`；`normal_offset(light, texel_world, base=0.0) -> float`；`receiver_bias(depth_grad, texel_size) -> float`；`resolve(light, receiver) -> dict`；`cache_stats() -> dict`；`clear_cache() -> None`；`invalidate(light_id) -> int`；`set_limits(*, max_offset=None, texel_min=None, texel_max=None) -> dict`；`stats() -> dict`；`reset_stats() -> None`；`snapshot() -> dict`；`restore(state) -> None`；`reset() -> None`。拒绝统一抛 ShadowBiasError(code, detail)。

边界与故障：一、光线方向与法线都先归一化，点乘取绝对值并截到 0 到 1；斜率偏移为根号下 1 减点乘平方，除以点乘，再乘纹素世界尺寸，最后截到 max_offset；点乘与 1 相差小于 1e-6 时按退化处理直接返回 max_offset，不得除零，也不得返回 NaN。二、纹素世界尺寸必须是有限数并落在 texel_min 到 texel_max 之间，否则抛 SHADOW_TEXEL_INVALID；硬范围是 1e-4 到 10，set_limits 只能在这个范围内收紧，不能放大。三、法线偏移为 base 加上纹素世界尺寸的一半，再截到 max_offset；base 缺省为 0，也可以从光源的 normal_bias_base 取。四、接收面偏差为深度梯度乘纹素尺寸再截到 max_offset，深度梯度为负抛 SHADOW_RECEIVER_INVALID。五、light 需要非空 id 与三维 dir，可带 normal_bias_base；receiver 需要非空 id、三维 normal、texel_world 与 depth_grad；缺字段、类型不符或法线全零时分别抛 SHADOW_LIGHT_INVALID 与 SHADOW_RECEIVER_INVALID。六、resolve 把斜率、法线、接收面三项相加后再截到 max_offset，返回 light、receiver、slope_bias、normal_offset、receiver_bias、offset、floating、clamped，clamped 表示截断前的原始和超过上限。七、原始和超过当前纹素世界尺寸 2 倍时 floating 为真，但仍要返回可用偏移；偏移被上限夹紧时仍按夹紧前的原始和判定悬浮并计入计数，不得因为夹紧而漏标。八、缓存键含光源 ID、接收面 ID 与纹素世界尺寸，命中时也要按当前纹素尺寸复核，尺寸变化后旧值必须失效并重新求解。九、cache_stats 返回 hits、misses、size；clear_cache 只清条目不清累计计数；invalidate 按光源 ID 撤销条目并返回条数，未知光源返回 0。十、set_limits 只接受 0 小于 max_offset 且不超过 1、0 小于 texel_min 且 texel_min 小于 texel_max 且 texel_max 不超过 10，否则抛 SHADOW_LIMIT_INVALID 且不变更；变更成功后清空缓存，返回 max_offset、texel_min、texel_max 三项当前值。十一、resolve 读到的纹素尺寸越过当前区间时夹紧到边界并计入 texel_clamped，不得静默吞掉。十二、同一光源与接收面连续两次 resolve 结果逐位一致；snapshot 覆盖上限、缓存条目与四个计数，必须是深拷贝，restore 遇非法结构抛 SHADOW_SNAPSHOT_INVALID 且原状态不变。

硬约束：并发的 resolve 与 set_limits、clear_cache 只能看到完整配置，不允许命中半套上限；快照恢复后上限、缓存条目与计数必须与中断前一致；缓存按光源与接收面去重，不随调用次数无界增长。stats 固定返回 resolves、floating、clamped、texel_clamped 四个整数，reset_stats 只清这四个计数。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与快照恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: shadow_bias_tuner
- typeName: ShadowBiasTuner
