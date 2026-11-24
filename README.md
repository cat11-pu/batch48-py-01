# batch48-py-safety-d-06

## 题面

从库集群更换签名密钥的那次变更之后，主从同步连续三天整段失败：主库发出的区域传送应答被从库一律丢弃，值班同事抓包才看清根因——从库返回的 NOTAUTH 错误响应本身没有签名，主库校验不过就把整条 TCP 连接掐断了；同一批查询在重试链路上绕回来时又被当成新请求重新处理了一次；轮换当天新旧两把密钥同时在用，从库只认旧密钥，新密钥签出的更新全部被退回。另外还有两起未归档的异常：弱摘要算法签出的记录被当成合法记录放行，以及重启之后密钥代次与重放记录一起丢失。上周演练里两台机器时钟相差四分多钟，返回的却是 MAC 校验失败，排查方向被带偏了很久。

范围是 gsb/batch48-py-safety-d-06 下的 dns_tsig_chain_guard.py，类 DnsTsigChainGuard，负责 TSIG 记录的规范化签名、密钥表与轮换窗口、时间偏移判定以及重放拦截。

对外方法：`DnsTsigChainGuard(*, clock_skew: int = 300, replay_window: int = 600, max_keys: int = 32, max_replay: int = 4096, clock=None)`；`register_key(key_name: str, secret: bytes, *, algorithm: str = "hmac-sha256", now=None) -> dict`；`begin_rotation(key_name: str, new_secret: bytes, *, algorithm: str = "hmac-sha256", overlap_seconds: int = 600, now=None) -> dict`；`retire_key(key_name: str, generation: int, *, now=None) -> None`；`active_keys(key_name: str, *, now=None) -> list[dict]`；`sign_message(message: bytes, *, key_name: str, generation: int | None = None, fudge: int = 300, now=None) -> dict`；`verify_tsig(message: bytes, tsig: dict, *, now=None) -> dict`；`verify_and_sign_error(message: bytes, tsig: dict, *, now=None) -> dict`；`purge_replay(*, now=None) -> int`；`snapshot() -> dict`；`restore(state: dict) -> None`；`stats() -> dict`；`reset_stats() -> None`。拒绝统一抛 DnsTsigError(code, detail)。

故障与边界：一、TSIG 记录必须带 key_name、algorithm、generation、timestamp、fudge、mac 六个字段，timestamp 取签名时传入的 now，mac 为 HMAC-SHA256 的十六进制字符串，签名覆盖去掉 TSIG 后的原始消息与规范化字段串，记录不是字典或缺字段抛 BADFORM；二、key_name 不在密钥表、或该代次已退役抛 BADKEY；三、消息或 MAC 被改动抛 BADSIG；四、timestamp 与本地时钟偏差超过 clock_skew 抛 BADTIME，恰好等于 clock_skew 放行；五、同一 key_name、timestamp、mac 三元组在 replay_window 内再次出现抛 REPLAY，rejected 计数包含 REPLAY；六、begin_rotation 产生新一代密钥，双活窗口内新旧两代都能签名和验签，窗口结束后旧代立即拒为 BADKEY；七、窗口边界按 now 等于 overlap_until 时仍放行，超过一秒即拒绝；八、同一 key_name 与 secret 重复登记幂等、代次不变，密钥代次从 1 开始递增，换 secret 而未走轮换抛 KEYCONFLICT；九、密钥表上限 max_keys，满后拒绝新 key_name 抛 KEYLIMIT；十、验证时算法必须与登记代次一致，弱摘要降级即使密钥匹配也不接受；十一、BADKEY、BADSIG、BADTIME 三种失败都要能生成带签名的错误响应，供对端正常校验而不是整段丢弃；十二、快照覆盖密钥表与重放缓存，恢复后仍能按原代次验签，结构非法抛 STATE_INVALID 且不改动现存状态，恢复失败时不得清空已有密钥或重放记录。

接口返回约定：verify_tsig 成功返回 status 为 ok、key_name、generation 与 signed 为 True；register_key 与 begin_rotation 返回含 key_name、generation 的记录，轮换记录另带 overlap_until；active_keys 按代次升序返回 generation 与 algorithm；校验顺序固定为 BADFORM、BADKEY、BADSIG、BADTIME、REPLAY，前一步通过才进入下一步。

硬约束：登记、轮换、签名与校验全程持锁，八路并发提交同一签名记录时只能有一个成功，其余必须得到 REPLAY，计数器不得多算；重放缓存按 replay_window 滚动回收，禁止无界增长；snapshot 必须是深拷贝，恢复后的对象不得与原状态共享可变结构；签名与错误响应生成共用同一把锁，避免轮换瞬间签出半旧半新的记录。stats 固定返回 signed、verified、rejected、replayed 四个整数，reset_stats 归零。

验收：`python3 -m pytest -q tests` 的 15 条测试必须全部通过；不得修改测试、跳过或按固定输入写死；并发与重启恢复自行验证；只做本地实现。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 安全
- sub: 网络与通信安全
- type: Bugfix
- difficulty: 地狱
- module: dns_tsig_chain_guard
- typeName: DnsTsigChainGuard
