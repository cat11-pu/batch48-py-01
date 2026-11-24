# batch48-py-safety-b-02

## 题面

沙箱集群这两周被磁盘告警淹没：一个作业把临时目录写到只剩几百兆，同节点的其他作业跟着失败；作业结束很久目录还留在盘上，配额却已经归还，值班分不清哪些目录能删；更麻烦的是有人用作业里的符号链接把宿主目录写穿。这批作业镜像里多数是写中间文件的批处理，几十个并发落在同一节点，配额、路径和清理三件事现在各做各的。范围是 gsb/batch48-py-safety-b-02 下的 sandbox_scratch_warden.py，类 ScratchWarden，负责按作业分配独立工作目录、计量用量、拒绝越界路径并在结束后回收。

公开接口：ScratchWarden(*, base_root="/srv/scratch", retention=5)；allocate(job_id, quota_mb=16, max_files=64, now=0)；resolve(job_id, relative_path)；link(job_id, relative_path, target)；charge(job_id, byte_count, path=None)；release(job_id, now=0)；sweep(now)；usage(job_id)；list_jobs()；orphans()；open_file(job_id, relative_path)；close_file(handle)；snapshot()；restore(state)。拒绝统一抛 ScratchError(code, detail)。

故障与边界：一、allocate 校验作业号形状，作业号必须以小写字母或数字开头、只含小写字母数字与下划线短横、长度不超过 64，quota_mb 与 max_files 为正整数，重复作业报 SCRATCH_DUPLICATE_JOB，quota_mb 传布尔或浮点也按非法处理，返回值含 job_id、root、quota_bytes、used_bytes、files、created_at；二、作业根固定为 /srv/scratch/<job_id>，不同作业的根与配额互不影响，一个作业写满不拖累邻居；三、resolve 只接受相对路径，空段与点段折叠，双点最多回退到作业根，越过根报 SCRATCH_PATH_ESCAPE，绝对路径同样按越界处理；四、非字符串或含 NUL 的路径报 SCRATCH_INVALID_PATH；五、作业内新建的符号链接必须在解析阶段就判定落点，目标落在工作根之外一律拒绝，不能拖到打开文件时才发现；六、charge 按字节累加，超过 quota_bytes 报 SCRATCH_QUOTA_EXCEEDED 且本次不落地，恰好等于配额允许；七、charge 带 path 时新路径计一个文件，超过 max_files 报 SCRATCH_FILE_LIMIT，本次的字节数与文件计数都不能落地；八、release 归还字节与文件并返回回收量，重复释放或未知作业报 SCRATCH_UNKNOWN_JOB，已释放作业再解析、再计量报 SCRATCH_JOB_RELEASED；九、sweep 只回收已释放且超过保留时长的作业，仍然活跃的一个都不能删，返回按作业号升序排好的列表；十、sweep 删除目录前必须确认没有进程还持有其中的文件句柄，直接删会让运行中的作业读到已经消失的路径；十一、快照保存作业表、链接表、已释放墓碑与观测时刻，非法结构报 SCRATCH_SNAPSHOT_INVALID 且原状态不变，恢复后残留作业要么能被认领要么能被下一次 sweep 清走；十二、list_jobs 按作业号升序，每项含 state、used_bytes、files，orphans 只列已释放但尚未清理的作业，回收动作要能被后续核对追上。

硬约束：同一作业的 charge 在并发下不得超卖配额，也不得丢记账，失败的那次不能留下半份文件计数；快照必须是深拷贝，恢复后不与调用方共享可变结构；配额与文件数都是硬顶，超限先报错再谈清理。

验收：`python3 -m pytest -q tests` 的 15 条测试全部通过；不得修改测试、跳过或按固定输入写死；并发超卖与重启清理场景自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 系统安全
- type: Bugfix
- module: sandbox_scratch_warden
- typeName: ScratchWarden
