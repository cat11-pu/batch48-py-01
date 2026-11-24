# batch48-py-game-b-17

## 题面

摄影模式接入第三方镜头预设后，广角端画面边缘黑了一圈，色差补偿把红蓝通道推到画面外；切到畸变很强的鱼眼预设，反畸变求解吐出 NaN，整帧采样表变成黑块；重拍时同一个采样点在两次调用里得到的坐标差最后一位，接缝对不上。范围是 gsb/batch48-py-game-b-17 下的 lens_distortion_pass.py，类 LensDistortionPass，负责畸变与色差采样坐标、反畸变迭代、逐像素采样表与越界处理。

公开接口：`LensDistortionPass(*, max_iter=16)`；`set_profile(*, k1=0.0, k2=0.0, k3=0.0, ca_r=0.0, ca_b=0.0) -> dict`；`profile() -> dict`；`distort(uv) -> dict`；`undistort(uv, max_iter=None) -> dict`；`border_mode(mode) -> str`；`border() -> str`；`border_uv(uv) -> dict`；`sample_grid(width, height, step) -> dict`；`last_grid() -> dict | None`；`stats() -> dict`；`reset_stats() -> None`；`snapshot() -> dict`；`restore(state) -> None`；`reset() -> None`。拒绝统一抛 LensError(code, detail)。

边界与故障：一、预设里的三个径向系数绝对值不得超过 10，两个色差系数绝对值不得超过 0.5，且全部必须是有限数，否则抛 LENS_PROFILE_INVALID，当前预设、边界模式与已有采样表都不变；profile 与 set_profile 都返回 k1、k2、k3、ca_r、ca_b 五项深拷贝。二、归一化坐标为 n 等于 ((u-0.5)*2, (v-0.5)*2)，r2 为 n 的模平方，径向因子为 1 + k1*r2 + k2*r2 平方 + k3*r2 立方；distort 把归一化坐标乘以因子后换回 0 到 1 的屏幕坐标。三、distort 返回 r、g、b 三个坐标与 valid；绿通道只乘径向因子，红通道额外乘 1+ca_r，蓝通道额外乘 1+ca_b；色差只偏移红蓝通道，绿通道始终是基准。四、border 只接受 clamp、black、mirror，其他取值抛 LENS_BORDER_MODE_INVALID，构造后默认 clamp；border_mode 设置并返回当前模式，border 只读取。五、border_uv 单独处理越界并返回 uv 与 valid：clamp 把坐标钳到 0 到 1 并标 valid 为真；black 保留计算值并标 valid 为假；mirror 先做一次镜像折叠，折叠后仍在 0 到 1 之外的角落再折第二次，两次之后仍越界就退化为钳制。六、色差偏移后红蓝通道越界同样按当前边界模式处理，绿通道的 valid 只由绿通道自身决定。七、越界处理全程不得产生 NaN，也不得抛出除 LensError 以外的异常。八、undistort 用牛顿迭代从畸变坐标反求原坐标，迭代次数不超过 max_iter；强畸变下迭代不收敛时返回最后一次坐标并标 converged 为假，不得返回 NaN 或抛异常。九、undistort 的 max_iter 必须在 1 到 64 之间，传 None 时用构造时的值，越界抛 LENS_ITER_INVALID；返回 uv、converged、iterations 三个字段。十、sample_grid 的采样点取 (i*step, j*step)，cols 为 width 除以 step 向上取整，rows 同理，count 为二者乘积；每格含 u、v、g 与 valid；step 小于 1 抛 LENS_STEP_INVALID；last_grid 返回最近采样表的深拷贝，从未生成过时为 None。十一、采样表宽高上限 16384、采样点总数上限 262144，超出抛 LENS_GRID_TOO_LARGE，且不改预设、边界模式与已有采样表。十二、同一 uv 连续两次 distort 逐位一致；snapshot 覆盖预设、边界模式、最近采样表与四个计数，必须是深拷贝，restore 遇非法结构抛 LENS_SNAPSHOT_INVALID 且原状态不变。

硬约束：并发的 distort、undistort 与 set_profile 只能看到完整预设，不允许出现半套参数；快照恢复后预设与最近采样表必须与中断前一致；预设有取值上限、采样表有尺寸上限，任何一次调用都不得让内部表无界增长。stats 固定返回 distorts、undistorts、grids、rejected 四个整数，rejected 按整次 distort 调用计，reset_stats 只清计数。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与快照恢复场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- module: lens_distortion_pass
- typeName: LensDistortionPass
