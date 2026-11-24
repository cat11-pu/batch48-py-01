# batch48-py-safety-d-12

## 题面

轮换脚本半夜把凭据文件截断重写，读方偶尔读到半份 JSON，解析失败后整批任务重试；还有人把凭据目录换成符号链接，进程以 root 身份跟进了攻击者准备好的目录，把伪造的凭据当成真的加载。那台机器同时开着三个轮换窗口，一份写坏的凭据被另一个进程读到，直到重启才被发现。事后排查发现读取前根本没有校验属主和权限位，写文件时也没有落盘和原子替换。范围是 gsb/batch48-py-safety-d-12 下的 secret_file_guard.py，类 SecretFileGuard，负责凭据文件的读取前校验与轮换写入。

公开接口：`SecretFileGuard(root, *, expected_uid: int = 0, expected_gid: int = 0, allow_follow_symlinks: bool = False, probe=None, max_secret_bytes: int = 8192)`；`rotate(name, payload) -> dict`；`read(name) -> dict`；`check(name) -> dict`；`version(name) -> int`；`list_names() -> list`；`remove(name) -> int`；`cleanup_temp() -> int`；`snapshot() -> dict`；`restore(state: dict) -> None`；`stats() -> dict`；`reset_stats() -> None`；`set_expected_owner(uid, gid) -> None`。

凭据文件名固定为 `<name>.secret`，name 只允许小写字母、数字、下划线与连字符，长度不超过 32；临时文件名为 `<name>.secret.tmp.<序号>`。元数据校验走注入的 probe(path)，返回 None 或包含 kind、mode、uid、gid 的字典，默认实现用本地 lstat；probe 让受限环境可以替换元数据来源，守卫本身不直接信任路径。rotate 接收字典 payload，序列化为紧凑 JSON 后写入；read 返回 payload、version 与 mode。

故障与边界：一、文件不存在抛 SECRET_NOT_FOUND；二、属主与基线不符抛 SECRET_OWNER_MISMATCH，属组不符抛 SECRET_GROUP_MISMATCH，报错而不是自动修改；三、权限位不等于 0600 抛 SECRET_MODE_TOO_OPEN，父目录对组或其他用户可写抛 SECRET_PARENT_INSECURE；四、名字非法抛 SECRET_NAME_INVALID，不得在磁盘上留下任何文件；五、payload 不是字典抛 SECRET_PAYLOAD_INVALID，序列化后超过 max_secret_bytes 抛 SECRET_PAYLOAD_TOO_LARGE；六、rotate 先在同目录写临时文件、把临时文件权限设为 0600、落盘后 rename 覆盖目标，临时文件与目标文件必须位于同一挂载点，读方只能看到完整旧文件或完整新文件，失败不得改动旧文件；七、rotate 成功返回 status、name、version、size、mode，版本在旧文件版本上加一；八、cleanup_temp 删除残留临时文件并返回条数，正常 rotate 结束后不得留下临时文件；九、check 返回 ok 与原因码而不抛异常；十、快照包含 names、versions、counters 三部分，不含 payload，restore 缺失任一部分抛 SECRET_SNAPSHOT_INVALID 且原状态不变；十一、stats 固定返回 rotations、reads、denied、temp_cleaned 四个整数，reset_stats 全部归零；每次拒绝计入 denied，成功读取才计入 reads。

硬约束：符号链接即使指向允许目录也一律拒绝，除非显式开启 allow_follow_symlinks；rotate、read、cleanup_temp 全程持锁；八路并发轮换同一文件时版本必须连续到 8，rotations 与调用次数相等，不得有两个线程拿到同一版本；快照必须是深拷贝，restore 后不得与快照共享可变结构；崩溃恢复后残留临时文件可以清理，但正式文件必须保持完整。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与崩溃恢复场景自行验证；只做本地实现，不涉及上游提交。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- difficulty: 地狱
- module: secret_file_guard
- typeName: SecretFileGuard
