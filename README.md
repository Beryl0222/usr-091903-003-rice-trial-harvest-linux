# 水稻试验收获隔离

永丰村试验收获后端：维护**试验方案（地块边界 / 品种代次 / 播种批次 / 取样方案）→ 七步田间作业 → 封样入库 → 实验室样本谱系**的全链路，重点解决品种混杂风险：联合收割机若未按要求清仓，相邻品种可能混入研究样本。

## 四条核心原则

1. **冻结不可变**：方案一旦冻结，只能读取，重复冻结返回 `409 PLOT_FROZEN`，任何角色都不能覆盖。
2. **事件只追加**：成熟度复核、机器清洁、首段弃粮、正式收获、称重、封样、入库七个阶段按实际发生时间追加，系统不提供更新/删除接口。
3. **冲突不覆盖**：定位漂移、离线补传、封签重复扫描、顺序缺失/倒挂、阶段重复，一律把原始记录隔离（`quarantined`）并生成带解释的冲突单，由研究负责人审核 `accept`/`reject` 后才有结论；驳回记录保留为 `rejected`，永不被静默改写。
4. **谱系不中断**：样本转交实验室后的重分装、耗用、作废全部追加到原始谱系，所有派生子样本回挂最初的试验 / 地块 / 封签根样本。

## 运行

```bash
python3 service.py --check          # 自检
python3 service.py --port 8000      # 启动，GET /health 返回服务身份
npm test                            # 契约测试（基础服务 + 端到端领域场景）
```

## 角色（最小可见）

| 角色 | 可见/可做 |
|---|---|
| `admin` | 冻结方案、代行上报、审核冲突、全部查询 |
| `lead` 研究负责人 | 查看方案与事件流水、审核冲突、异常反查、按方案重建产量与样本、转交样本 |
| `operator` 机手 | 只看自己的作业卡 / 待作业队列（不含品种、代次等研究信息）、上报作业、按客户端事件号查自己的补传结果 |
| `lab` 实验室 | 仅操作**已转交**样本：重分装、耗用、作废、查谱系 |

演示令牌（`Authorization: Bearer <token>`）：`admin-token`、`lead-token`、`lab-token`、`op01-token`、`op02-token`。生产环境应由网关注入身份。

## 作业七阶段与必录字段

`maturity_review`（成熟度复核 `maturity_grade`）→ `machine_clean`（机器清洁，机器号+GPS）→ `head_discard`（首段弃粮，机器号+GPS）→ `harvest`（正式收获，机器号+GPS）→ `weighing`（称重 `weight_kg`、`moisture_pct`）→ `sealing`（封样 `seal_code`）→ `storage`（入库 `bin_code`）。

每个事件都要带 `occurred_at`（ISO8601，设备实际发生时间），可选 `client_event_id`（设备本地事件号，用于**幂等重传**：同号重放返回原记录，绝不产生第二条）。

- 上报即时通过返回 `200 {"accepted": true}`；
- 进入冲突隔离返回 `202 {"accepted": false, "conflicts": [...]}`。

## 冲突类型（可解释、可审核）

| 代码 | 触发条件 | 放行规则 |
|---|---|---|
| `GPS_DRIFT` | GPS 坐标不在所填地块边界内（射线法+外接框）；会反查坐标实际落在哪个相邻地块 | 人工核对后放行或驳回 |
| `OFFLINE_LATE` | 上传时间晚于发生时间超过 300 秒（离线补传） | 核对轨迹后放行 |
| `SEAL_DUPLICATE` | 封签已被其他记录使用 | 若原占用样本已生效，`accept` 被系统强制拒绝（`409 SEAL_STILL_CLASHING`），只能换签重报 |
| `STAGE_DUPLICATE` | 该地块此阶段已有生效记录 | 通常驳回 |
| `ORDER_GAP` | 前置阶段缺失或时间相对已生效记录倒挂 | 补齐前置后重报 |

冲突单只增不改，审核结果（决策人、时间、备注）永久留痕，已结案冲突不能二次审核。

## 主要接口

```
POST   /admin/plots                          冻结方案
GET    /admin/trials/<trial>                 查看冻结方案
POST   /plots/<plot>/events                  上报作业事件
GET    /plots/<plot>/events                  地块事件流水（lead/admin）
GET    /plots/<plot>/work-card               机手作业卡（仅下一步动作）
GET    /operator/queue                       机手待作业队列
GET    /client-events/<id>                   按设备事件号查补传结果（仅本人）
GET    /conflicts?status=&plot=&machine=     冲突单列表
POST   /conflicts/<id>/resolve               审核 {decision: accept|reject, note}
GET    /conflicts/<id>/machine-prior-plots   异常反查：同机当天此前经过的田块
GET    /machines/<machine>/trace?date=YYYY-MM-DD  机器当日轨迹
GET    /trials/<trial>/results               重建有效产量与样本清单
POST   /samples/<code>/transfer              转交实验室 {carrier, lab_code, at}
POST   /samples/<code>/split                 重分装 {parts:[{quantity_kg,purpose}]}
POST   /samples/<code>/consume               耗用 {quantity_kg, purpose}
POST   /samples/<code>/void                  作废 {reason}
GET    /samples/<code>/lineage               整族谱系（含已作废）
```

## 有效产量重建

`GET /trials/<trial>/results` 只统计收获/称重/封样**均有生效记录**的小区：

- `weight_kg` 为实测湿重；`standard_weight_kg` 按标准含水率 13.5% 折算：
  `标准重 = 湿重 × (100 − 实测含水率) / (100 − 13.5)`；
- 无效小区给出 `invalid_reasons`（如"缺少封样生效记录"），不会悄悄进入产量汇总；
- `samples` 为该方案的有效样本清单（默认隐藏已作废子样本，但谱系查询中仍完整可见）。

## 快速试一遍

```bash
python3 service.py --port 8000 &
curl -s localhost:8000/health
curl -s -X POST localhost:8000/admin/plots -H 'Authorization: Bearer admin-token' \
  -H 'Content-Type: application/json' -d '{
    "code":"P-A","trial_code":"T-2026-R01","variety_code":"V-7","generation":"F6",
    "sowing_batch":"B-0918",
    "boundary":[[103.91,30.12],[103.92,30.12],[103.92,30.13],[103.91,30.13]],
    "sampling_plan":{"method":"five_point","bags":3}}'
```

完整的"冻结→七步作业→漂移/重复封签/离线补传冲突→审核→封样入库→实验室分装耗用作废→产量重建→轨迹反查"场景见 `domain_contract.py`。
