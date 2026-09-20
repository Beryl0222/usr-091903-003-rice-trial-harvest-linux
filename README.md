# 水稻试验收获隔离（rice-trial-harvest）

永丰村六千余亩水稻集中收获期的**试验收获后端**。在联合收割机频繁转场、相邻品种小区
密集分布、田间设备经常离线的条件下，保证：

1. **预先冻结**：地块边界、品种代次、播种批次、取样方案在作业开工前冻结成快照，
   之后的任何读数都以快照为准，方案不能被中途改写；
2. **按实记录**：成熟度复核 → 机器清洁（清仓）→ 首段弃粮 → 正式收获 → 称重 →
   封样 → 入库，七段作业严格按实际发生顺序追加记录，缺段/跳段不得计入有效产量；
3. **冲突不覆盖**：卫星定位漂移/越界、设备离线补传乱序、同一封签重复扫描等情况，
   原始上报完整保留并进入隔离区，生成**带中文解释、严重级别和关联证据**的冲突单，
   由研究负责人裁决（采信须填写处置说明，高危冲突限负责人/管理员）；
4. **谱系不断**：样本封样后转交实验室，重分装（拆分）、耗用、作废全部挂接在原始
   谱系节点上，任意子样本可回溯到原田块、品种代次、播种批次与作业；
5. **按需可见**：研究负责人按试验方案重建有效产量与样本清单、从异常反查机器当天
   此前经过的田块；普通机手只能看到自己当前作业的进度、下一步与挂起异常。

纯 Python 标准库实现，数据落在本地只追加 JSONL 文件，无需外部依赖。

## 运行

```bash
python3 service.py --check            # 配置自检（首次运行会在数据目录播种用户）
python3 service.py --port 8000        # 启动服务
curl http://localhost:8000/health
```

数据目录默认 `./data`，可用 `--data-dir` 或环境变量 `RHT_DATA_DIR` 指定。
首次启动生成四个演示角色的 Bearer 令牌并打印一次，同时写入
`data/users.json`（权限 600）：

| 角色 | 用户 | 能力 |
|---|---|---|
| `admin` | u_admin | 全部能力 |
| `researcher` | u_lead | 冻结方案、登记作业、裁决冲突、重建产量、轨迹反查 |
| `lab` | u_lab | 样本转交后的重分装/耗用/作废 |
| `operator` | u_driver | 上报自己作业的阶段事件、查看最小作业视图 |

## 主要接口

除 `GET /health` 外均需 `Authorization: Bearer <token>`。

```
POST /v1/plans                             冻结试验方案
GET  /v1/plans/{code}                      查看方案（机手只见骨架）
POST /v1/operations                        登记收获作业（冻结快照入作业）
POST /v1/operations/{id}/events            田间阶段事件上报（见下）
GET  /v1/operations/{id}/events            作业事件流水
GET  /v1/operations/{id}/brief             机手最小视图
GET  /v1/me/work                           当前机手的作业清单
GET  /v1/conflicts?status=open             冲突队列
POST /v1/conflicts/{id}/resolve            裁决 {action: accept|reject, note}
GET  /v1/conflicts/{id}/trace              从异常反查机器当天此前经过的田块
GET  /v1/machines/{machine_id}/stops?day=  机器停靠轨迹
GET  /v1/plans/{code}/rebuild              重建有效产量与样本清单
GET  /v1/samples/{sample_code}             样本谱系视图
POST /v1/samples/{code}/transfer           实验室转交登记
POST /v1/samples/{code}/split              重分装（items 列表，守恒校验）
POST /v1/samples/{code}/consume            耗用（不得超过余量）
POST /v1/samples/{code}/void               作废（存在活跃子样时先处置子样）
```

### 阶段事件上报

```json
POST /v1/operations/op_xxx/events
{
  "stage": "harvest",
  "occurred_at": "2026-09-20T08:15:00Z",
  "client_event_id": "tablet-7-00042",
  "payload": {
    "machine_id": "M-7",
    "gps": {"lng": 104.021, "lat": 30.0101, "accuracy_m": 8}
  }
}
```

* 同一 `client_event_id` + 同一载荷重放返回 `200 replay`，幂等不新增；
  同号不同载荷判定为 `duplicate_event` 冲突；
* 立即生效返回 `201`；进入隔离区返回 `202`，载荷形如
  `{"outcome": "quarantined", "event": {...}, "conflicts": [...]}`；
* 各阶段必填：成熟度复核 `approved`；机器清洁 `cleaned=true`；首段弃粮
  `discarded_weight_kg`；称重 `net_weight_kg>0`；封样 `seal_code`+`sample_code`；
  入库 `storage_bin`。

## 冲突规则与裁决语义

| 代码 | 触发 | 级别 |
|---|---|---|
| `gps_low_accuracy` | 定位精度 > 30 m | medium |
| `gps_drift` | 坐标在地块边界 15 m 容差带内 | medium |
| `gps_outside_plot` | 坐标远离冻结地块（疑似误入相邻小区） | high |
| `late_arrival` | 离线补传事件的发生时间早于已接收事件 | medium |
| `stage_skip` | 前置阶段未确认就上报后续阶段 | medium |
| `stage_repeated` | 同一阶段重复上报（不能覆盖原记录） | high |
| `plot_not_released` | 成熟度复核未放行即收获 | high |
| `machine_mismatch` | 收获机器与作业登记机器不一致 | high |
| `seal_already_scanned` | 封签已被其他样本绑定 | high |
| `duplicate_sample_code` | 样本编号冲突 | high |
| `duplicate_storage_bin` | 入库货位已被占用 | medium |
| `duplicate_event` | 同客户端事件号携带不同内容 | high |

裁决规则：

* **驳回即否决**：事件标记 `rejected`，同事件上的其他未决冲突级联关闭；
  收获及之前阶段被驳回会终止作业（`aborted`，品种隔离优先），同一地块可重新
  登记作业从头记录；
* **全部采信才生效**：一个事件挂多张冲突单时，须全部 `accept`；仅因 `stage_skip`
  挂起的后续事件会在前置阶段采信后**自动续链**（以 `auto_accept` 裁决留痕）；
* 高危冲突的采信限 `researcher`/`admin`，且任何采信都必须填写处置说明。

## 产量重建语义

`GET /v1/plans/{code}/rebuild` 只统计链路完整（复核→清洁→弃粮→收获→称重→封样，
入库单独标记）、样本未作废、无未决冲突的作业，输出每地块净重、折亩产量
（kg/亩，面积由冻结多边形计算）、在库/分装样本清单与余量；不合规地块进入
`excluded` 并注明原因（无作业 / 链路缺失阶段 / 样本作废 / 未决冲突），
绝不静默丢弃。

## 数据与可靠性

`data/*.jsonl` 为只追加事件流（plans / operations / events / conflicts /
resolutions / samples / sample_actions / op_status），写入串行化并 fsync；
服务重启后整体重放，事件生效状态由裁决记录派生。域层只依赖存储的
`all/append/next_seq` 三个方法，可替换为消息流实现。

## 测试

```bash
npm test          # 等价于 python3 -m unittest -v service_contract test_domain test_api
```

覆盖：冻结快照、七段正常链路、跳段/重复/未放行/机器不符、GPS 精度/漂移/越界、
离线补传、封签重扫、幂等重放、冲突级联与自动续链、作业终止与重开、样本
拆分守恒/耗用/作废/谱系回溯、产量重建排除清单、机器轨迹反查、重启恢复、
RBAC 与机手最小视图。
