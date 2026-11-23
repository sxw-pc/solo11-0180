# 社区养老助餐补贴核销采录台

面向真实业务采录的离线样例项目，包名 `mealassist`。全部样例均为虚构身份和资源；运行不需要外部服务。

## 安装与运行

`python -m pip install -e . --no-deps`

`python -m mealassist init --db local.db`

`python -m mealassist demo --db local.db`

`python -m mealassist audit --db local.db`

`python -m unittest discover -s tests -v`

## 四个独立入口

### 语音订餐采录

`python -m mealassist demo --db voice.db --fixture fixtures/voice-orders.json`

电话订餐录音按片段采录：时间轴、说话人、N-best 转写、所指旧订单、修正/分拆/撤销动作与被转赠人确认并列保存（`segments`）。份额级谱系（`shares`/`lineage`）保证每份餐只有一个当前归属人，每次修正指向确切旧录音与旧份额，拆分前后总餐数守恒，取消才释放库存；没有明确所指订单的片段先悬挂。只有经确认的逐人餐数汇入 `service.prepare` 的 orders（`source: voice-confirmed`），领取凭据指向具体份额，晚到更正不悄改已备餐承诺，差异由 `service.commitment_drift` 显式呈现。领域模块位于 `mealassist/order_capture.py`，基线 `capture` 行为由 `tests/test_voice_seed.py` 固定。

### 资格与额度

`python -m mealassist eligibility --db eligibility.db --fixture fixtures/eligibility_normal.json`

失败证据：`fixtures/eligibility_failure.json`。资格期间和补贴档位决定可用餐补。持久化对象包括 长者资格、额度台账，领域模块位于 `mealassist/eligibility.py`。

### 供餐与领餐

`python -m mealassist service --db service.db --fixture fixtures/service_normal.json`

失败证据：`fixtures/service_failure.json`。订餐、备餐与实际领取决定有效餐次。持久化对象包括 菜单库存、领餐凭证，领域模块位于 `mealassist/service.py`。

### 核销与清算

`python -m mealassist settlement --db settlement.db --fixture fixtures/settlement_normal.json`

失败证据：`fixtures/settlement_failure.json`。有效餐次与价格版本决定补贴和商户应收。持久化对象包括 核销流水、清算批次，领域模块位于 `mealassist/settlement.py`。

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

自动测试覆盖身份冲突、状态传播、拒绝与恢复、幂等重放及审计篡改。fixture 展示正常与失败输入。项目使用离线确定性规则和小规模虚构样例；尚未处理真实部门系统、真实证照签名或现场设备。
