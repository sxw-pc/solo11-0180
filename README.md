# 社区养老助餐补贴核销采录台

面向真实业务采录的离线样例项目，包名 `mealassist`。全部样例均为虚构身份和资源；运行不需要外部服务。

## 安装与运行

`python -m pip install -e . --no-deps`

`python -m mealassist init --db local.db`

`python -m mealassist demo --db local.db`

`python -m mealassist audit --db local.db`

`python -m unittest discover -s tests -v`

## 三个独立入口

### 资格与额度

`python -m mealassist eligibility --db eligibility.db --fixture fixtures/eligibility_normal.json`

失败证据：`fixtures/eligibility_failure.json`。资格期间和补贴档位决定可用餐补。持久化对象包括 长者资格、额度台账，领域模块位于 `mealassist/eligibility.py`。

### 供餐与领餐

`python -m mealassist service --db service.db --fixture fixtures/service_normal.json`

失败证据：`fixtures/service_failure.json`。订餐、备餐与实际领取决定有效餐次。持久化对象包括 菜单库存、领餐凭证，领域模块位于 `mealassist/service.py`。

### 核销与清算

`python -m mealassist settlement --db settlement.db --fixture fixtures/settlement_normal.json`

失败证据：`fixtures/settlement_failure.json`。有效餐次与价格版本决定补贴和商户应收。持久化对象包括 核销流水、清算批次，领域模块位于 `mealassist/settlement.py`。

### 纸票采入与全局分配

`python -m mealassist voucher --db voucher.db --fixture fixtures/ticket-assignment.json`

同一批纸票影像先经 OCR 采入，再按整批求唯一分配。`mealassist/ticket_scan.py` 只负责候选排序与发行册校验；置信分不是发行证明。`mealassist/voucher.py` 同时求解两条全局约束：同一发行券号至多配一张有效纸票，同一领餐至多核一券。发行册、领取人、手工签认和封袋来源全部入账，原图与旧分配版本只追加不覆盖。

`fixtures/ticket-assignment.json` 复现真实碰撞：T1 读成 A17(0.9)、A71(0.8)，T2 只读成 A17(0.95)，发行册确有 A17、A71 各一券。逐张贪心会先占 A17 并误判本批只能核一券；整批求解得到 T1→A71、T2→A17，共 30 元。

T1 待复核的 A17 手工签认决定可选协议：

- **全额核券协议**（签认人与发行方共同批准撤回）：T1→A71、T2→A17，商户待结 30 元。
- **保留签认协议**（签认未撤或处于封账期）：T1→A17、T2 未配券；商户仅可申报 10 元，`blocked_by` 标出阻断 T2 的 A17 冲突边，领取事实保留待下批人工处置。

`recompute` 可从分配版本内嵌的证据快照重算并核对结果。`settlement.py` 的 `redeem_paper` 只消费 `state='confirmed'` 的分配行：OCR 的逐张 `accepted` 不会生成商户应收，同一分配也不能被重复核销。


## 持久化与恢复

`events` 留原始 JSON、摘要、有效时刻和接收时刻。`subjects`、`documents`、`allocations`、`receipts` 留业务主体、版本、占位与回执；`decisions` 保存每次裁定。事件 ID 相同且摘要相同视为重试，摘要不同拒绝。每个命令在 SQLite 事务内更新事件、状态和决定，避免半写。`audit` 在独立内存数据库从原始事件重新运行规则，对照五类表，可发现被篡改的裁定。

## 业务能力

- **资格期间判定**：历史资格按供餐日而非入库日生效，改变最终业务可执行状态。

- **补贴额度占用**：并发预订不能超过月度额度，改变最终业务可执行状态。

- **资格追撤重算**：追溯撤销影响未结与已结清算，改变最终业务可执行状态。

- **备餐承诺**：订单与库存配给决定备餐量，改变最终业务可执行状态。

- **领餐身份核对**：代领授权与重复扫码影响有效餐次，改变最终业务可执行状态。

- **缺餐退款**：供餐中断释放额度与商户应收，改变最终业务可执行状态。

- **餐价版本核销**：价格生效日决定用户支付和补贴额，改变最终业务可执行状态。

- **清算差异冻结**：支付回执与餐次证据不符时冻结批次，改变最终业务可执行状态。

## 跨系统案例

- 长者供餐日资格被追溯撤销，领餐证据仍保留但补贴核销转为自费待追收并冻结商户清算差额。异常：资格事件晚到已结批次时须生成更正而非覆盖原结算。

- 助餐点缺餐导致核销退款，退款事件释放长者当月额度，后续订餐才可重新占用。异常：退款重试不能重复释放额度或产生负应收。

- 对账发现支付回执与核销餐次不符，清算冻结影响资格额度的最终消耗状态，领餐服务保留凭证等待人工复核。异常：对账回执迟到于下一次订餐时需区别预留与已核销额度。

## 验证范围与局限

自动测试覆盖身份冲突、状态传播、拒绝与恢复、幂等重放及审计篡改。fixture 展示正常与失败输入。纸票分配另有专项测试：单券基线、逐张贪心错失第二券、两个签认协议、重复与晚到影像保留历史，以及商户核款只消费确认分配。项目使用离线确定性规则和小规模虚构样例；尚未处理真实部门系统、真实证照签名或现场设备。
