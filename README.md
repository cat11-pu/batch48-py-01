# batch48-py-game-c-07

## 题面

外语剧上线后，字幕一条挂了八个字每秒，观众根本读不完；自动切分把标点切到行首，两条字幕还叠在一起，宽字符行比半角行多挤出一截。上游试过按字符数直接砍半，结果半角行空一半、全角行溢出画布，标点被甩到行首成了常态。范围是 `gsb/batch48-py-game-c-07` 下的 `caption_reading_fit_guard.py`，类 `CaptionReadingFitGuard`，负责阅读速度预算、显示列宽、按词切分与时长扩展。

公开接口：`CaptionReadingFitGuard(*, max_cps=17.0, max_columns=42, max_lines=2, min_duration_ms=800, max_duration_ms=7000, clock=None)`；`add_cue(start_ms, end_ms, text, *, cue_id=None) -> dict`；`remove_cue(cue_id) -> None`；`cues() -> list`；`cps_of(text, start_ms, end_ms) -> float`；`display_width(text) -> int`；`wrap_text(text, columns) -> list`；`split_cue(cue_id) -> list`；`extend_durations() -> int`；`fit_all() -> dict`；`timeline_valid() -> bool`；`snapshot() -> dict`；`restore(state) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 `CaptionError(code, detail)`，code 取 CAPTION_INVALID_TIMING、CAPTION_INVALID_TEXT、CAPTION_INVALID_WIDTH、CAPTION_DUPLICATE_CUE、CAPTION_CUE_NOT_FOUND、CAPTION_SNAPSHOT_INVALID。add_cue 返回 cue_id、start_ms、end_ms、text 四个字段，cues 返回深拷贝并按开始时间升序排列。

故障与边界：一、CPS 按去空格字符数除以时长秒数计算，保留三位小数，时长非正报 CAPTION_INVALID_TIMING，空文本合法返回零。二、开始时间不得为负，结束必须大于开始，否则同码拒绝。三、文本去空格后为空报 CAPTION_INVALID_TEXT；cue_id 重复报 CAPTION_DUPLICATE_CUE，未指定时按 cue-1、cue-2 顺延，编号不复用。四、display_width 按显示列计，全角汉字、假名与全角标点算两列，半角字母数字与半角标点算一列；行宽上限按显示列而不是字符数，wrap_text 的 columns 必须是正整数，否则报 CAPTION_INVALID_WIDTH。五、换行按空格贪心装行，同一行内词间只留一个空格，行首行尾不得留空白，单个词长于列宽时整词独占一行，不硬拆。六、CPS 超过 max_cps 时拆条，条数向上取整，拆分点优先标点、其次空格，标点不得落在拆分后的行首；拆条不得丢字或重字，拼接后去空格字符数必须与原文一致。七、每条时长按本段字符数除以 max_cps 向上取整，且不低于 min_duration_ms，也不超过 max_duration_ms；单条最多两行，排不下时先缩行宽再重排。八、拆分后各条首尾相接，整条时间轴保持单调，timeline_valid 在出现重叠或倒序时返回 False。九、拆分后不足最短时长向后扩，但不得压到下一条。十、fit_all 先拆后扩，返回 split 与 extended 两个计数。十一、snapshot 深拷贝 cue 表与配置，restore 校验 version 与字段类型，非法报 CAPTION_SNAPSHOT_INVALID 且原表不变。十二、cps_queries 只统计成功完成的换算，失败的调用只进 rejected。

硬约束：装载与查询共用一把可重入锁，三路并发各加五条后 cue 表恰好十五条、顺序稳定，读到的永远是完整表，不允许出现半条 cue；拆条与扩时长同样在这把锁内整段完成，中途不对外暴露。snapshot 必须在锁内取，restore 要么整份生效要么整份不生效。同一份 cue 表连续跑两遍 fit_all 结果必须一致，已达标的不再拆、已够长的不再扩，计数归零。单条 cue 的时长上限 7000 毫秒是硬顶，扩时长不得越过。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发装载与重启恢复自行验证；只做本地实现，不依赖第三方字幕库或真实字体度量。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- name: batch48-py-game-c-07
- domain: 游戏、图形与多媒体
- sub: 音视频处理
- type: Bugfix
- difficulty: 困难
- module: caption_reading_fit_guard
- typeName: CaptionReadingFitGuard
