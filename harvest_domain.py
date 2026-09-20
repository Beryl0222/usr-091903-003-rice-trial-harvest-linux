"""水稻试验收获隔离 —— 领域核心。

设计原则：
* 冻结不可变：试验方案（地块边界、品种代次、播种批次、取样方案）一经冻结，
  只能读取，不能覆盖。
* 事件只追加：成熟度复核、机器清洁、首段弃粮、正式收获、称重、封样、入库
  七个阶段按实际发生时间追加，不提供更新/删除接口。
* 冲突不覆盖：定位漂移、离线补传、封签重复、顺序倒挂等情况不做静默纠正，
  原始记录进入隔离区并生成可解释的冲突单，由研究负责人审核后才生效。
* 谱系不中断：样本转交实验室后的重分装、耗用、作废全部追加到原始谱系，
  派生子样本始终回挂最初的试验/地块/封签。
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timezone

# 七个标准作业阶段，顺序即田间作业顺序。
STAGES = [
    "maturity_review",  # 成熟度复核
    "machine_clean",    # 机器清洁
    "head_discard",     # 首段弃粮
    "harvest",          # 正式收获
    "weighing",         # 称重
    "sealing",          # 封样
    "storage",          # 入库
]

# 各阶段田间录入时必须提供的字段。
STAGE_REQUIRED_FIELDS = {
    "maturity_review": ["maturity_grade"],
    "machine_clean": ["machine_code", "gps"],
    "head_discard": ["machine_code", "gps"],
    "harvest": ["machine_code", "gps"],
    "weighing": ["weight_kg", "moisture_pct"],
    "sealing": ["seal_code"],
    "storage": ["bin_code"],
}

# 设备离线超过该阈值才视为“离线补传”（秒）。现场网络抖动几分钟内属正常。
OFFLINE_LATE_THRESHOLD_SECONDS = 300

# 产量折算的标准含水率（稻谷籼稻常规贮运基准 13.5%）。
STANDARD_MOISTURE_PCT = 13.5

ROLES = ("admin", "lead", "operator", "lab")

# 演示用令牌表：令牌 -> (角色, 人员编号)。生产环境应由网关注入身份。
TOKENS = {
    "admin-token": ("admin", "A01"),
    "lead-token": ("lead", "R01"),
    "lab-token": ("lab", "L01"),
    "op01-token": ("operator", "O01"),
    "op02-token": ("operator", "O02"),
}

CONFLICT_LABELS = {
    "GPS_DRIFT": "定位漂移：上报坐标不在所填地块边界内",
    "OFFLINE_LATE": "离线补传：设备发生时间远早于上传时间",
    "SEAL_DUPLICATE": "封签重复：该封签已被其他样本使用",
    "ORDER_GAP": "作业顺序异常：存在阶段缺失或时间倒挂",
    "STAGE_DUPLICATE": "阶段重复：该地块此阶段已有生效记录",
}


class DomainError(Exception):
    """领域校验失败，status 是建议的 HTTP 状态码。"""

    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"error": self.code, "message": self.message, "details": self.details}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def parse_dt(value):
    """把 ISO8601 时间统一解析为带 UTC 时区的 datetime。"""
    if value is None:
        raise DomainError(400, "MISSING_FIELD", "缺少 occurred_at 字段")
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise DomainError(400, "BAD_TIME", f"无法解析时间：{value}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_day(dt_utc, raw_value):
    """优先取上报字符串里的自然日，保证“当天”按机手本地日期理解。"""
    raw = str(raw_value)
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    return dt_utc.date().isoformat()


def point_in_polygon(lon, lat, polygon):
    """射线法判断点是否在多边形内，polygon 为 [[lon, lat], ...]。"""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def bbox(polygon):
    lons = [p[0] for p in polygon]
    lats = [p[1] for p in polygon]
    return min(lons), min(lats), max(lons), max(lats)


# --------------------------------------------------------------------------- #
# 领域服务
# --------------------------------------------------------------------------- #

class HarvestService:
    def __init__(self, clock=None):
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.plots = {}          # plot_code -> 冻结方案
        self.events = {}         # event_id -> 作业事件记录
        self.events_by_plot = {}  # plot_code -> [event_id]
        self.client_event_index = {}  # client_event_id -> event_id
        self.conflicts = {}      # conflict_id -> 冲突单
        self.samples = {}        # sample_code -> 样本
        self.seal_index = {}     # seal_code -> [(event_id, plot_code, status)]

    # -- 身份 -------------------------------------------------------------- #

    def authenticate(self, token):
        if not token:
            raise DomainError(401, "UNAUTHENTICATED", "缺少 Bearer 令牌")
        identity = TOKENS.get(token)
        if identity is None:
            raise DomainError(401, "BAD_TOKEN", "令牌无效")
        return {"role": identity[0], "actor_code": identity[1]}

    def require_role(self, actor, *roles):
        if actor["role"] not in roles:
            raise DomainError(
                403,
                "FORBIDDEN",
                f"角色 {actor['role']} 无权执行该操作，需要：{'/'.join(roles)}",
            )

    def now(self):
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    # -- 试验方案冻结 ------------------------------------------------------ #

    def freeze_plot(self, actor, payload):
        self.require_role(actor, "admin")
        required = [
            "code", "trial_code", "variety_code", "generation",
            "sowing_batch", "boundary", "sampling_plan",
        ]
        missing = [f for f in required if f not in payload]
        if missing:
            raise DomainError(400, "MISSING_FIELD", "冻结方案缺少字段", {"missing": missing})

        code = str(payload["code"]).strip()
        if not code:
            raise DomainError(400, "BAD_PLOT_CODE", "地块编号不能为空")
        with self._lock:
            if code in self.plots:
                raise DomainError(409, "PLOT_FROZEN", f"地块 {code} 已冻结，禁止覆盖")
            boundary = self._validate_boundary(payload["boundary"])
            plan = self._validate_sampling_plan(payload["sampling_plan"])
            plot = {
                "code": code,
                "trial_code": str(payload["trial_code"]),
                "variety_code": str(payload["variety_code"]),
                "variety_name": payload.get("variety_name"),
                "generation": str(payload["generation"]),
                "sowing_batch": str(payload["sowing_batch"]),
                "boundary": boundary,
                "boundary_bbox": bbox(boundary),
                "sampling_plan": plan,
                "frozen_at": self.now().isoformat(),
                "frozen_by": actor["actor_code"],
            }
            self.plots[code] = plot
            self.events_by_plot[code] = []
            return self._public_plot(plot)

    @staticmethod
    def _validate_boundary(boundary):
        if not isinstance(boundary, list) or len(boundary) < 3:
            raise DomainError(400, "BAD_BOUNDARY", "边界至少需要 3 个 [经度, 纬度] 坐标点")
        clean = []
        for point in boundary:
            if (not isinstance(point, (list, tuple)) or len(point) != 2
                    or not all(isinstance(v, (int, float)) for v in point)):
                raise DomainError(400, "BAD_BOUNDARY", "边界点必须是 [经度, 纬度] 数字对")
            lon, lat = float(point[0]), float(point[1])
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise DomainError(400, "BAD_BOUNDARY", f"坐标越界：{point}")
            clean.append([lon, lat])
        return clean

    @staticmethod
    def _validate_sampling_plan(plan):
        if not isinstance(plan, dict):
            raise DomainError(400, "BAD_PLAN", "取样方案必须是对象")
        method = plan.get("method")
        if not method:
            raise DomainError(400, "BAD_PLAN", "取样方案必须声明 method")
        bags = plan.get("bags")
        if bags is not None and (not isinstance(bags, int) or bags <= 0):
            raise DomainError(400, "BAD_PLAN", "取样袋数 bags 必须为正整数")
        return dict(plan)

    def _public_plot(self, plot):
        return {k: v for k, v in plot.items() if k != "boundary_bbox"}

    def get_plot(self, plot_code):
        plot = self.plots.get(plot_code)
        if plot is None:
            raise DomainError(404, "PLOT_NOT_FOUND", f"地块 {plot_code} 不存在")
        return plot

    def get_trial_plan(self, actor, trial_code):
        self.require_role(actor, "admin", "lead")
        with self._lock:
            plots = [p for p in self.plots.values() if p["trial_code"] == trial_code]
            if not plots:
                raise DomainError(404, "TRIAL_NOT_FOUND", f"试验方案 {trial_code} 不存在")
            return {
                "trial_code": trial_code,
                "plot_count": len(plots),
                "plots": [self._public_plot(p) for p in sorted(plots, key=lambda p: p["code"])],
            }

    # -- 作业事件录入 ------------------------------------------------------ #

    def record_event(self, actor, plot_code, payload):
        self.require_role(actor, "admin", "operator")
        plot = self.get_plot(plot_code)
        stage = payload.get("stage")
        if stage not in STAGES:
            raise DomainError(400, "BAD_STAGE", f"未知阶段：{stage}", {"allowed": STAGES})

        missing = [f for f in STAGE_REQUIRED_FIELDS[stage] if payload.get(f) in (None, "")]
        if missing:
            raise DomainError(400, "MISSING_FIELD", f"阶段 {stage} 缺少字段", {"missing": missing})

        occurred = parse_dt(payload.get("occurred_at"))
        arrived = self.now()
        day = local_day(occurred, payload.get("occurred_at"))
        machine_code = payload.get("machine_code")
        operator_code = actor["actor_code"] if actor["role"] == "operator" else payload.get("operator_code", actor["actor_code"])

        gps = payload.get("gps")
        if gps is not None:
            gps = self._validate_gps(gps)

        if stage == "weighing":
            weight = payload["weight_kg"]
            moisture = payload["moisture_pct"]
            if not isinstance(weight, (int, float)) or weight <= 0:
                raise DomainError(400, "BAD_WEIGHT", "称重重量必须为正数")
            if not isinstance(moisture, (int, float)) or not 0 <= moisture <= 100:
                raise DomainError(400, "BAD_MOISTURE", "含水率必须在 0~100 之间")

        client_event_id = payload.get("client_event_id")
        with self._lock:
            if client_event_id:
                old = self.client_event_index.get(client_event_id)
                if old:
                    event = self.events[old]
                    # 只有原上报人可以重放自己的设备事件；他人复用编号直接拒绝。
                    if (actor["role"] == "operator"
                            and event["submitted_by"] != actor["actor_code"]):
                        raise DomainError(
                            409, "CLIENT_EVENT_FOREIGN",
                            "该客户端事件编号已被其他上报人使用",
                        )
                    # 同一设备事件重传：幂等返回原记录及其当前生效状态，绝不生成第二条。
                    return event, event["status"] == "clean"

            event = {
                "event_id": _new_id("evt"),
                "plot_code": plot_code,
                "trial_code": plot["trial_code"],
                "stage": stage,
                "occurred_at": occurred.isoformat(),
                "arrived_at": arrived.isoformat(),
                "day": day,
                "machine_code": machine_code,
                "operator_code": operator_code,
                "gps": gps,
                "maturity_grade": payload.get("maturity_grade"),
                "weight_kg": payload.get("weight_kg"),
                "moisture_pct": payload.get("moisture_pct"),
                "seal_code": payload.get("seal_code"),
                "bin_code": payload.get("bin_code"),
                "note": payload.get("note"),
                "client_event_id": client_event_id,
                "device_id": payload.get("device_id"),
                "submitted_by": actor["actor_code"],
                "status": "quarantined",   # 先隔离，冲突检测全部通过后才生效
                "conflict_ids": [],
            }

            reasons = []
            details = {}

            # 1) 定位漂移
            if gps is not None:
                lon, lat = gps["lon"], gps["lat"]
                min_lon, min_lat, max_lon, max_lat = plot["boundary_bbox"]
                inside_box = min_lon <= lon <= max_lon and min_lat <= lat <= max_lat
                inside = inside_box and point_in_polygon(lon, lat, plot["boundary"])
                if not inside:
                    reasons.append("GPS_DRIFT")
                    other = self._plot_at_point(lon, lat, exclude=plot_code)
                    details["gps"] = gps
                    details["submitted_plot"] = plot_code
                    details["positioned_plot"] = other
                    details["explanation"] = CONFLICT_LABELS["GPS_DRIFT"]
                    if other:
                        details["explanation"] += f"，坐标实际落在地块 {other} 内"

            # 2) 离线补传
            delay = (arrived - occurred).total_seconds()
            if delay > OFFLINE_LATE_THRESHOLD_SECONDS:
                reasons.append("OFFLINE_LATE")
                details.setdefault("delay_seconds", int(delay))
                details["offline_threshold_seconds"] = OFFLINE_LATE_THRESHOLD_SECONDS

            # 3) 封签重复
            if stage == "sealing":
                seal = payload["seal_code"]
                owners = [s for s in self.seal_index.get(seal, []) if s[0] != event["event_id"]]
                if owners:
                    reasons.append("SEAL_DUPLICATE")
                    details["seal_code"] = seal
                    details["existing_uses"] = [
                        {"event_id": eid, "plot_code": pc, "status": st} for eid, pc, st in owners
                    ]

            # 4) 阶段重复 / 顺序倒挂（只看已生效记录）
            accepted = self._accepted_events(plot_code)
            present = {e["stage"] for e in accepted}
            if stage in present:
                reasons.append("STAGE_DUPLICATE")
                details["existing_event_id"] = next(e["event_id"] for e in accepted if e["stage"] == stage)
            else:
                idx = STAGES.index(stage)
                later = [s for s in STAGES[idx + 1:] if s in present]
                missing_stages = [s for s in STAGES[:idx] if s not in present]
                if later or missing_stages:
                    reasons.append("ORDER_GAP")
                    details["later_accepted_stages"] = later
                    details["missing_stages"] = missing_stages

            self.events[event["event_id"]] = event
            self.events_by_plot[plot_code].append(event["event_id"])
            if client_event_id:
                self.client_event_index[client_event_id] = event["event_id"]
            if stage == "sealing":
                self.seal_index.setdefault(payload["seal_code"], []).append(
                    (event["event_id"], plot_code, "quarantined")
                )

            if reasons:
                conflict = self._open_conflict(event, reasons, details, actor)
                event["conflict_ids"].append(conflict["conflict_id"])
                return event, False

            self._apply_event(event)
            return event, True

    @staticmethod
    def _validate_gps(gps):
        if not isinstance(gps, dict) or "lat" not in gps or "lon" not in gps:
            raise DomainError(400, "BAD_GPS", "gps 必须包含 lat 与 lon")
        lat, lon = gps["lat"], gps["lon"]
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            raise DomainError(400, "BAD_GPS", "gps 坐标必须是数字")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise DomainError(400, "BAD_GPS", f"gps 坐标越界：{gps}")
        if lat == 0 and lon == 0:
            raise DomainError(400, "BAD_GPS", "gps 坐标为 (0,0)，疑似设备未定位")
        return {"lat": float(lat), "lon": float(lon)}

    def _plot_at_point(self, lon, lat, exclude=None):
        for code, plot in self.plots.items():
            if code == exclude:
                continue
            min_lon, min_lat, max_lon, max_lat = plot["boundary_bbox"]
            if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
                if point_in_polygon(lon, lat, plot["boundary"]):
                    return code
        return None

    def _accepted_events(self, plot_code):
        return [
            self.events[eid]
            for eid in self.events_by_plot.get(plot_code, [])
            if self.events[eid]["status"] == "clean"
        ]

    def _open_conflict(self, event, reasons, details, actor):
        conflict = {
            "conflict_id": _new_id("cfl"),
            "event_id": event["event_id"],
            "plot_code": event["plot_code"],
            "machine_code": event.get("machine_code"),
            "stage": event["stage"],
            "reasons": reasons,
            "reason_labels": [CONFLICT_LABELS[r] for r in reasons],
            "details": details,
            "status": "pending",
            "opened_by": actor["actor_code"],
            "opened_at": self.now().isoformat(),
            "resolution": None,
        }
        self.conflicts[conflict["conflict_id"]] = conflict
        return conflict

    def _apply_event(self, event):
        """把通过检测/审核通过的事件置为生效，并派生样本等产物。"""
        event["status"] = "clean"
        if event["stage"] == "sealing":
            seal = event["seal_code"]
            # 封签索引状态同步为生效。
            self.seal_index[seal] = [
                (eid, pc, "clean" if eid == event["event_id"] else st)
                for eid, pc, st in self.seal_index.get(seal, [])
            ]
            accepted = self._accepted_events(event["plot_code"])
            weighing = next((e for e in accepted if e["stage"] == "weighing"), None)
            plot = self.plots[event["plot_code"]]
            sample = {
                "sample_code": f"S-{event['plot_code']}-{seal}",
                "root_sample_code": None,  # 封样产生的是谱系根，下面回填
                "trial_code": plot["trial_code"],
                "plot_code": event["plot_code"],
                "variety_code": plot["variety_code"],
                "generation": plot["generation"],
                "sowing_batch": plot["sowing_batch"],
                "seal_code": seal,
                "sealing_event_id": event["event_id"],
                "status": "sealed",
                "holder": "field",
                "location": event.get("bin_code") or "field",
                "remaining_kg": weighing["weight_kg"] if weighing else None,
                "lineage": [
                    {
                        "action": "sealed",
                        "at": event["occurred_at"],
                        "by": event["operator_code"],
                        "event_id": event["event_id"],
                        "note": event.get("note"),
                    }
                ],
            }
            sample["root_sample_code"] = sample["sample_code"]
            self.samples[sample["sample_code"]] = sample
        elif event["stage"] == "storage":
            # 入库生效：同地块封样根样本转为在库并记录货位。
            for sample in self.samples.values():
                if (sample["plot_code"] == event["plot_code"]
                        and sample["root_sample_code"] == sample["sample_code"]
                        and sample["status"] == "sealed"):
                    sample["status"] = "stored"
                    sample["location"] = event["bin_code"]
                    sample["lineage"].append({
                        "action": "stored",
                        "at": event["occurred_at"],
                        "by": event["operator_code"],
                        "bin_code": event["bin_code"],
                        "event_id": event["event_id"],
                    })

    # -- 冲突审核 ---------------------------------------------------------- #

    def list_conflicts(self, actor, status=None, plot_code=None, machine_code=None):
        self.require_role(actor, "admin", "lead")
        with self._lock:
            items = list(self.conflicts.values())
            if status:
                items = [c for c in items if c["status"] == status]
            if plot_code:
                items = [c for c in items if c["plot_code"] == plot_code]
            if machine_code:
                items = [c for c in items if c["machine_code"] == machine_code]
            return sorted(items, key=lambda c: c["opened_at"])

    def get_conflict(self, conflict_id):
        conflict = self.conflicts.get(conflict_id)
        if conflict is None:
            raise DomainError(404, "CONFLICT_NOT_FOUND", f"冲突单 {conflict_id} 不存在")
        return conflict

    def resolve_conflict(self, actor, conflict_id, decision, note=""):
        self.require_role(actor, "admin", "lead")
        if decision not in ("accept", "reject"):
            raise DomainError(400, "BAD_DECISION", "decision 只能是 accept 或 reject")
        with self._lock:
            conflict = self.get_conflict(conflict_id)
            if conflict["status"] != "pending":
                raise DomainError(409, "CONFLICT_CLOSED", "冲突单已审核，不能再次覆盖")
            event = self.events[conflict["event_id"]]

            if decision == "accept":
                # 放行前复核封签：若这期间同一封签已被另一记录生效，仍然不能放行。
                if "SEAL_DUPLICATE" in conflict["reasons"]:
                    clash = [
                        s for s in self.seal_index.get(event["seal_code"], [])
                        if s[0] != event["event_id"] and s[2] == "clean"
                    ]
                    if clash:
                        raise DomainError(
                            409,
                            "SEAL_STILL_CLASHING",
                            "封签已被其他生效样本占用，不能放行；请先更换封签并重新上报",
                            {"existing_uses": [
                                {"event_id": eid, "plot_code": pc} for eid, pc, _ in clash
                            ]},
                        )
                self._apply_event(event)
            else:
                event["status"] = "rejected"
                if event["stage"] == "sealing":
                    seal = event["seal_code"]
                    self.seal_index[seal] = [
                        (eid, pc, "rejected" if eid == event["event_id"] else st)
                        for eid, pc, st in self.seal_index.get(seal, [])
                    ]

            conflict["status"] = "resolved"
            conflict["resolution"] = {
                "decision": decision,
                "note": note,
                "by": actor["actor_code"],
                "at": self.now().isoformat(),
            }
            return conflict

    def machine_prior_plots(self, actor, conflict_id):
        """异常反查：冲突发生前，同一台机器当天已经经过哪些地块。"""
        self.require_role(actor, "admin", "lead")
        with self._lock:
            conflict = self.get_conflict(conflict_id)
            event = self.events[conflict["event_id"]]
            machine = event.get("machine_code")
            if not machine:
                return {"machine_code": None, "day": event["day"], "prior_plots": [], "trace": []}
            return self._trace_until(machine, event["day"], event["occurred_at"])

    def machine_trace(self, actor, machine_code, day):
        self.require_role(actor, "admin", "lead")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day or ""):
            raise DomainError(400, "BAD_DAY", "date 格式应为 YYYY-MM-DD")
        with self._lock:
            return self._trace_until(machine_code, day, None)

    def _trace_until(self, machine_code, day, until_occurred=None):
        chain = [
            e for e in self.events.values()
            if e["machine_code"] == machine_code and e["day"] == day
            and e["status"] in ("clean", "quarantined")
        ]
        chain.sort(key=lambda e: e["occurred_at"])
        prior = []
        trace = []
        for e in chain:
            row = {
                "event_id": e["event_id"],
                "occurred_at": e["occurred_at"],
                "plot_code": e["plot_code"],
                "stage": e["stage"],
                "status": e["status"],
                "gps": e["gps"],
                "operator_code": e["operator_code"],
            }
            if until_occurred is None or e["occurred_at"] < until_occurred:
                trace.append(row)
                if e["plot_code"] not in prior:
                    prior.append(e["plot_code"])
        return {
            "machine_code": machine_code,
            "day": day,
            "prior_plots": prior,
            "trace": trace,
            "anomaly_event_id": until_occurred,
        }

    # -- 查询：作业进度与最小可见 ----------------------------------------- #

    def list_plot_events(self, actor, plot_code):
        self.require_role(actor, "admin", "lead")
        self.get_plot(plot_code)
        with self._lock:
            return [self.events[eid] for eid in self.events_by_plot.get(plot_code, [])]

    def get_client_event(self, actor, client_event_id):
        """机手只能查自己上报的记录（用于离线端确认补传结果）。"""
        with self._lock:
            event_id = self.client_event_index.get(client_event_id)
            if not event_id:
                raise DomainError(404, "EVENT_NOT_FOUND", "找不到该客户端事件编号")
            event = self.events[event_id]
            if actor["role"] == "operator" and event["submitted_by"] != actor["actor_code"]:
                # 不暴露他人作业信息，统一按不存在处理。
                raise DomainError(404, "EVENT_NOT_FOUND", "找不到该客户端事件编号")
            return event

    def work_card(self, actor, plot_code):
        """机手视角：只给当前作业所需，不含品种代次等研究信息。"""
        self.require_role(actor, "admin", "operator")
        plot = self.get_plot(plot_code)
        with self._lock:
            accepted = {e["stage"] for e in self._accepted_events(plot_code)}
            next_stage = next((s for s in STAGES if s not in accepted), None)
            return {
                "plot_code": plot["code"],
                "next_stage": next_stage,
                "required_fields": STAGE_REQUIRED_FIELDS.get(next_stage, []),
                "completed_stages": [s for s in STAGES if s in accepted],
                "pending_conflicts": [
                    cid for eid in self.events_by_plot[plot_code]
                    for cid in self.events[eid]["conflict_ids"]
                    if self.conflicts[cid]["status"] == "pending"
                    and self.events[eid].get("submitted_by") == actor["actor_code"]
                ],
            }

    def work_queue(self, actor):
        """机手当前可作业的地块清单，仅含下一步动作，不含研究元数据。"""
        self.require_role(actor, "admin", "operator")
        with self._lock:
            queue = []
            for code in sorted(self.plots):
                card = self.work_card(actor, code)
                if card["next_stage"]:
                    queue.append({
                        "plot_code": card["plot_code"],
                        "next_stage": card["next_stage"],
                        "required_fields": card["required_fields"],
                    })
            return {"queue": queue}

    # -- 研究负责人：按方案重建有效产量与样本 ----------------------------- #

    def trial_results(self, actor, trial_code):
        self.require_role(actor, "admin", "lead")
        with self._lock:
            plots = sorted(
                (p for p in self.plots.values() if p["trial_code"] == trial_code),
                key=lambda p: p["code"],
            )
            if not plots:
                raise DomainError(404, "TRIAL_NOT_FOUND", f"试验方案 {trial_code} 不存在")
            rows = []
            for plot in plots:
                accepted = self._accepted_events(plot["code"])
                by_stage = {e["stage"]: e for e in accepted}
                weighing = by_stage.get("weighing")
                harvest = by_stage.get("harvest")
                sealing = by_stage.get("sealing")
                storage = by_stage.get("storage")
                effective = all(by_stage.get(s) for s in ("harvest", "weighing", "sealing"))

                weight = weighing["weight_kg"] if weighing else None
                moisture = weighing["moisture_pct"] if weighing else None
                standard_weight = None
                if weight is not None and moisture is not None:
                    standard_weight = round(
                        weight * (100 - moisture) / (100 - STANDARD_MOISTURE_PCT), 3
                    )

                rows.append({
                    "plot_code": plot["code"],
                    "variety_code": plot["variety_code"],
                    "variety_name": plot.get("variety_name"),
                    "generation": plot["generation"],
                    "sowing_batch": plot["sowing_batch"],
                    "sampling_plan": plot["sampling_plan"],
                    "effective": effective,
                    "invalid_reasons": self._invalid_reasons(by_stage),
                    "harvest_at": harvest["occurred_at"] if harvest else None,
                    "machine_code": harvest["machine_code"] if harvest else None,
                    "weight_kg": weight,
                    "moisture_pct": moisture,
                    "standard_weight_kg": standard_weight,
                    "standard_moisture_pct": STANDARD_MOISTURE_PCT,
                    "seal_code": sealing["seal_code"] if sealing else None,
                    "bin_code": storage["bin_code"] if storage else None,
                })
            samples = self._trial_samples(trial_code)
            return {
                "trial_code": trial_code,
                "effective_plot_count": sum(1 for r in rows if r["effective"]),
                "plot_count": len(rows),
                "plots": rows,
                "samples": samples,
            }

    @staticmethod
    def _invalid_reasons(by_stage):
        reasons = []
        for stage, label in (
            ("harvest", "缺少正式收获生效记录"),
            ("weighing", "缺少称重生效记录"),
            ("sealing", "缺少封样生效记录"),
        ):
            if not by_stage.get(stage):
                reasons.append(label)
        return reasons

    def _trial_samples(self, trial_code, include_voided=False):
        roots = [
            s for s in self.samples.values()
            if s["trial_code"] == trial_code and s["root_sample_code"] == s["sample_code"]
        ]
        result = []
        for root in sorted(roots, key=lambda s: s["sample_code"]):
            family = [s for s in self.samples.values() if s["root_sample_code"] == root["sample_code"]]
            for s in sorted(family, key=lambda x: x["sample_code"]):
                if s["status"] == "voided" and not include_voided:
                    continue
                result.append(self._sample_view(s))
        return result

    def _sample_view(self, sample):
        return {
            "sample_code": sample["sample_code"],
            "root_sample_code": sample["root_sample_code"],
            "plot_code": sample["plot_code"],
            "variety_code": sample["variety_code"],
            "generation": sample["generation"],
            "sowing_batch": sample["sowing_batch"],
            "seal_code": sample["seal_code"],
            "status": sample["status"],
            "holder": sample["holder"],
            "location": sample["location"],
            "remaining_kg": sample["remaining_kg"],
            "lineage": sample["lineage"],
        }

    # -- 样本转交与实验室谱系 --------------------------------------------- #

    def get_sample(self, code):
        sample = self.samples.get(code)
        if sample is None:
            raise DomainError(404, "SAMPLE_NOT_FOUND", f"样本 {code} 不存在")
        return sample

    def transfer_sample(self, actor, code, payload):
        """封样/入库样本转交实验室，只追加转交记录，谱系不断。"""
        self.require_role(actor, "admin", "lead")
        with self._lock:
            sample = self.get_sample(code)
            if sample["status"] == "voided":
                raise DomainError(409, "SAMPLE_VOIDED", "已作废样本不能转交")
            carrier = payload.get("carrier")
            lab_code = payload.get("lab_code", "LAB-01")
            at = parse_dt(payload.get("at"))
            sample["status"] = "transferred"
            sample["holder"] = lab_code
            sample["location"] = lab_code
            sample["lineage"].append({
                "action": "transferred",
                "at": at.isoformat(),
                "by": actor["actor_code"],
                "carrier": carrier,
                "lab_code": lab_code,
                "note": payload.get("note"),
            })
            return self._sample_view(sample)

    def _require_lab_holder(self, actor, sample):
        if actor["role"] not in ("lab", "admin"):
            raise DomainError(403, "FORBIDDEN", "只有实验室可以处理在库样本")
        if sample["status"] == "voided":
            raise DomainError(409, "SAMPLE_VOIDED", "已作废样本不能再操作")
        if sample["holder"] == "field":
            raise DomainError(409, "NOT_TRANSFERRED", "样本尚未转交实验室")

    def split_sample(self, actor, code, payload):
        """重分装：派生子样本，数量从母样本扣减，谱系回挂原始根样本。"""
        self.require_role(actor, "lab", "admin")
        parts = payload.get("parts")
        if not isinstance(parts, list) or not parts:
            raise DomainError(400, "BAD_PARTS", "parts 必须是非空列表，每项含 quantity_kg")
        for part in parts:
            q = part.get("quantity_kg")
            if not isinstance(q, (int, float)) or q <= 0:
                raise DomainError(400, "BAD_PARTS", "每个分装数量必须为正数")
        at = parse_dt(payload.get("at"))
        with self._lock:
            sample = self.get_sample(code)
            self._require_lab_holder(actor, sample)
            total = sum(p["quantity_kg"] for p in parts)
            if sample["remaining_kg"] is not None and total > sample["remaining_kg"] + 1e-9:
                raise DomainError(
                    409,
                    "INSUFFICIENT_QUANTITY",
                    f"分装总量 {total}kg 超过当前余量 {sample['remaining_kg']}kg",
                )
            children = []
            root = sample["root_sample_code"]
            for part in parts:
                seq = len([s for s in self.samples.values()
                           if s["root_sample_code"] == root]) + 1
                child_code = f"{root}.{seq}"
                child = {
                    "sample_code": child_code,
                    "root_sample_code": sample["root_sample_code"],
                    "trial_code": sample["trial_code"],
                    "plot_code": sample["plot_code"],
                    "variety_code": sample["variety_code"],
                    "generation": sample["generation"],
                    "sowing_batch": sample["sowing_batch"],
                    "seal_code": sample["seal_code"],
                    "status": "transferred",
                    "holder": sample["holder"],
                    "location": part.get("location", sample["location"]),
                    "remaining_kg": part["quantity_kg"],
                    "lineage": [
                        {
                            "action": "split_from",
                            "from": code,
                            "at": at.isoformat(),
                            "by": actor["actor_code"],
                            "quantity_kg": part["quantity_kg"],
                            "purpose": part.get("purpose"),
                            "note": payload.get("note"),
                        }
                    ],
                }
                self.samples[child_code] = child
                children.append(child_code)

            if sample["remaining_kg"] is not None:
                sample["remaining_kg"] = round(sample["remaining_kg"] - total, 6)
            sample["lineage"].append({
                "action": "split",
                "at": at.isoformat(),
                "by": actor["actor_code"],
                "quantity_kg": total,
                "children": children,
                "purpose": payload.get("note"),
            })
            return {"parent": self._sample_view(sample),
                    "children": [self._sample_view(self.samples[c]) for c in children]}

    def consume_sample(self, actor, code, payload):
        """耗用：登记用途与数量，保留剩余，谱系可追溯。"""
        self.require_role(actor, "lab", "admin")
        qty = payload.get("quantity_kg")
        purpose = payload.get("purpose")
        if not isinstance(qty, (int, float)) or qty <= 0:
            raise DomainError(400, "BAD_QUANTITY", "耗用数量必须为正数")
        if not purpose:
            raise DomainError(400, "MISSING_FIELD", "耗用必须登记 purpose")
        at = parse_dt(payload.get("at"))
        with self._lock:
            sample = self.get_sample(code)
            self._require_lab_holder(actor, sample)
            if sample["remaining_kg"] is not None and qty > sample["remaining_kg"] + 1e-9:
                raise DomainError(
                    409, "INSUFFICIENT_QUANTITY",
                    f"耗用量 {qty}kg 超过当前余量 {sample['remaining_kg']}kg",
                )
            sample["remaining_kg"] = round(sample["remaining_kg"] - qty, 6)
            sample["lineage"].append({
                "action": "consumed",
                "at": at.isoformat(),
                "by": actor["actor_code"],
                "quantity_kg": qty,
                "purpose": purpose,
                "remaining_kg": sample["remaining_kg"],
                "note": payload.get("note"),
            })
            return self._sample_view(sample)

    def void_sample(self, actor, code, payload):
        """作废：状态翻转且记录原因，历史谱系保留。"""
        self.require_role(actor, "lab", "admin")
        reason = payload.get("reason")
        if not reason:
            raise DomainError(400, "MISSING_FIELD", "作废必须填写 reason")
        at = parse_dt(payload.get("at"))
        with self._lock:
            sample = self.get_sample(code)
            if sample["status"] == "voided":
                raise DomainError(409, "SAMPLE_VOIDED", "样本已经是作废状态")
            sample["status"] = "voided"
            sample["lineage"].append({
                "action": "voided",
                "at": at.isoformat(),
                "by": actor["actor_code"],
                "reason": reason,
                "note": payload.get("note"),
            })
            return self._sample_view(sample)

    def sample_lineage(self, actor, code):
        with self._lock:
            sample = self.get_sample(code)
            # 实验室只能看已交实验室（本室 holder）的样本；负责人看全部。
            if actor["role"] == "lab" and sample["holder"] == "field":
                raise DomainError(403, "FORBIDDEN", "样本尚未转交实验室")
            if actor["role"] == "operator":
                raise DomainError(403, "FORBIDDEN", "机手无权查看样本谱系")
            family = sorted(
                (s for s in self.samples.values()
                 if s["root_sample_code"] == sample["root_sample_code"]),
                key=lambda s: s["sample_code"],
            )
            return {
                "root_sample_code": sample["root_sample_code"],
                "samples": [self._sample_view(s) for s in family],
            }
