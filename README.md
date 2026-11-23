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

## 配送轨迹重建与里程证书

`fixtures/delivery-route.json` 给出离线 GPS 配送证据：道路 A 仓到站直行 10km，平行道路 B 仅经单向连接道 C、D 进出（A→C→B→D→A 共 14km），09:00 离仓、09:40 到站、限速 30km/h。三个 GPS 原包的单点最近道路仍是 A/B/A，中点原包误差椭圆同时覆盖 A（6m）与 B（2m）。

- `mealassist/gps.py` 保留基线 `snap`（单点最近道路），新增原包 SHA-256 摘要、设备 HMAC 验签、采样时钟偏移/漂移检核、误差椭圆相容道路与去噪。
- `mealassist/route_capture.py` 在有向道路图上枚举路径并应用路口转向限制，以误差椭圆做道路相容、以时钟和限速做相邻点时间/速度检核，保留**全部**相容候选路径及其里程、到站时间和所需签认；证据不足时只给歧义报告，不按最高似然选路。
- `naive_pipeline` 复现“按点取最近路并把相邻标签直连”的普通管道：它把图上不存在的 A→B 跳路判为拓扑无效并标记遗漏 C/D，但仍会按原包直线记约 10km，作为责任核查对照，不产生证书。
- 证书只能由核实证据签发：司机与交接站双签的 B 路停靠 → 14km 证书（C/B/D 成立，中途与站点交接均可认领）；相机帧加车辆里程表证明 GPS 向 B 漂移 → 直行 A 的 10km 证书（B 上停靠不得当交接）。两路证书都保留同一组原始 A/B/A 标签、GPS 原包与司机签名。
- `route_corrections` 只追加更正（如晚到证据），不覆盖原证书、原 GPS 与签名；`service.pickup` 新增 `delivery` 凭据门，只有 `delivery_handovers` 中 `verified` 且主体、餐盒匹配的到站交接才可认领，不带该字段的既有领餐流程不变。

## 跨系统案例

- 长者供餐日资格被追溯撤销，领餐证据仍保留但补贴核销转为自费待追收并冻结商户清算差额。异常：资格事件晚到已结批次时须生成更正而非覆盖原结算。

- 助餐点缺餐导致核销退款，退款事件释放长者当月额度，后续订餐才可重新占用。异常：退款重试不能重复释放额度或产生负应收。

- 对账发现支付回执与核销餐次不符，清算冻结影响资格额度的最终消耗状态，领餐服务保留凭证等待人工复核。异常：对账回执迟到于下一次订餐时需区别预留与已核销额度。

## 验证范围与局限

自动测试覆盖身份冲突、状态传播、拒绝与恢复、幂等重放及审计篡改。fixture 展示正常与失败输入。项目使用离线确定性规则和小规模虚构样例；尚未处理真实部门系统、真实证照签名或现场设备。
