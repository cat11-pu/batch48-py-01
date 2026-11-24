# batch48-py-game-a-33

## 题面

拍卖行改版后资金开始对不上账：同一笔保证金被冻结两次，最后一秒出价没有把倒计时往后推，同一场拍卖结算了两遍，赢家账户被扣了两次钱，卖家撤销一场已经有人出价的拍卖后买家还在等货，无人出价的挂单还占着卖家的钱，客服按出价时间逐笔核对，发现同一笔出价被记了两次账，每笔冻结都要能追溯到具体拍卖。问题集中在 gsb/batch48-py-game-a-33 的 auction_settlement_guard.py，类 AuctionSettlementGuard 负责余额、挂单、出价、关闭、结算与撤销。

公开接口：AuctionSettlementGuard(*, guarantee_rate=0.1, extension_seconds=30, max_duration=86400)；deposit(player, amount) -> dict；withdraw(player, amount) -> dict；balance(player) -> int；list_item(seller, item, reserve, duration, now=0) -> dict；bid(bidder, auction_id, amount, now=0) -> dict；close(auction_id, now=0) -> dict；settle(auction_id, now=None) -> dict；cancel(seller, auction_id) -> dict；auction_state(auction_id) -> dict；bids(auction_id) -> list；active(now=0) -> list；snapshot() -> dict；restore(state) -> None。拒绝统一抛 AuctionError(code, detail)；list_item 返回 auction_id、guarantee、status 与 close_at，bid 返回 count，cancel 返回 refund，settle 返回 winner、price 与 status，close 返回 status 且只改状态不动资金，active 返回仍在开放的拍卖标识列表；auction_state 返回 auction_id、seller、item、reserve、close_at、status、guarantee、bid_count、highest_amount 与 highest_bidder。

故障与边界：一、挂单只冻结一次保证金，撤销后原额退回，重复撤销抛 AUCTION_CANCELLED 且不再动钱；二、已有出价的拍卖禁止卖家撤销，抛 AUCTION_HAS_BIDS，非卖家撤销抛 AUCTION_NOT_OWNER；三、最后一秒出价要把截止时间按固定时长延后，且同场拍卖只延一次；四、出价必须不低于保留价且严格高于当前最高价，否则抛 AUCTION_BELOW_RESERVE 或 AUCTION_BID_TOO_LOW，被拒的出价不入榜；五、已到期或已关闭的拍卖不能再出价抛 AUCTION_CLOSED，未知拍卖一律抛 AUCTION_UNKNOWN；六、结算把最高出价判给赢家、款项转给卖家并退回保证金，出价榜按金额降序、同额按时间戳升序排列，保留价未达到时整场流拍；七、同一拍卖重复结算抛 AUCTION_ALREADY_SETTLED 且不产生第二笔转账；八、无人出价时结算原样退还保证金、不扣任何人；九、未到期又未手动关闭时结算抛 AUCTION_NOT_DUE，不传 now 时同样只接受已关闭的拍卖，状态不因失败尝试改变；十、赢家余额不足时结算整次失败、状态与资金不变，余额不足与存取金额非正分别抛 AUCTION_INSUFFICIENT_FUNDS 与 AUCTION_INVALID_AMOUNT；十一、结算与撤拍并发时只能有一个成功，失败方不得产生任何资金变动，也不得改变拍卖状态；十二、快照返回 version 为 1 的字典，含 balances 与 auctions 两段，结构非法抛 AUCTION_SNAPSHOT_INVALID 且原状态不变。

硬约束：结算原子完成扣款、转移与退款，中途失败不得留下半套账；同一拍卖的并发出价要串行记账，金额、时间戳与序号不得错位；保证金与余额之和在任意时刻守恒，撤拍与结算都不得凭空创造或销毁资金，出价不预扣资金、只在结算时从赢家余额扣款。

验收：python3 -m pytest -q tests 的 15 条测试必须全过，不得改测试、跳过或按固定输入写死；最后一秒延时与撤拍竞态自行验证；只做本地实现，接口名与错误码保持上面列出的写法，资金流向与错误码一致。

## 运行方式

```bash
python3 -m pytest -q tests
```

## 平台字段

- domain: 游戏、图形与多媒体
- sub: 游戏开发
- type: Bugfix
- difficulty: 困难
- module: auction_settlement_guard
- typeName: AuctionSettlementGuard
