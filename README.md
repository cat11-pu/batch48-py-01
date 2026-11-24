# batch48-py-game-b-03

## 题面

延迟渲染切到可见性缓冲之后，编辑器里解析出来的材质开始错位：同一块石墙上一半是岩石一半是草皮，鼠标悬停两次同一像素读出的三角形编号还不一样；树冠边缘的像素整块被判定给最上层的叶子，底下透出来的天空消失，覆盖率被当成了一。压力测试里候选数溢出之后，解析直接返回候选列表里的第一个三角形，画面出现随机噪点；进程重启后缓存里的旧世代结果又被拿出来用，改过的缓冲尺寸完全没有生效。

范围是 gsb/batch48-py-game-b-03 下的 visibility_buffer_resolver.py，类 VisibilityBufferResolver，负责可见性缓冲的尺寸管理、按深度写入三角形 ID、逐像素与区域解析、解析缓存、统计与快照恢复。

公开接口：`VisibilityBufferResolver(*, max_width=512, max_height=512, max_candidates=4, max_triangles=1024, clock=None)`；`resize(width, height) -> dict`；`clear() -> dict`；`submit_triangle(triangle) -> dict`；`resolve_pixel(x, y) -> dict`；`resolve_region(x0, y0, x1, y1) -> dict`；`generation() -> int`；`buffered_triangles() -> list`；`stats() -> dict`；`reset_stats() -> None`；`snapshot() -> dict`；`restore(state) -> None`。拒绝统一抛 VisibilityError(code, detail)。

故障与边界：一、宽高只接受 1 到 max_width 与 max_height 之间的整数，否则报 VBUF_INVALID_SIZE 并计入 rejected，失败不得改动当前世代；二、未 resize 之前 submit_triangle、resolve_pixel、resolve_region 与 clear 一律报 VBUF_NOT_RESIZED；三、三角形要带正整数 id、非空 material、字典 params 与非空 pixels，depth 必须大于零，coverage 必须大于零且不超过一，否则报 VBUF_INVALID_TRIANGLE，同一缓冲里重复的 id 报 VBUF_DUPLICATE_TRIANGLE；四、任何一个像素越界（含等于宽或高）都整颗拒绝并报 VBUF_OUT_OF_BOUNDS，不得只写入合法部分；五、缓冲内三角形数超过 max_triangles 报 VBUF_TOO_MANY_TRIANGLES，detail 里带 triangle_count 与 max_triangles；六、像素按深度比较写入，近的胜出，后写的只有更近才替换，深度完全相同时按稳定次序裁决，重复解析结果一致；七、只覆盖像素一部分的三角形要保留 coverage，不能当作整像素覆盖；八、每像素候选超过 max_candidates 的像素要显式标记为不可解析，解析时不得返回任意一个候选；九、未被写入的像素解析为背景，triangle、material 与 depth 为 None，coverage 为零，params 为空字典，并带上当前 generation；十、解析结果按缓冲世代缓存，返回的是深拷贝，调用方改动返回值不得污染缓存，写入新三角形要让旧缓存失效；十一、resolve_region 要求 x0 不大于 x1、y0 不大于 y1 且四角都在缓冲内，否则报 VBUF_INVALID_REGION，cells 按行给出且与逐像素结果一致；十二、clear 清空缓冲但保留尺寸并把世代加一，snapshot 是深拷贝，restore 结构非法报 VBUF_SNAPSHOT_INVALID 且原状态不变。

硬约束：resize、clear、submit_triangle、resolve_pixel、resolve_region、snapshot 与 restore 全程持锁，八路并发提交互不覆盖，triangles_submitted 必须等于成功提交的次数，缓冲区尺寸与三角形数、每像素候选数都是硬顶，超限拒绝而不是静默丢弃或扩容；解析缓存以世代为键，世代推进必须让旧条目整体不可见，返回结构不得与内部缓存共享引用；快照恢复后尺寸、世代、三角形表与统计都要在，且不与恢复前的对象共享结构。

验收：`python3 -m pytest -q tests` 的 15 条必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-b-03
- domain: 游戏、图形与多媒体
- sub: 图形渲染
- type: Bugfix
- difficulty: 困难
- module: visibility_buffer_resolver
- typeName: VisibilityBufferResolver
- file: visibility_buffer_resolver.py
