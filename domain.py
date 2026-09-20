"""水稻试验收获后端的领域核心。

设计要点
========
* **冻结在先**：试验方案（地块边界、品种代次、播种批次、取样方案）一经创建即冻结，
  作业开工时把冻结数据快照进作业，后续任何读数都以快照为准。
* **事件溯源**：成熟度复核 → 机器清洁 → 首段弃粮 → 正式收获 → 称重 → 封样 → 入库，
  每个阶段事件只追加、不修改；有效状态全部由“已接受”的事件重放得到。
* **冲突不覆盖**：卫星定位漂移/越界、设备离线补传乱序、同一封签重复扫描等情况，
  事件进入隔离区并生成带原因的冲突单，由负责人裁决后才生效，全程留痕。
* **样本谱系**：封样产生样本，转交实验室后的重分装（拆分）、耗用、作废全部挂在
  原始谱系节点上，可从任一子样本回溯到原田块与作业。
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 常量与枚举
# ---------------------------------------------------------------------------

STAGES = [
    "maturity_check",      # 成熟度复核
    "machine_clean",       # 机器清洁（清仓）
    "first_grain_discard", # 首段弃粮
    "harvest",             # 正式收获
    "weighing",            # 称重
    "sealing",             # 封样
    "storage",             # 入库
]
STAGE_NAMES_CN = {
    "maturity_check": "成熟度复核",
    "machine_clean": "机器清洁",
    "first_grain_discard": "首段弃粮",
    "harvest": "正式收获",
    "weighing": "称重",
    "sealing": "封样",
    "storage": "入库",
}

# 冲突代码 -> （严重级别, 人话解释）
SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"
CONFLICT_INFO = {
    "gps_low_accuracy": (
        SEVERITY_MEDIUM,
        "卫星定位精度过差，坐标可能存在漂移，需核对作业位置后采信",
    ),
    "gps_drift": (
        SEVERITY_MEDIUM,
        "上报坐标落在地块边界容差带内，判定为卫星定位漂移而非错地块",
    ),
    "gps_outside_plot": (
        SEVERITY_HIGH,
        "上报坐标远离冻结地块边界，机器可能误入相邻品种小区",
    ),
    "late_arrival": (
        SEVERITY_MEDIUM,
        "设备离线补传：事件发生时间早于已接收事件，按补传处理而不覆盖原记录",
    ),
    "stage_skip": (
        SEVERITY_MEDIUM,
        "作业阶段跳跃：前置阶段尚未确认，防止跳过清仓或首段弃粮",
    ),
    "stage_repeated": (
        SEVERITY_HIGH,
        "同一作业阶段被重复上报，重新提交不能覆盖既有记录",
    ),
    "plot_not_released": (
        SEVERITY_HIGH,
        "成熟度复核未放行该地块，不得进入正式收获",
    ),
    "machine_mismatch": (
        SEVERITY_HIGH,
        "收获机器与作业登记机器不一致，无法保证清仓隔离链可追溯",
    ),
    "seal_already_scanned": (
        SEVERITY_HIGH,
        "该封签已被其他样本扫描登记，重复扫描不能重复绑定",
    ),
    "duplicate_sample_code": (
        SEVERITY_HIGH,
        "样本编号已被使用，样本编号必须全局唯一",
    ),
    "duplicate_storage_bin": (
        SEVERITY_MEDIUM,
        "入库货位已有在库样本，需确认是否混放",
    ),
    "duplicate_event": (
        SEVERITY_HIGH,
        "同一客户端事件号携带了不同内容再次上传，疑似重复提交冲突",
    ),
}

MU_PER_SQUARE_METER = 1.0 / 666.6667
GPS_TOLERANCE_M = 15.0   # 边界容差带：米
GPS_LOW_ACCURACY_M = 30.0


class DomainError(Exception):
    """领域规则拒绝（非冲突类），code 用于 API 错误信封。"""

    def __init__(self, code, message, status=409, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def _id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def parse_ts(value):
    """把 ISO8601（含结尾 Z）归一化为 UTC aware datetime。"""
    if not isinstance(value, str):
        raise DomainError("invalid_timestamp", "时间必须为 ISO8601 字符串", 400)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainError("invalid_timestamp", f"无法解析时间 {value!r}", 400) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def payload_hash(payload):
    raw = json_dumps(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 几何：多边形包含、距离、面积（等距圆柱投影，田块尺度足够精确）
# ---------------------------------------------------------------------------

EARTH_RADIUS_M = 6371000.0


def _meters_per_degree(lat):
    lat_rad = math.radians(lat)
    return (
        EARTH_RADIUS_M * math.radians(1) * math.cos(lat_rad),  # x / 经度
        EARTH_RADIUS_M * math.radians(1),                      # y / 纬度
    )


def point_in_polygon(lng, lat, polygon):
    """射线法判点是否在多边形内（含边界）。polygon: [[lng, lat], ...]。"""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        intersects = ((yi > lat) != (yj > lat)) and (
            lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def point_to_segment_m(lng, lat, a, b):
    """点到线段距离（米），在当地平面近似坐标中计算。"""
    lat0 = lat
    mx, my = _meters_per_degree(lat0)
    px, py = lng * mx, lat * my
    ax, ay = a[0] * mx, a[1] * my
    bx, by = b[0] * mx, b[1] * my
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def distance_to_polygon_m(lng, lat, polygon):
    if point_in_polygon(lng, lat, polygon):
        return 0.0
    return min(
        point_to_segment_m(lng, lat, polygon[i], polygon[i - 1])
        for i in range(len(polygon))
    )


def polygon_area_mu(polygon):
    """多边形面积（亩），围绕形心的平面近似。"""
    if len(polygon) < 3:
        return 0.0
    avg_lat = sum(p[1] for p in polygon) / len(polygon)
    mx, my = _meters_per_degree(avg_lat)
    area_sq = 0.0
    for i in range(len(polygon)):
        x1, y1 = polygon[i][0] * mx, polygon[i][1] * my
        x2, y2 = polygon[i - 1][0] * mx, polygon[i - 1][1] * my
        area_sq += x1 * y2 - x2 * y1
    return abs(area_sq) / 2.0 * MU_PER_SQUARE_METER


# ---------------------------------------------------------------------------
# 参数校验小工具
# ---------------------------------------------------------------------------


def require(body, fields, label="请求体"):
    if not isinstance(body, dict):
        raise DomainError("invalid_body", f"{label}必须是对象", 400)
    missing = [f for f in fields if body.get(f) in (None, "")]
    if missing:
        raise DomainError(
            "missing_fields", f"{label}缺少必填字段: {', '.join(missing)}", 400
        )


def validate_gps(payload_gps):
    if payload_gps is None:
        return None
    if not isinstance(payload_gps, dict):
        raise DomainError("invalid_gps", "gps 必须是对象", 400)
    try:
        lng = float(payload_gps["lng"])
        lat = float(payload_gps["lat"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainError("invalid_gps", "gps 需要数值型 lng/lat", 400) from exc
    if not -180 <= lng <= 180 or not -90 <= lat <= 90:
        raise DomainError("invalid_gps", "经纬度超出合法范围", 400)
    accuracy = payload_gps.get("accuracy_m")
    if accuracy is not None:
        try:
            accuracy = float(accuracy)
        except (TypeError, ValueError) as exc:
            raise DomainError("invalid_gps", "accuracy_m 必须为数值", 400) from exc
        if accuracy < 0:
            raise DomainError("invalid_gps", "accuracy_m 不能为负", 400)
    return {"lng": lng, "lat": lat, "accuracy_m": accuracy}


def validate_polygon(polygon):
    if not isinstance(polygon, list) or len(polygon) < 3:
        raise DomainError("invalid_geometry", "地块边界至少需要 3 个坐标点", 400)
    norm = []
    for point in polygon:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise DomainError("invalid_geometry", "边界点必须是 [经度, 纬度]", 400)
        try:
            lng, lat = float(point[0]), float(point[1])
        except (TypeError, ValueError) as exc:
            raise DomainError("invalid_geometry", "边界坐标必须为数值", 400) from exc
        if not -180 <= lng <= 180 or not -90 <= lat <= 90:
            raise DomainError("invalid_geometry", "经纬度超出合法范围", 400)
        norm.append([lng, lat])
    return norm


# ---------------------------------------------------------------------------
# 核心服务
# ---------------------------------------------------------------------------


@dataclass
class StoredEvent:
    record: dict

    @property
    def accepted(self):
        return self.record["status"] == "accepted"


class HarvestService:
    """把追加型存储重放成内存索引，并承载全部业务规则。"""

    def __init__(self, store, clock=None, gps_tolerance_m=GPS_TOLERANCE_M):
        self.store = store
        self.gps_tolerance_m = gps_tolerance_m
        self.now = clock or (lambda: datetime.now(timezone.utc))
        self._replay()

    # -- 重放 --------------------------------------------------------------

    def _replay(self):
        self.plans = {}            # plan_id -> record
        self.plans_by_code = {}
        self.operations = {}       # op_id -> record
        self.events = []           # 全部事件记录（追加顺序）
        self.events_by_op = {}
        self.conflicts = {}        # conflict_id -> record
        self.samples = {}          # sample_id -> 创建记录（不可变）
        self.sample_actions = []   # transfer/split/consume/void
        self.seal_registry = {}    # seal_code -> sample_id
        self.sample_code_index = {}
        self._seq = 0
        for record in self.store.all("plans"):
            self.plans[record["id"]] = record
            self.plans_by_code[record["code"]] = record
        for record in self.store.all("operations"):
            self.operations[record["id"]] = record
            self.events_by_op[record["id"]] = []
        for record in self.store.all("op_status"):
            op = self.operations.get(record["op_id"])
            if op:
                op["status"] = record["status"]
                op["status_reason"] = record.get("reason")
        for record in self.store.all("events"):
            self._index_event(record)
        for record in self.store.all("conflicts"):
            self.conflicts[record["id"]] = record
        # 裁决记录是“状态变更”的唯一事实来源：重放时据此派生事件/冲突状态
        for record in self.store.all("resolutions"):
            conflict = self.conflicts.get(record["conflict_id"])
            if conflict and conflict["status"] == "open":
                conflict["status"] = (
                    "closed" if record["action"] == "auto_rejected" else "resolved"
                )
                conflict["resolution"] = {
                    k: record[k]
                    for k in ("action", "resolved_by", "resolved_at", "note", "actor_role")
                    if k in record
                }
        self._derive_event_states()
        for record in self.store.all("samples"):
            self.samples[record["id"]] = record
            self.sample_code_index[record["sample_code"]] = record["id"]
            if record.get("seal_code"):
                self.seal_registry[record["seal_code"]] = record["id"]
        for record in self.store.all("sample_actions"):
            self.sample_actions.append(record)
            self._apply_sample_action_index(record)
        self._enrich_sample_weights()

    def _derive_event_states(self):
        """事件的 accepted/rejected 状态由其冲突单裁决结果派生。"""
        for event in self.events:
            if event["status"] not in ("quarantined",):
                continue
            conflicts = [self.conflicts[c] for c in event.get("conflict_ids", [])
                         if c in self.conflicts]
            actions = [
                c["resolution"]["action"]
                for c in conflicts if c.get("resolution")
            ]
            if any(a in ("reject", "auto_rejected") for a in actions):
                event["status"] = "rejected"
            elif conflicts and all(
                c["status"] == "resolved"
                and c["resolution"]["action"] in ("accept", "auto_accept")
                for c in conflicts
            ):
                event["status"] = "accepted"

    def _index_event(self, record):
        self.events.append(record)
        self.events_by_op.setdefault(record["op_id"], []).append(record)

    def _apply_sample_action_index(self, record):
        # 子样本随 samples 流独立落库，动作流只负责行为本身
        return

    def _enrich_sample_weights(self):
        """净重来自称重阶段；补传/乱序裁决使称重晚于封样生效时回填。"""
        for sample in self.samples.values():
            if sample.get("sealed_event_id") is None:
                continue  # 实验室分装件自重不回填
            weighing = self._accepted_by_stage(sample["op_id"]).get("weighing")
            if weighing:
                sample["net_weight_kg"] = weighing["payload"].get("net_weight_kg")

    def _append(self, kind, record):
        seq = self.store.append(kind, record)
        self._seq = max(self._seq, seq)
        return record

    # -- 方案冻结 ----------------------------------------------------------

    def create_plan(self, body, actor):
        require(body, ["code", "plots"], "试验方案")
        code = body["code"]
        if code in self.plans_by_code:
            raise DomainError("plan_exists", f"方案编号 {code} 已存在，方案不可改写", 409)
        plots = body["plots"]
        if not isinstance(plots, list) or not plots:
            raise DomainError("invalid_plots", "plots 至少包含一个地块", 400)
        frozen_plots = []
        for plot in plots:
            require(
                plot,
                ["plot_code", "geometry", "cultivar", "cultivar_generation", "sowing_batch"],
                "地块",
            )
            frozen = {
                "plot_code": str(plot["plot_code"]),
                "geometry": validate_polygon(plot["geometry"]),
                "cultivar": str(plot["cultivar"]),
                "cultivar_generation": str(plot["cultivar_generation"]),
                "sowing_batch": str(plot["sowing_batch"]),
                "area_mu": round(
                    polygon_area_mu(validate_polygon(plot["geometry"])), 4
                ),
            }
            frozen_plots.append(frozen)
        if len({p["plot_code"] for p in frozen_plots}) != len(frozen_plots):
            raise DomainError("duplicate_plot_code", "同一方案内地块编号不得重复", 400)
        record = {
            "id": _id("plt"),
            "code": str(code),
            "season": body.get("season"),
            "frozen_at": iso(self.now()),
            "frozen_by": actor["user_id"],
            "sampling_scheme": body.get("sampling_scheme", {}),
            "plots": frozen_plots,
            "status": "frozen",
        }
        self._append("plans", record)
        self.plans[record["id"]] = record
        self.plans_by_code[record["code"]] = record
        return record

    def get_plan(self, plan_code):
        plan = self.plans_by_code.get(plan_code)
        if not plan:
            raise DomainError("plan_not_found", f"方案 {plan_code} 不存在", 404)
        return plan

    def _frozen_plot(self, plan, plot_code):
        for plot in plan["plots"]:
            if plot["plot_code"] == plot_code:
                return plot
        raise DomainError("plot_not_found", f"方案 {plan['code']} 中无地块 {plot_code}", 404)

    # -- 作业 --------------------------------------------------------------

    def create_operation(self, body, actor):
        require(body, ["plan_code", "plot_code", "machine_id"], "作业")
        plan = self.get_plan(body["plan_code"])
        plot = self._frozen_plot(plan, body["plot_code"])
        for existing in self.operations.values():
            if (
                existing["plan_id"] == plan["id"]
                and existing["plot_code"] == plot["plot_code"]
                and existing["status"] == "active"
            ):
                raise DomainError(
                    "plot_operation_active",
                    f"地块 {plot['plot_code']} 已有进行中的作业，同一小区不得并行收割",
                    409,
                )
        record = {
            "id": _id("op"),
            "plan_id": plan["id"],
            "plan_code": plan["code"],
            "plot_code": plot["plot_code"],
            "machine_id": str(body["machine_id"]),
            "operator_id": body.get("operator_id") or actor["user_id"],
            "created_at": iso(self.now()),
            "created_by": actor["user_id"],
            "status": "active",
            # 冻结快照：作业的一切核验以开工时刻为准
            "frozen_snapshot": {
                "plot": plot,
                "sampling_scheme": plan["sampling_scheme"],
                "season": plan["season"],
            },
        }
        self._append("operations", record)
        self.operations[record["id"]] = record
        self.events_by_op[record["id"]] = []
        return record

    def _get_operation(self, op_id):
        op = self.operations.get(op_id)
        if not op:
            raise DomainError("operation_not_found", f"作业 {op_id} 不存在", 404)
        return op

    def accepted_events(self, op_id):
        return [e for e in self.events_by_op.get(op_id, []) if e["status"] == "accepted"]

    def _accepted_by_stage(self, op_id):
        return {e["stage"]: e for e in self.accepted_events(op_id)}

    # -- 事件上报 ----------------------------------------------------------

    def submit_event(self, op_id, body, actor):
        """田间记录入口。

        返回 (outcome, record)：
        * accepted   —— 事件立即生效（201）
        * replay     —— 幂等重放（200）
        * quarantined —— 进入隔离区并挂冲突单（202，绝不覆盖）
        """
        op = self._get_operation(op_id)
        require(body, ["stage", "occurred_at", "client_event_id"], "阶段事件")
        stage = body["stage"]
        if stage not in STAGES:
            raise DomainError(
                "unknown_stage",
                f"未知阶段 {stage}，合法阶段: {', '.join(STAGES)}",
                400,
            )
        occurred_at = parse_ts(body["occurred_at"])
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            raise DomainError("invalid_payload", "payload 必须是对象", 400)
        gps = validate_gps(payload.get("gps")) if "gps" in payload else None
        client_event_id = str(body["client_event_id"])

        # 1) 幂等 / 重复提交
        duplicate_existing = None
        for prior in self.events_by_op.get(op_id, []):
            if prior["client_event_id"] != client_event_id:
                continue
            if prior["payload_hash"] == payload_hash(body.get("payload") or {}):
                return "replay", self._replay_view(prior)
            duplicate_existing = prior
            break

        # 2) 阶段机核验
        conflict_codes = []
        if duplicate_existing:
            conflict_codes.append("duplicate_event")
        accepted = self._accepted_by_stage(op_id)
        ordered_times = sorted(
            parse_ts(e["occurred_at"]) for e in self.accepted_events(op_id)
        )

        if stage in accepted:
            conflict_codes.append("stage_repeated")
        else:
            expected_index = self._next_stage_index(accepted)
            if STAGES.index(stage) > expected_index:
                conflict_codes.append("stage_skip")

        self._validate_stage_payload(stage, payload)

        maturity = accepted.get("maturity_check")
        if stage == "harvest":
            approved = bool(maturity and maturity["payload"].get("approved", False))
            if not approved:
                conflict_codes.append("plot_not_released")
            machine_id = payload.get("machine_id")
            if machine_id and str(machine_id) != op["machine_id"]:
                conflict_codes.append("machine_mismatch")

        # 3) 封签 / 样本号 / 货位唯一性
        related = {}
        if stage == "sealing":
            seal_code = payload.get("seal_code")
            sample_code = payload.get("sample_code")
            if not seal_code or not sample_code:
                raise DomainError(
                    "missing_fields", "封样事件需要 seal_code 与 sample_code", 400
                )
            if seal_code in self.seal_registry:
                conflict_codes.append("seal_already_scanned")
                related["existing_sample_id"] = self.seal_registry[seal_code]
            if sample_code in self.sample_code_index:
                conflict_codes.append("duplicate_sample_code")
                related["existing_sample_code_owner"] = self.sample_code_index[sample_code]
        if stage == "storage":
            bin_code = payload.get("storage_bin")
            if bin_code:
                owner = self._bin_owner(str(bin_code), exclude_op=op_id)
                if owner:
                    conflict_codes.append("duplicate_storage_bin")
                    related["bin_occupied_by"] = owner

        # 4) GPS 漂移 / 越界
        if gps is not None:
            polygon = op["frozen_snapshot"]["plot"]["geometry"]
            dist = distance_to_polygon_m(gps["lng"], gps["lat"], polygon)
            if dist > 0:
                code = (
                    "gps_drift"
                    if dist <= self.gps_tolerance_m
                    else "gps_outside_plot"
                )
                conflict_codes.append(code)
                related["distance_to_plot_m"] = round(dist, 2)
            accuracy = gps.get("accuracy_m")
            if accuracy is not None and accuracy > GPS_LOW_ACCURACY_M:
                conflict_codes.append("gps_low_accuracy")
                related["gps_accuracy_m"] = accuracy

        # 5) 离线补传乱序
        if ordered_times and occurred_at < ordered_times[-1]:
            conflict_codes.append("late_arrival")
            lag = ordered_times[-1] - occurred_at
            related["backfill_lag_seconds"] = int(lag.total_seconds())

        event_record = {
            "id": _id("evt"),
            "op_id": op_id,
            "stage": stage,
            "stage_name": STAGE_NAMES_CN[stage],
            "client_event_id": client_event_id,
            "operator_id": actor["user_id"],
            "occurred_at": iso(occurred_at),
            "received_at": iso(self.now()),
            "received_seq": self.store.next_seq(),
            "payload": payload,
            "payload_hash": payload_hash(payload),
            "gps": gps,
            "status": "quarantined" if conflict_codes else "accepted",
            "conflict_ids": [],
            "overrides": [],
        }
        if duplicate_existing:
            related["existing_event_id"] = duplicate_existing["id"]

        if not conflict_codes:
            self._append("events", event_record)
            self._index_event(event_record)
            self._apply_accepted_event(event_record, op)
            if stage == "storage":
                self._set_op_status(op, "completed", "入库完成")
            # 更正后重新提交的阶段，可能使此前仅因顺序挂起的后续事件续通
            self._propagate_chain(op_id)
            return "accepted", event_record

        # 隔离：先按最终事件号开冲突单，再连同 conflict_ids 一起把事件落库，
        # 保证重启重放后事件与冲突仍可关联（绝不覆盖原始上报）
        open_conflicts = [
            self._make_conflict(op, stage, code, event_record, body,
                                occurred_at, related=related)
            for code in dict.fromkeys(conflict_codes)  # 去重保序
        ]
        event_record["conflict_ids"] = [c["id"] for c in open_conflicts]
        self._append("events", event_record)
        self._index_event(event_record)
        for conflict in open_conflicts:
            self._append("conflicts", conflict)
            self.conflicts[conflict["id"]] = conflict
        return "quarantined", {"event": event_record, "conflicts": open_conflicts}

    def _replay_view(self, event_record):
        return {
            "event": event_record,
            "conflicts": [
                self.conflicts[cid]
                for cid in event_record.get("conflict_ids", [])
                if cid in self.conflicts
            ],
        }

    def _make_conflict(self, op, stage, code, event_record, raw_body,
                       occurred_at, related=None):
        severity, explanation = CONFLICT_INFO[code]
        return {
            "id": _id("cfl"),
            "op_id": op["id"],
            "plot_code": op["plot_code"],
            "plan_code": op["plan_code"],
            "stage": stage,
            "stage_name": STAGE_NAMES_CN[stage],
            "code": code,
            "severity": severity,
            "explanation": explanation,
            "detected_at": iso(self.now()),
            "event_id": event_record["id"],
            "event_snapshot": raw_body,
            "occurred_at": iso(occurred_at),
            "related": related or {},
            "status": "open",
            "resolution": None,
        }

    @staticmethod
    def _next_stage_index(accepted):
        """当前允许的下一阶段序号。已接受阶段集合须为前缀（裁决可制造缺口）。"""
        index = 0
        while index < len(STAGES) and STAGES[index] in accepted:
            index += 1
        return index

    @staticmethod
    def _validate_stage_payload(stage, payload):
        """各阶段自身的字段完整性校验（与跨事件规则无关）。"""
        if stage == "maturity_check":
            if "approved" not in payload:
                raise DomainError("missing_fields", "成熟度复核需要 approved 布尔值", 400)
            if not isinstance(payload["approved"], bool):
                raise DomainError("invalid_payload", "approved 必须为布尔值", 400)
        elif stage == "machine_clean":
            if payload.get("cleaned") is not True:
                raise DomainError(
                    "invalid_payload", "机器清洁事件必须确认 cleaned=true（清仓完成）", 400
                )
        elif stage == "first_grain_discard":
            weight = payload.get("discarded_weight_kg")
            if weight is None:
                raise DomainError(
                    "missing_fields", "首段弃粮需要 discarded_weight_kg", 400
                )
            if float(weight) < 0:
                raise DomainError("invalid_payload", "弃粮重量不能为负", 400)
        elif stage == "weighing":
            weight = payload.get("net_weight_kg")
            if weight is None:
                raise DomainError("missing_fields", "称重需要 net_weight_kg", 400)
            try:
                weight = float(weight)
            except (TypeError, ValueError) as exc:
                raise DomainError("invalid_payload", "net_weight_kg 必须为数值", 400) from exc
            if weight <= 0:
                raise DomainError("invalid_payload", "净重必须为正", 400)
        elif stage == "storage":
            if not payload.get("storage_bin"):
                raise DomainError("missing_fields", "入库需要 storage_bin 货位号", 400)

    def _bin_owner(self, bin_code, exclude_op=None):
        for event in self.events:
            if (
                event["status"] == "accepted"
                and event["stage"] == "storage"
                and str(event["payload"].get("storage_bin")) == bin_code
                and event["op_id"] != exclude_op
            ):
                return event["op_id"]
        return None

    # -- 事件生效后的副作用 -------------------------------------------------

    def _apply_accepted_event(self, event_record, op):
        if event_record["stage"] != "sealing":
            return
        payload = event_record["payload"]
        weighing = self._accepted_by_stage(op["id"]).get("weighing")
        net_weight = weighing["payload"].get("net_weight_kg") if weighing else None
        location_event = None  # 入库通常在封样之后
        sample = {
            "id": _id("smp"),
            "sample_code": str(payload["sample_code"]),
            "seal_code": str(payload["seal_code"]),
            "op_id": op["id"],
            "plan_code": op["plan_code"],
            "plot_code": op["plot_code"],
            "cultivar": op["frozen_snapshot"]["plot"]["cultivar"],
            "cultivar_generation": op["frozen_snapshot"]["plot"]["cultivar_generation"],
            "sowing_batch": op["frozen_snapshot"]["plot"]["sowing_batch"],
            "sampling_scheme": op["frozen_snapshot"]["sampling_scheme"],
            "net_weight_kg": net_weight,
            "sealed_event_id": event_record["id"],
            "sealed_at": event_record["occurred_at"],
            "sealed_by": event_record["operator_id"],
            "parent_ids": [],
            "root_sample_id": None,
            "depth": 0,
        }
        self._append("samples", sample)
        self.samples[sample["id"]] = sample
        self.sample_code_index[sample["sample_code"]] = sample["id"]
        self.seal_registry[sample["seal_code"]] = sample["id"]

    # -- 冲突裁决 ----------------------------------------------------------

    def resolve_conflict(self, conflict_id, body, actor):
        conflict = self.conflicts.get(conflict_id)
        if not conflict:
            raise DomainError("conflict_not_found", f"冲突单 {conflict_id} 不存在", 404)
        if conflict["status"] != "open":
            raise DomainError(
                "conflict_closed", "冲突单已裁决，裁决记录不可修改", 409,
                {"resolution": conflict["resolution"]},
            )
        require(body, ["action"], "裁决")
        action = body["action"]
        if action not in ("accept", "reject"):
            raise DomainError("invalid_action", "action 只能是 accept 或 reject", 400)
        note = body.get("note")
        if action == "accept":
            if conflict["severity"] == SEVERITY_HIGH and actor["role"] not in (
                "admin", "researcher"
            ):
                raise DomainError(
                    "forbidden_override",
                    f"高危冲突 {conflict['code']} 仅研究负责人/管理员可判定采信",
                    403,
                )
            if not note:
                raise DomainError(
                    "missing_resolution_note",
                    "采信冲突事件必须填写处置说明（可解释、可追溯）",
                    400,
                )
        event = next(
            (e for e in self.events_by_op.get(conflict["op_id"], [])
             if e["id"] == conflict["event_id"]),
            None,
        )
        resolution = {
            "action": action,
            "resolved_by": actor["user_id"],
            "resolved_at": iso(self.now()),
            "note": note,
            "actor_role": actor["role"],
        }
        conflict["status"] = "resolved"
        conflict["resolution"] = resolution
        self.store.append("resolutions", {
            "id": _id("rsl"),
            "conflict_id": conflict["id"],
            **resolution,
        })
        if event is not None and action == "reject":
            # 驳回具有否决权：事件作废，挂在同一事件上的其他未决冲突级联关闭
            for other_id in event.get("conflict_ids", []):
                other = self.conflicts.get(other_id)
                if other and other["id"] != conflict["id"] and other["status"] == "open":
                    self._close_with_resolution(other, {
                        "action": "auto_rejected",
                        "resolved_by": actor["user_id"],
                        "resolved_at": iso(self.now()),
                        "note": f"同事件已由冲突 {conflict['id']} 驳回，级联关闭",
                        "actor_role": actor["role"],
                    })
            event["status"] = "rejected"
            self._abort_downstream(conflict["op_id"], event, conflict["id"], actor)
        elif event is not None and action == "accept":
            event["overrides"].append(conflict["code"])
            still_open = [
                cid for cid in event.get("conflict_ids", [])
                if self.conflicts[cid]["status"] == "open"
            ]
            if not still_open and event["status"] == "quarantined":
                # 所有冲突均采信，事件此时才生效
                event["status"] = "accepted"
                op = self._get_operation(conflict["op_id"])
                self._apply_accepted_event(event, op)
                if event["stage"] == "weighing":
                    # 称重补传晚于封样生效：回填已建样本净重
                    self._enrich_sample_weights()
                self._propagate_chain(op["id"])
        return conflict

    def _close_with_resolution(self, conflict, resolution):
        conflict["status"] = "closed"
        conflict["resolution"] = resolution
        self.store.append("resolutions", {
            "id": _id("rsl"),
            "conflict_id": conflict["id"],
            **resolution,
        })

    def _abort_downstream(self, op_id, rejected_event, conflict_id, actor):
        """污染关键阶段（收获及之前）驳回即作业链断裂。

        后续仅因 stage_skip 挂起的事件一并关闭，作业置为 aborted，同一地块可
        重新登记作业并按完整流程重来（品种隔离优先于产量连续性）。
        称重/封样/入库阶段的驳回不终止作业：更正后可重新提交该阶段，
        挂起的后续事件在新阶段采信后自动续链。
        """
        rejected_index = STAGES.index(rejected_event["stage"])
        if rejected_index > STAGES.index("harvest"):
            return
        for later in self.events_by_op[op_id]:
            if later["status"] != "quarantined":
                continue
            if STAGES.index(later["stage"]) <= rejected_index:
                continue
            codes = {self.conflicts[c]["code"] for c in later["conflict_ids"]}
            if codes <= {"stage_skip"}:
                later["status"] = "rejected"
                for cid in later["conflict_ids"]:
                    linked = self.conflicts[cid]
                    if linked["status"] == "open":
                        self._close_with_resolution(linked, {
                            "action": "auto_rejected",
                            "resolved_by": actor["user_id"],
                            "resolved_at": iso(self.now()),
                            "note": f"上游阶段 {STAGE_NAMES_CN[rejected_event['stage']]} "
                                    f"被驳回（冲突 {conflict_id}），作业链终止",
                            "actor_role": actor["role"],
                        })
        op = self.operations[op_id]
        if op["status"] == "active":
            self._set_op_status(
                op, "aborted",
                f"{STAGE_NAMES_CN[rejected_event['stage']]}阶段被驳回（{conflict_id}），"
                f"须重新登记作业",
            )

    def _propagate_chain(self, op_id):
        """前置阶段被采信后，仅因“阶段跳跃”挂起的后续事件按发生顺序自动生效。

        自动解封同样生成 auto_accept 裁决记录，可在冲突单上看到解除原因；
        携带任何其他冲突（漂移、封签重复等）的事件不在自动解封之列。
        """
        progressed = True
        while progressed:
            progressed = False
            accepted = self._accepted_by_stage(op_id)
            next_index = self._next_stage_index(accepted)
            if next_index >= len(STAGES):
                return
            candidates = sorted(
                (e for e in self.events_by_op[op_id]
                 if e["status"] == "quarantined" and e["stage"] == STAGES[next_index]),
                key=lambda e: e["occurred_at"],
            )
            for event in candidates:
                codes = {self.conflicts[c]["code"] for c in event["conflict_ids"]}
                if not codes <= {"stage_skip"}:
                    continue
                auto = {
                    "action": "auto_accept",
                    "resolved_by": "system",
                    "resolved_at": iso(self.now()),
                    "note": "前置阶段采信后作业链路续通，顺序挂起自动解除",
                    "actor_role": "system",
                }
                for cid in event["conflict_ids"]:
                    linked = self.conflicts[cid]
                    linked["status"] = "resolved"
                    linked["resolution"] = auto
                    self.store.append("resolutions", {
                        "id": _id("rsl"), "conflict_id": cid, **auto,
                    })
                event["status"] = "accepted"
                event["overrides"].append("stage_skip:auto")
                self._apply_accepted_event(event, self.operations[op_id])
                if event["stage"] == "storage":
                    self._set_op_status(self.operations[op_id], "completed", "入库完成")
                progressed = True
                break  # 链路可能续通多级，从头重算

    def _set_op_status(self, op, status, reason):
        if op.get("status") == status:
            return
        op["status"] = status
        op["status_reason"] = reason
        self.store.append("op_status", {
            "id": _id("ost"),
            "op_id": op["id"],
            "status": status,
            "reason": reason,
            "at": iso(self.now()),
        })


    def list_conflicts(self, op_id=None, status_filter="open"):
        out = list(self.conflicts.values())
        if op_id:
            out = [c for c in out if c["op_id"] == op_id]
        if status_filter and status_filter != "all":
            out = [c for c in out if c["status"] == status_filter]
        return sorted(out, key=lambda c: c["detected_at"])

    # -- 样本谱系：转交 / 重分装 / 耗用 / 作废 ------------------------------

    def _get_sample(self, sample_ref):
        if sample_ref in self.samples:
            return self.samples[sample_ref]
        for sample in self.samples.values():
            if sample["sample_code"] == sample_ref:
                return sample
        raise DomainError("sample_not_found", f"样本 {sample_ref} 不存在", 404)

    def sample_view(self, sample_ref):
        sample = self._get_sample(sample_ref)
        actions = [
            a for a in self.sample_actions
            if a["sample_id"] == sample["id"]
            or sample["id"] in a.get("child_ids", [])
        ]
        children = [
            self.samples[cid]
            for cid in self._children(sample["id"])
        ]
        return {
            **sample,
            "status": self.sample_status(sample["id"]),
            "remaining_weight_kg": self.remaining_weight(sample["id"]),
            "lineage": self._lineage(sample["id"]),
            "children": children,
            "actions": sorted(actions, key=lambda a: a["at"]),
        }

    def _children(self, sample_id):
        out = []
        for action in self.sample_actions:
            if action["type"] == "split" and action["sample_id"] == sample_id:
                out.extend(action["child_ids"])
        return out

    def sample_status(self, sample_id):
        sample = self.samples[sample_id]
        # 分装子样本由实验室拆分产生，出生即在实验室；母样须显式转交登记
        status = "in_lab" if sample.get("parent_ids") else "sealed"
        for action in self.sample_actions:
            if action["sample_id"] != sample_id:
                continue
            if action["type"] == "transfer":
                status = "in_lab"
            elif action["type"] == "consume":
                status = "consumed" if self.remaining_weight(sample_id) <= 0 else "in_lab"
            elif action["type"] == "void":
                status = "voided"
        return status

    def remaining_weight(self, sample_id):
        sample = self.samples[sample_id]
        remaining = float(sample.get("net_weight_kg") or 0.0)
        for action in self.sample_actions:
            if action["sample_id"] != sample_id:
                continue
            if action["type"] == "consume":
                remaining -= float(action["amount_kg"])
            elif action["type"] == "split":
                remaining -= float(action.get("weight_out_kg") or 0.0)
        return round(remaining, 6)

    def _require_lab_sample(self, sample):
        status = self.sample_status(sample["id"])
        if status == "voided":
            raise DomainError("sample_voided", "已作废样本不得继续操作", 409)
        if status not in ("in_lab", "consumed"):
            raise DomainError(
                "sample_not_transferred",
                "样本须先转交实验室登记，后续重分装/耗用/作废才沿用实验室谱系",
                409,
            )

    def transfer_sample(self, sample_ref, body, actor):
        sample = self._get_sample(sample_ref)
        require(body, ["lab", "handed_to"], "转交记录")
        if self.sample_status(sample["id"]) not in ("sealed",):
            raise DomainError(
                "sample_already_transferred", "样本已完成实验室转交登记", 409
            )
        action = {
            "id": _id("act"),
            "type": "transfer",
            "sample_id": sample["id"],
            "at": iso(self.now()),
            "by": actor["user_id"],
            "lab": str(body["lab"]),
            "handed_to": str(body["handed_to"]),
            "note": body.get("note"),
        }
        self._append("sample_actions", action)
        self.sample_actions.append(action)
        return self.sample_view(sample["id"])

    def split_sample(self, sample_ref, body, actor):
        sample = self._get_sample(sample_ref)
        self._require_lab_sample(sample)
        require(body, ["items"], "重分装")
        items = body["items"]
        if not isinstance(items, list) or not items:
            raise DomainError("invalid_items", "items 至少包含一个分装件", 400)
        prepared = []
        total = 0.0
        for item in items:
            require(item, ["sample_code", "weight_kg"], "分装件")
            code = str(item["sample_code"])
            if code in self.sample_code_index:
                raise DomainError(
                    "duplicate_sample_code", f"样本编号 {code} 已存在", 409
                )
            weight = float(item["weight_kg"])
            if weight <= 0:
                raise DomainError("invalid_weight", "分装重量必须为正", 400)
            total += weight
            prepared.append((code, weight, item.get("seal_code"), item.get("note")))
        if total > self.remaining_weight(sample["id"]) + 1e-9:
            raise DomainError(
                "insufficient_sample",
                "分装总重超过样本当前余量，不允许凭空产生谱系节点",
                409,
                {"remaining_weight_kg": self.remaining_weight(sample["id"])}
            )
        root_id = sample.get("root_sample_id") or sample["id"]
        children = []
        child_ids = []
        for code, weight, seal_code, note in prepared:
            child = {
                "id": _id("smp"),
                "sample_code": code,
                "seal_code": seal_code,
                "op_id": sample["op_id"],
                "plan_code": sample["plan_code"],
                "plot_code": sample["plot_code"],
                "cultivar": sample["cultivar"],
                "cultivar_generation": sample["cultivar_generation"],
                "sowing_batch": sample["sowing_batch"],
                "sampling_scheme": sample["sampling_scheme"],
                "net_weight_kg": weight,
                "sealed_event_id": None,
                "sealed_at": iso(self.now()),
                "sealed_by": actor["user_id"],
                "parent_ids": [sample["id"]],
                "root_sample_id": root_id,
                "depth": sample["depth"] + 1,
            }
            children.append(child)
            child_ids.append(child["id"])
        action = {
            "id": _id("act"),
            "type": "split",
            "sample_id": sample["id"],
            "child_ids": child_ids,
            "weight_out_kg": round(total, 6),
            "at": iso(self.now()),
            "by": actor["user_id"],
            "note": body.get("note"),
        }
        self._append("sample_actions", action)
        self.sample_actions.append(action)
        for child in children:
            self._append("samples", child)
            self.samples[child["id"]] = child
            self.sample_code_index[child["sample_code"]] = child["id"]
            if child.get("seal_code"):
                self.seal_registry.setdefault(child["seal_code"], child["id"])
        return self.sample_view(sample["id"])

    def consume_sample(self, sample_ref, body, actor):
        sample = self._get_sample(sample_ref)
        self._require_lab_sample(sample)
        require(body, ["amount_kg", "purpose"], "耗用记录")
        amount = float(body["amount_kg"])
        if amount <= 0:
            raise DomainError("invalid_amount", "耗用量必须为正", 400)
        if amount > self.remaining_weight(sample["id"]) + 1e-9:
            raise DomainError(
                "insufficient_sample",
                "耗用量超过样本余量",
                409,
                {"remaining_weight_kg": self.remaining_weight(sample["id"])},
            )
        action = {
            "id": _id("act"),
            "type": "consume",
            "sample_id": sample["id"],
            "amount_kg": amount,
            "purpose": str(body["purpose"]),
            "at": iso(self.now()),
            "by": actor["user_id"],
            "note": body.get("note"),
        }
        self._append("sample_actions", action)
        self.sample_actions.append(action)
        return self.sample_view(sample["id"])

    def void_sample(self, sample_ref, body, actor):
        sample = self._get_sample(sample_ref)
        self._require_lab_sample(sample)
        require(body, ["reason"], "作废记录")
        active_children = [
            cid for cid in self._children(sample["id"])
            if self.sample_status(cid) not in ("voided", "consumed")
        ]
        if active_children:
            raise DomainError(
                "children_still_active",
                "存在仍在库/在验的分装件，须先处置子样本才能作废母样",
                409,
                {"child_ids": active_children},
            )
        action = {
            "id": _id("act"),
            "type": "void",
            "sample_id": sample["id"],
            "reason": str(body["reason"]),
            "at": iso(self.now()),
            "by": actor["user_id"],
        }
        self._append("sample_actions", action)
        self.sample_actions.append(action)
        return self.sample_view(sample["id"])

    def _lineage(self, sample_id):
        """从任一节点回溯到封样原点的链。"""
        chain = []
        current = self.samples[sample_id]
        seen = set()
        while current and current["id"] not in seen:
            seen.add(current["id"])
            chain.append({
                "sample_id": current["id"],
                "sample_code": current["sample_code"],
                "depth": current["depth"],
                "parent_ids": current["parent_ids"],
                "op_id": current["op_id"],
                "plot_code": current["plot_code"],
                "cultivar": current["cultivar"],
                "cultivar_generation": current["cultivar_generation"],
                "sowing_batch": current["sowing_batch"],
            })
            if not current["parent_ids"]:
                break
            current = self.samples.get(current["parent_ids"][0])
        return list(reversed(chain))

    # -- 产量重建 ----------------------------------------------------------

    def rebuild_plan(self, plan_code):
        plan = self.get_plan(plan_code)
        plots_out = []
        total_weight = 0.0
        total_area = 0.0
        excluded = []
        for plot in plan["plots"]:
            ops = [
                op for op in self.operations.values()
                if op["plan_id"] == plan["id"] and op["plot_code"] == plot["plot_code"]
            ]
            ops.sort(key=lambda o: o["created_at"])
            if not ops:
                excluded.append({"plot_code": plot["plot_code"], "reason": "no_operation"})
                continue
            for op in ops:
                accepted = self._accepted_by_stage(op["id"])
                open_conflicts = self.list_conflicts(op["id"], "open")
                chain_ok = all(stage in accepted for stage in STAGES if stage != "storage")
                sample_ids = [
                    sid for sid, s in self.samples.items() if s["op_id"] == op["id"]
                ]
                samples = [self.sample_view(sid) for sid in sample_ids]
                valid_samples = [s for s in samples if s["status"] != "voided"]
                if not chain_ok or not valid_samples:
                    missing = [
                        STAGE_NAMES_CN[s] for s in STAGES
                        if s != "storage" and s not in accepted
                    ]
                    excluded.append({
                        "plot_code": plot["plot_code"],
                        "operation_id": op["id"],
                        "reason": "chain_incomplete" if not chain_ok else "sample_voided",
                        "missing_stages": missing,
                        "open_conflicts": [c["code"] for c in open_conflicts],
                    })
                    continue
                weighing = accepted.get("weighing")
                net_weight = float(weighing["payload"]["net_weight_kg"])
                storage = accepted.get("storage")
                per_mu = (
                    round(net_weight / plot["area_mu"], 3) if plot["area_mu"] else None
                )
                total_weight += net_weight
                total_area += plot["area_mu"]
                plots_out.append({
                    "plot_code": plot["plot_code"],
                    "cultivar": plot["cultivar"],
                    "cultivar_generation": plot["cultivar_generation"],
                    "sowing_batch": plot["sowing_batch"],
                    "area_mu": plot["area_mu"],
                    "operation_id": op["id"],
                    "net_weight_kg": net_weight,
                    "yield_kg_per_mu": per_mu,
                    "stored": storage is not None,
                    "storage_bin": storage["payload"].get("storage_bin") if storage else None,
                    "samples": [
                        {
                            "sample_code": s["sample_code"],
                            "seal_code": s["seal_code"],
                            "status": s["status"],
                            "remaining_weight_kg": s["remaining_weight_kg"],
                            "root_sample_id": s.get("root_sample_id") or s["id"],
                        }
                        for s in valid_samples
                    ],
                    "open_conflicts": [c["code"] for c in open_conflicts],
                })
        return {
            "plan_code": plan["code"],
            "season": plan["season"],
            "frozen_at": plan["frozen_at"],
            "generated_at": iso(self.now()),
            "sampling_scheme": plan["sampling_scheme"],
            "effective_plot_count": len(plots_out),
            "total_net_weight_kg": round(total_weight, 3),
            "total_area_mu": round(total_area, 4),
            "avg_yield_kg_per_mu": (
                round(total_weight / total_area, 3) if total_area else None
            ),
            "plots": plots_out,
            "excluded": excluded,
        }

    # -- 机器轨迹反查 ------------------------------------------------------

    def machine_stops(self, machine_id, day=None, before_iso=None, plot_code=None):
        """按发生时间排序机器当天的停靠点（清仓点 + 正式收获点）。"""
        stops = []
        for event in self.events:
            if event["status"] != "accepted" or event["stage"] not in (
                "machine_clean", "harvest"
            ):
                continue
            op = self.operations[event["op_id"]]
            # 事件自带机器号优先（清仓跨地块时），缺省回退作业登记机器
            stop_machine = str(event["payload"].get("machine_id") or op["machine_id"])
            if stop_machine != str(machine_id):
                continue
            at = parse_ts(event["occurred_at"])
            if day and at.strftime("%Y-%m-%d") != day:
                continue
            stops.append({
                "sequence": 0,
                "plot_code": op["plot_code"],
                "plan_code": op["plan_code"],
                "operation_id": op["id"],
                "stage": event["stage"],
                "stage_name": STAGE_NAMES_CN[event["stage"]],
                "at": iso(at),
            })
        stops.sort(key=lambda s: s["at"])
        for i, stop in enumerate(stops, 1):
            stop["sequence"] = i
        anchor = None
        if plot_code and before_iso:
            cutoff = parse_ts(before_iso)
            for i, stop in enumerate(stops):
                if stop["plot_code"] == plot_code and parse_ts(stop["at"]) <= cutoff:
                    anchor = i
            if anchor is None:
                raise DomainError(
                    "anchor_not_found",
                    f"未找到机器 {machine_id} 在 {day or ''} 进入地块 {plot_code} 的记录",
                    404,
                )
            stops = stops[:anchor]
        elif before_iso:
            cutoff = parse_ts(before_iso)
            stops = [s for s in stops if parse_ts(s["at"]) < cutoff]
        return stops

    def trace_conflict(self, conflict_id):
        """从异常单直接反查机器当天在此之前经过的田块。"""
        conflict = self.conflicts.get(conflict_id)
        if not conflict:
            raise DomainError("conflict_not_found", f"冲突单 {conflict_id} 不存在", 404)
        op = self._get_operation(conflict["op_id"])
        at = parse_ts(conflict["occurred_at"])
        day = at.strftime("%Y-%m-%d")
        machine_id = op["machine_id"]
        event = next(
            (e for e in self.events_by_op[op["id"]] if e["id"] == conflict["event_id"]),
            None,
        )
        if event and event["payload"].get("machine_id"):
            machine_id = str(event["payload"]["machine_id"])
        prior = self.machine_stops(machine_id, day=day, before_iso=iso(at))
        # “此前经过的田块”不含异常发生地块本身（其清仓准备属于本小区作业）
        prior = [stop for stop in prior if stop["plot_code"] != op["plot_code"]]
        return {
            "conflict_id": conflict["id"],
            "code": conflict["code"],
            "machine_id": machine_id,
            "day": day,
            "anchor_plot_code": op["plot_code"],
            "anchor_at": iso(at),
            "prior_stops": prior,
        }

    # -- 机手最小视图 ------------------------------------------------------

    def operator_workload(self, actor):
        ops = [
            op for op in self.operations.values()
            if op["operator_id"] == actor["user_id"] and op["status"] == "active"
        ]
        return [self.operator_view(op["id"], actor) for op in sorted(ops, key=lambda o: o["created_at"])]

    def operator_view(self, op_id, actor=None):
        op = self._get_operation(op_id)
        if actor and actor["role"] == "operator" and op["operator_id"] != actor["user_id"]:
            raise DomainError(
                "forbidden", "机手只能查看分配给自己的当前作业", 403
            )
        accepted = self._accepted_by_stage(op_id)
        next_index = self._next_stage_index(accepted)
        open_conflicts = [
            {
                "id": c["id"],
                "code": c["code"],
                "stage": c["stage"],
                "explanation": c["explanation"],
            }
            for c in self.list_conflicts(op_id, "open")
        ]
        # 机手只看当前作业所需：到了哪一步、下一步做什么、是否有挂起异常
        return {
            "operation_id": op["id"],
            "plot_code": op["plot_code"],
            "machine_id": op["machine_id"],
            "completed_stages": [
                {"stage": s, "stage_name": STAGE_NAMES_CN[s],
                 "at": accepted[s]["occurred_at"]}
                for s in STAGES if s in accepted
            ],
            "next_stage": STAGES[next_index] if next_index < len(STAGES) else None,
            "next_stage_name": (
                STAGE_NAMES_CN[STAGES[next_index]] if next_index < len(STAGES) else None
            ),
            "open_conflicts": open_conflicts,
        }
