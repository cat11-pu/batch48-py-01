# batch48-py-game-b-25

## 题面

主机档把渲染分辨率降到 0.6 倍后直接双线性放大，远景栅栏糊成一片；美术把锐化拉满，远景边缘又出现一圈白边；非整数缩放比下越靠画面边缘的像素偏得越多，长焦镜头里能看到明显的像素漂移；同一帧两次取权重结果还对不上，画面每帧闪一下；1080p 与 4K 两档上的权重查询次数还和维护面板上的计数对不上。范围是 gsb/batch48-py-game-b-25 下的 spatial_upscaler.py，类 SpatialUpscaler，负责上采样几何规划、4x4 采样权重、锐化强度、越界处理与配置快照。

公开接口：SpatialUpscaler()；set_sharpen(amount) -> None；sharpen_state() -> dict；set_edge_threshold(value) -> None；edge_threshold() -> float；set_gradient(x, y, gx, gy) -> None；clear_gradients() -> None；sample_point(src_w, src_h, dst_w, dst_h, x, y) -> dict；weights_at(src_w, src_h, dst_w, dst_h, x, y) -> list[float]；upscale(src_w, src_h, dst_w, dst_h) -> dict；clamp_out(v) -> float；stats() -> dict；reset_counters() -> None；snapshot() -> dict；restore(state) -> None。所有拒绝抛 UpscaleError(code)，code 取 UPSCALE_SIZE_INVALID、UPSCALE_SIZE_TOO_LARGE、UPSCALE_THRESHOLD_INVALID、UPSCALE_SNAPSHOT_INVALID。

故障与边界：一、采样坐标按像素中心对齐，用 (x+0.5)*src/dst-0.5 计算，sample_point 返回 src_x 与 src_y，非整数缩放比不得逐像素累积漂移，结果要夹在输入范围内；二、4x4 邻域权重整体归一化到 1，所有权重非负；三、邻域全平坦时权重退化为双线性，权重和仍为 1；四、登记梯度后权重沿边缘方向偏移，不同方向的梯度必须给出不同的权重分布；五、锐化强度有效区间是 0 到 1，超过上限按 1.0 处理并计数，不得静默忽略，sharpen_state 返回 amount 与 clamped；六、clamp_out 把越界值夹到 0 到 1；七、输出尺寸上限 16384x16384，输入小于 1x1 报错，非法调用整份拒绝且不计入上采样次数，成功的 upscale 返回 src_width、src_height、dst_width、dst_height、scale_x、scale_y、sharpen 与 taps，scale_x 与 scale_y 分别为目标宽高除以源宽高，taps 固定为 16；八、边缘阈值必须大于 0 且不超过 1，非法阈值保持原值；九、同一坐标两次 weights_at 的结果逐位一致，按 float32 计算，不得依赖调用顺序或全局状态；十、非整数缩放比下最后一个输出像素的中心坐标必须落在输入范围内；十一、stats 返回 upscales、weight_queries、clamped 三个计数，weight_queries 只统计 weights_at 调用，并发下不得丢计数，reset_counters 把三者全部归零；十二、快照含 version 与 gradients，必须深拷贝，恢复后配置、计数与梯度表逐项一致，结构非法整份拒绝并保持原状态。

硬约束：多路渲染线程并发查询权重与执行上采样时，只能读到同一份完整配置，不得看到一半旧阈值一半新梯度，切换分辨率时也不得残留上一档的梯度；梯度表按像素坐标登记，同一坐标重复登记以最后一次为准；进程重启后凭快照恢复，配置与计数保持一致；锐化强度、输出尺寸与统计计数都有硬顶，并发查询不得丢计数，非法调用不改变已有配置。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；非整数缩放比与并发查询场景自行验证；只做本地实现，并附一段改动说明，写清像素中心对齐与权重归一的口径。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- difficulty: 困难
- module: spatial_upscaler
- typeName: SpatialUpscaler
