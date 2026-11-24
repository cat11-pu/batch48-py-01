# batch48-py-game-c-05

## 题面

连麦人数一多，画布上两路画面就叠在一起，谁压谁全看加入顺序；切换讲解布局时观众还能看到半套旧布局，旧槽位和新槽位混在一帧里，编码器还因为奇数坐标裁掉一条边；同一路摄像头被绑到两个槽位，运营只能手工挪位，越挪越乱。回放日志发现布局调整是一槽一槽改的，画布从 1280 换到 1920 时槽位只是按宽度缩放，高度没跟上，底部画面被切掉；这是云端混流服务上线的第三个版本，早期为了赶功能把槽位、源和布局揉在一个字典里，谁先写谁生效，美工给的模板经常带半像素对齐，编码器对奇数坐标一律裁掉一列；运营侧每天收到四五条相关录屏，返工都落在同一段布局代码上。范围是 `gsb/batch48-py-game-c-05` 下的 `layout_mixer_session.py`，类 `LayoutMixerSession`，负责画布槽位、源绑定、层级覆盖、原子切换与缩放重排。

公开接口：`LayoutMixerSession(*, canvas_width=1280, canvas_height=720, max_slots=16, max_sources=16, clock=None)`；`add_slot(slot_id, rect, *, z=0) -> dict`；`remove_slot(slot_id) -> None`；`bind_source(slot_id, source_id) -> dict`；`unbind_source(slot_id) -> None`；`apply_layout(layout) -> dict`；`current_layout() -> dict`；`resize_canvas(width, height) -> dict`；`hit_test(x, y) -> str | None`；`slot_info(slot_id) -> dict`；`source_slot(source_id) -> str | None`；`revision() -> int`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 `LayoutError(code, detail)`，code 取 LAYOUT_INVALID_RECT、LAYOUT_INVALID_Z、LAYOUT_RECT_OUT_OF_BOUNDS、LAYOUT_DUPLICATE_SLOT、LAYOUT_SLOT_LIMIT、LAYOUT_SOURCE_LIMIT、LAYOUT_SOURCE_BOUND、LAYOUT_INVALID_SOURCE、LAYOUT_UNKNOWN_SLOT、LAYOUT_INVALID_CANVAS、LAYOUT_LAYOUT_INVALID、LAYOUT_SNAPSHOT_INVALID。

故障与边界：一、槽位矩形按左、上、宽、高四个整数给出，不接受浮点，宽高为正，否则 LAYOUT_INVALID_RECT；z 取 0 到 1000 的整数，否则 LAYOUT_INVALID_Z。二、矩形越出画布或坐标为负报 LAYOUT_RECT_OUT_OF_BOUNDS。三、槽位号重复报 LAYOUT_DUPLICATE_SLOT，超过 max_slots 报 LAYOUT_SLOT_LIMIT。四、同一路源最多占一个槽位，重复占用直接拒绝报 LAYOUT_SOURCE_BOUND，不自动挪位。五、新增源超过 max_sources 报 LAYOUT_SOURCE_LIMIT；空源名报 LAYOUT_INVALID_SOURCE。六、bind_source 对同一槽位是替换语义，旧源自动解绑；unbind_source 只清空该槽位。七、apply_layout 先校验整份新布局的全部槽位、z 与源，全部合法才整体替换并递增 revision，任何一条不合法整份拒绝且旧布局与 revision 不变。八、current_layout 返回 revision、canvas 与 slots 的深拷贝，调用方改动返回值不得影响会话。九、resize_canvas 按新旧画布比例重算每个槽位，坐标与宽高取偶对齐，结果必须落在新画布内并保持绑定关系；宽高非正报 LAYOUT_INVALID_CANVAS。十、hit_test 返回包含该点的最高 z 槽位，同 z 取后加入的，没有命中返回 None。十一、remove_slot、slot_info、bind_source 对未知槽位报 LAYOUT_UNKNOWN_SLOT；布局字典缺 slots 键报 LAYOUT_LAYOUT_INVALID。十二、snapshot 深拷贝槽位、源绑定与 revision，restore 非法报 LAYOUT_SNAPSHOT_INVALID 且原状态不变。

硬约束：add_slot、remove_slot、bind_source、apply_layout 共用一把可重入锁，四路并发各加 4 个槽位后 slots_added 等于 16 且 revision 等于 16；会话最多 16 路；槽位与源绑定必须在同一把锁里完成，读到的布局要么全旧要么全新，混流输出不得读到半套旧槽位和新槽位；统计固定返回 slots_added、slots_removed、binds、unbinds、layouts_applied、resizes、hit_tests、rejected 八个整数。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-c-05
- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 困难
- module: layout_mixer_session
- typeName: LayoutMixerSession
