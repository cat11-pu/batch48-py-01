# batch48-py-game-b-12

## 题面

室外场景改成级联阴影后，靠近镜头的地面出现细密条纹，转视角时两级交界处有一条硬边，远处物体在级联切换瞬间跳动；同一台相机每次重新计算，级联近远距离还会差一点点，缓存完全对不上；美术在清晨场景里把阴影距离拉到两百米，级联边界又出现一条锯齿，程序侧发现连续两次出图时光空间原点跳动半个纹素，整张阴影贴图跟着抖了一下。复查还发现切到无限远平面后级联数被撑到五级，超出预算。范围是 gsb/batch48-py-game-b-12 下的 csm_cascade_planner.py，类 CsmCascadePlanner，负责级联划分、纹素对齐、过渡带与按距离收敛级联数。

公开接口：`CsmCascadePlanner(*, max_cascades=4, max_resolution=4096, far_cap=10000.0, resolution=2048)`；`set_resolution(resolution) -> int`；`plan(camera, split_lambda=0.5, count=4) -> dict`；`snap_to_texel(cascade, world_pos, texel_size) -> tuple`；`blend_band(cascade) -> dict`；`stable_split_count(distance) -> int`；`current_plan() -> dict`；`stats() -> dict`；`reset_stats() -> None`；`snapshot() -> dict`；`restore(state) -> None`。拒绝统一抛 CsmError(code, detail)。

故障与边界：一、camera 需含 near、far、fov、aspect，near 必须大于零且小于 far，fov 在零到一百八十度之间，aspect 大于零，否则抛 CSM_INVALID_CAMERA 且不改当前计划。二、split_lambda 限于零到一，count 为正整数且不超过 max_cascades，否则分别抛 CSM_INVALID_LAMBDA、CSM_INVALID_COUNT、CSM_COUNT_LIMIT。三、切分按对数项与均匀项的混合权重计算，lambda 为零时退化为均匀切分，为一则取对数切分。四、各级 near 从相机近平面开始，相邻级 near 等于上一级 far，far 严格递增，最后一级 far 必须等于相机远平面；切分点恰好落在远平面时仍给出非零范围。五、远平面为无限时按 far_cap 截断，级联数不再增加，过渡带按截断后的距离计算。六、正交投影范围以该级 far、fov 与纵横比推导，不能沿用上一级，ortho_height 等于宽度除以 aspect，纹素尺寸等于正交宽度除以当前分辨率，抖动与光源方向不得改变纹素尺寸。七、snap_to_texel 把世界坐标各分量吸附到纹素尺寸的整数倍，texel_size 非正抛 CSM_INVALID_TEXEL，坐标不是三元组抛 CSM_INVALID_POSITION，级联结构非法抛 CSM_INVALID_CASCADE。八、blend_band 返回 start、end、width，宽度为级联跨度的一成，end 落在该级 far。九、stable_split_count 按每级两千五百单位收敛，结果夹在一到 max_cascades，距离非正抛 CSM_INVALID_DISTANCE。十、分辨率只接受一到 max_resolution 的整数，非法抛 CSM_INVALID_RESOLUTION 且保留原值。十一、plan 返回 count、lambda、resolution 与 cascades；cascades 每项含 index、near、far、ortho_width、ortho_height、texel_size，索引从零连续递增；current_plan 返回最近一次成功的计划，失败不得覆盖它。十二、snapshot 覆盖 version、resolution、plan、stats，必须深拷贝；restore 遇非法结构抛 CSM_SNAPSHOT_INVALID 且原状态不变。

硬约束：plan 与 set_resolution 全程持锁，多路并发下每次返回的计划必须自洽，纹素尺寸与计划里声明的分辨率对应，不得出现半新半旧参数；快照恢复后分辨率与级联参数必须与恢复前一致。验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: csm_cascade_planner
- typeName: CsmCascadePlanner
