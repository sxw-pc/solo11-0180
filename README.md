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

### 商户拨款与银行对账

核销后的应付单不直接向银行付款，而是先写本地付款意图，再按稳定业务键（应付单 ID）查询银行。银行接口 `mealassist/bank.py` 以稳定键去重：同一键重复提交返回原回执；同一键但收款人或金额不同的第二次提交记为 `conflict` 并保留原回执；冲正追加 `reversal` 交易，从不删除银行事实。`mealassist/payout.py` 负责意图、受理/最终结果、待定与恢复，`mealassist/storage.py` 增加 `payment_intents`、`payment_attempts`、`external_payments`、`budget_entries` 四张证据表。

状态含义：

- `transferred`：银行已完成，回填外部流水并本地确认应付单为 `settled`。
- `accepted` / `unknown`：结果未定，应付单转 `pending`，不允许再次提交，只能继续查询。
- `rejected`：银行明确拒绝，未动账，应付单解冻为 `open`，可用同一稳定键重试。
- `conflict`：金额、收款商户或回执归属不符，应付单冻结并保留原件等待人工复核。

恢复场景见 `fixtures/payout-recovery.json`，验收测试见 `tests/test_payout_recovery.py`。

### 同一应付单的两种获准清算路径

核销与标价输入相同，但外部收款人、实际拨款额、补贴预算和追索对象由来源凭证决定：

| 来源 | 站点实际付款 | 收款人 | 预算账户 | 追索对象 |
| --- | --- | --- | --- | --- |
| `city_subsidy` | 餐价 15 | 商户 `shop-A` | 城市补贴账户 15 | 商户 |
| `senior_reimbursement` | 补贴 10 | 长者 `senior-1` | 站点报销账户 10 | 长者 |

长者先付商户的路径必须先核验长者付款凭据；商户已收款，站点不再发起商户转账，同一凭据只能资助一笔应付单。付款意图按应付单唯一，两条路径不会互相重付。


## 持久化与恢复

`events` 留原始 JSON、摘要、有效时刻和接收时刻。`subjects`、`documents`、`allocations`、`receipts` 留业务主体、版本、占位与回执；`decisions` 保存每次裁定。事件 ID 相同且摘要相同视为重试，摘要不同拒绝。每个命令在 SQLite 事务内更新事件、状态和决定，避免半写。`audit` 在独立内存数据库从原始事件重新运行规则，对照五类表，可发现被篡改的裁定。

外部银行调用无法纳入 SQLite 事务，因此拨款另有三层追加式证据：`payment_intents`（每笔应付单一条本地意图，含稳定银行键与来源条款）、`payment_attempts`（每次查询/提交/落账/冻结的尝试记录）、`external_payments`（银行回执本地镜像，冲突回执原样保留）、`budget_entries`（预算分录，冲正写负数新分录）。`receipts` 仍是本地确认账，但不再是外部付款是否发生的唯一依据。

## 业务能力

- **资格期间判定**：历史资格按供餐日而非入库日生效，改变最终业务可执行状态。

- **补贴额度占用**：并发预订不能超过月度额度，改变最终业务可执行状态。

- **资格追撤重算**：追溯撤销影响未结与已结清算，改变最终业务可执行状态。

- **备餐承诺**：订单与库存配给决定备餐量，改变最终业务可执行状态。

- **领餐身份核对**：代领授权与重复扫码影响有效餐次，改变最终业务可执行状态。

- **缺餐退款**：供餐中断释放额度与商户应收，改变最终业务可执行状态。

- **餐价版本核销**：价格生效日决定用户支付和补贴额，改变最终业务可执行状态。

- **清算差异冻结**：支付回执与餐次证据不符时冻结批次，改变最终业务可执行状态。

- **拨款断点恢复**：银行已转出而本地未记账时，按稳定键查回原回执恢复，不产生第二笔付款，改变最终业务可执行状态。

- **拨款来源分流**：同一核销按城市补贴或长者报销凭证决定收款人、拨款额、预算和追索对象，改变最终业务可执行状态。

## 跨系统案例

- 长者供餐日资格被追溯撤销，领餐证据仍保留但补贴核销转为自费待追收并冻结商户清算差额。异常：资格事件晚到已结批次时须生成更正而非覆盖原结算。

- 助餐点缺餐导致核销退款，退款事件释放长者当月额度，后续订餐才可重新占用。异常：退款重试不能重复释放额度或产生负应收。

- 对账发现支付回执与核销餐次不符，清算冻结影响资格额度的最终消耗状态，领餐服务保留凭证等待人工复核。异常：对账回执迟到于下一次订餐时需区别预留与已核销额度。

## 验证范围与局限

自动测试覆盖身份冲突、状态传播、拒绝与恢复、幂等重放及审计篡改。fixture 展示正常与失败输入。项目使用离线确定性规则和小规模虚构样例；尚未处理真实部门系统、真实证照签名或现场设备。

`audit` 从事件重放资格、供餐和核销状态；商户拨款确认来自外部银行回执而非事件，因此由 `payout.trace` / `payout.recover` 按其自身证据链核对。

## 错误推演：断点后的常规队列重付

输入同上：`p1` 应付 15，`shop-A`。原副本在 `bank.transfer` 写盘后、本地落账前断线，`fail_after_bank=True` 抛出 `connection_lost_after_bank`，此时银行已有 `PAY1/transferred/15`，本地没有 payout，`p1` 仍为 `open`。

原实现的实际后果：常规任务队列只看 `p1` 为 `open`，便会执行 `pay(db,bank,'PAY2','shop-A','p1')`。旧 `bank.transfer` 直接 append，`bank.json` 于是同时存在 `PAY1` 与 `PAY2` 两笔各 15、合计 30；本地只给 `PAY2` 记 15 并把 `p1` 置为 `settled`。给本地 SQLite 加唯一索引或重放锁都挡不住，因为第一次付款已经在外部完成而本地毫不知情；即便把 `PAY1` 再次入队，旧 `bank.transfer` 没有幂等键，也会再转一次。

改造后的实际行为：本地意图以 `p1` 为稳定键先落库；`pay` 在提交前先查银行该键的最终状态。发现 `PAY1/transferred` 时回填对应外部流水、写唯一本地确认并拒绝 `PAY2` 重付（`tests/test_payout_recovery.py::test_retry_with_pay2_does_not_transfer_twice` 断言银行只有一笔 15）。若银行返回 `unknown`，应付单转 `pending`，`pay` 不推定未付、不放行第二笔，只允许 `recover` 继续查询；银行文件丢失时，本地镜像的未定回执仍会阻止再次提交。
