# batch48-py-game-b-22

## 题面

贴图批量转压缩格式上线后，界面图里凡是宽高不是块尺寸倍数的，边缘一圈发花；法线贴图切到 6x6 块格式后，mip 链在最后几级对不上，远处反而闪白点；打包机还把线性格式和 sRGB 格式的同尺寸贴图当成同一份缓存，颜色空间串了；同一张贴图重复打包时，出包日志里的级数一栏和实际级表也对不上。范围是 gsb/batch48-py-game-b-22 下的 texture_compression_planner.py，类 TextureCompressionPlanner，负责块对齐补齐、mip 链规划、链连续性校验与格式缓存。

公开接口：TextureCompressionPlanner()；block_size(format) -> int；format_info(format) -> dict；pad_to_block(width, height, block) -> dict；mip_count(width, height, block) -> int；level_blocks(width, height, block) -> dict；plan(width, height, format) -> dict；validate(levels) -> list[int]；cache_key(width, height, format) -> str；cache_size() -> int；invalidate_cache() -> None；snapshot() -> dict；restore(state) -> None。所有拒绝抛 TextureError(code)，code 取 TEXTURE_FORMAT_INVALID、TEXTURE_SIZE_INVALID、TEXTURE_SIZE_TOO_LARGE、TEXTURE_LEVEL_TOO_MANY、TEXTURE_BLOCK_INVALID、TEXTURE_SNAPSHOT_INVALID。

故障与边界：一、块尺寸按格式取，BC 系列 4x4、ASTC_6x6 系列 6x6、ASTC_8x8 系列 8x8，未压缩的 RGBA8 按 1 像素一块，format_info 返回 format、block、srgb 与 kind，未知格式整份拒绝；二、尺寸先补齐到块倍数再算块数，补齐尺寸与有效区域都要如实返回，尺寸字段统一命名为 padded_width、padded_height、valid_width、valid_height、blocks_x、blocks_y、blocks，非 2 的幂尺寸允许但必须记录补齐信息；三、每级向下取整、至少一块，块格式逐级减半到某一边小于块尺寸后保留该级，再补一级 1x1，线性格式一直减半到 1x1，末级 1x1 必须显式出现在级表里；四、6x6 块格式下 64x64 的级数与 4x4 以及非块格式都不同，最后一级仍然是 1x1；五、线性与 sRGB 格式即便宽高相同也不得共用缓存键；六、尺寸恰好是块倍数时补齐尺寸与原始尺寸相同，有效区域覆盖整张图；七、尺寸上限 16384、级数上限 16，级数由尺寸与块尺寸共同决定，超限整份拒绝；八、格式非法、尺寸为 0、块尺寸不在 1、4、6、8 内都要报错，且不得改动已有计划与缓存；九、validate 逐级检查尺寸连续、末级 1x1、补齐尺寸与块数一致，连续指每一级的宽高恰好是上一级向下取整的一半，最后一级可以只补 1x1，空链按 [0] 处理，函数返回问题级号的升序列表，空列表表示通过；十、同一输入两次 plan 结果逐项一致，plan 返回 format、block、level_count、levels 与 cache_key，每级含 level、width、height、padded_width、padded_height、blocks_x、blocks_y、blocks、block，缓存命中与重新计算的结果必须逐字段相等，不得返回会被调用方改坏的对象；十一、并发 plan 只读缓存必须安全，同一格式与尺寸只落一份缓存，cache_size 返回当前条目数；十二、快照含 version 与 cache，必须深拷贝，恢复后缓存条目与再算出的计划逐项一致，结构非法整份拒绝并保持原状态。

硬约束：多路打包线程同时 plan 不同格式尺寸时，缓存写入与读取不得互相覆盖，缓存按格式与尺寸分键互不遮挡；进程重启后凭快照恢复，已规划的计划与缓存计数保持一致；缓存条目数与单计划级数都有硬顶，失败调用不新增条目。

验收：python3 -m pytest -q tests 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，并附一段改动说明，写清块对齐与 mip 终止条件。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- difficulty: 困难
- module: texture_compression_planner
- typeName: TextureCompressionPlanner
