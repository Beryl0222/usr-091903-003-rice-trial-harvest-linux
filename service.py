"""水稻试验收获隔离的运行入口与 HTTP 接口。

路由总览（除 /health 外均需 Bearer 令牌）
==========================================
方案与作业
* POST   /v1/plans                        冻结试验方案（researcher/admin）
* GET    /v1/plans/{code}
* POST   /v1/operations                   登记收获作业（researcher/admin）
* GET    /v1/operations/{id}
田间记录
* POST   /v1/operations/{id}/events       上报阶段事件（机手限本人作业）
* GET    /v1/operations/{id}/events
冲突处理
* GET    /v1/conflicts                    冲突清单（researcher/admin）
* GET    /v1/conflicts/{id}
* POST   /v1/conflicts/{id}/resolve       裁决（高危采信限负责人/管理员）
* GET    /v1/conflicts/{id}/trace         反查机器当天此前经过的田块
* GET    /v1/machines/{id}/stops          机器停靠轨迹
样本谱系
* GET    /v1/samples/{code}
* POST   /v1/samples/{code}/transfer      转交实验室（lab）
* POST   /v1/samples/{code}/split         重分装（lab）
* POST   /v1/samples/{code}/consume       耗用（lab）
* POST   /v1/samples/{code}/void          作废（lab）
重建与机手视图
* GET    /v1/plans/{code}/rebuild         重建有效产量与样本清单（researcher/admin）
* GET    /v1/me/work                      机手当前作业最小视图
* GET    /v1/operations/{id}/brief
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from auth import Auth, AuthError, require_role
from domain import DomainError, HarvestService
from store import AppendStore

SERVICE_ID = "rice-trial-harvest"
SERVICE_NAME = "水稻试验收获隔离"

DATA_DIR = os.environ.get("RHT_DATA_DIR", os.path.join(os.getcwd(), "data"))


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class ServiceContext:
    """存储/鉴权/领域服务的组合根，测试可注入自己的实例。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.store = AppendStore(data_dir)
        self.auth = Auth(data_dir)
        self.domain = HarvestService(self.store)
        self.lock = threading.RLock()

    def reset_domain(self):
        """测试辅助：存储变更后重建领域索引。"""
        self.domain = HarvestService(self.store)


_CONTEXT = None


def get_context():
    global _CONTEXT
    if _CONTEXT is None:
        _CONTEXT = ServiceContext(DATA_DIR)
    return _CONTEXT


def set_context(context):
    """测试注入。"""
    global _CONTEXT
    _CONTEXT = context


# (角色集合, 是否机手限本人作业)
class Route:
    def __init__(self, method, pattern, roles, handler, own_op=False):
        self.method = method
        self.pattern = pattern
        self.roles = roles
        self.handler = handler
        self.own_op = own_op


class Handler(BaseHTTPRequestHandler):
    """JSON API + 健康检查入口。"""

    server_version = "RiceHarvest/1.0"

    # ---- 响应工具 -------------------------------------------------------

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, code, message, details=None):
        self._send_json(status, {"error": {
            "code": code, "message": message, "details": details or {},
        }})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DomainError("invalid_json", "请求体不是合法 JSON", 400)
        if not isinstance(data, dict):
            raise DomainError("invalid_body", "请求体必须是 JSON 对象", 400)
        return data

    def _actor(self, ctx):
        return ctx.auth.authenticate(self.headers.get("Authorization"))

    # ---- 入口 -----------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health":
            self._send_json(200, health_payload())
            return
        route, kwargs = self._match(method, path)
        if route is None:
            self._send_error(404, "not_found", f"未找到接口 {method} {path}")
            return
        ctx = get_context()
        try:
            actor = self._actor(ctx)
            require_role(actor, route.roles)
            body = self._read_body() if method == "POST" else {}
            query = parse_qs(parsed.query)
            if route.own_op and actor["role"] == "operator":
                op = ctx.domain._get_operation(kwargs["op_id"])
                if op["operator_id"] != actor["user_id"]:
                    raise AuthError(
                        "forbidden", "机手只能接触自己当前作业所需的信息", 403
                    )
            with ctx.lock:
                result = route.handler(ctx.domain, actor, body, query, kwargs)
        except AuthError as exc:
            self._send_error(exc.status, exc.code, exc.message)
            return
        except DomainError as exc:
            self._send_error(exc.status, exc.code, exc.message, exc.details)
            return
        except Exception as exc:  # noqa: BLE001 - 兜底，保证错误信封一致
            self._send_error(500, "internal_error", f"服务内部错误: {exc}")
            return
        status, payload = result
        self._send_json(status, payload)

    def log_message(self, *_args):
        return

    # ---- 路由表 ---------------------------------------------------------

    def _match(self, method, path):
        for route in ROUTES:
            if route.method != method:
                continue
            parts = path.strip("/").split("/")
            pattern = route.pattern.strip("/").split("/")
            if len(parts) != len(pattern):
                continue
            kwargs = {}
            ok = True
            for actual, expected in zip(parts, pattern):
                if expected.startswith("{") and expected.endswith("}"):
                    kwargs[expected[1:-1]] = actual
                elif actual != expected:
                    ok = False
                    break
            if ok:
                return route, kwargs
        return None, {}


# ---------------------------------------------------------------------------
# 接口处理函数：返回 (HTTP 状态, 载荷)
# ---------------------------------------------------------------------------

LEAD_ROLES = ("researcher", "admin")
LAB_ROLES = ("lab", "admin")
FIELD_ROLES = ("operator", "researcher", "admin")


def h_create_plan(d, actor, body, _q, _k):
    return 201, d.create_plan(body, actor)


def h_get_plan(d, actor, body, _q, kwargs):
    plan = d.get_plan(kwargs["code"])
    if actor["role"] == "operator":
        # 机手无需接触其他小区的品种代次/边界，只给方案骨架
        return 200, {
            "code": plan["code"],
            "season": plan["season"],
            "status": plan["status"],
            "plot_codes": [p["plot_code"] for p in plan["plots"]],
        }
    return 200, plan


def h_create_operation(d, actor, body, _q, _k):
    return 201, d.create_operation(body, actor)


def h_get_operation(d, actor, body, _q, kwargs):
    return 200, d._get_operation(kwargs["op_id"])


def h_submit_event(d, actor, body, _q, kwargs):
    outcome, record = d.submit_event(kwargs["op_id"], body, actor)
    status = {"accepted": 201, "replay": 200, "quarantined": 202}[outcome]
    return status, {"outcome": outcome, **record}


def h_list_events(d, actor, body, _q, kwargs):
    op = d._get_operation(kwargs["op_id"])
    return 200, {
        "operation_id": op["id"],
        "events": d.events_by_op.get(op["id"], []),
    }


def h_list_conflicts(d, actor, body, query, _k):
    op_id = (query.get("op_id") or [None])[0]
    status_filter = (query.get("status") or ["open"])[0]
    return 200, {"conflicts": d.list_conflicts(op_id, status_filter)}


def h_get_conflict(d, actor, body, _q, kwargs):
    conflict = d.conflicts.get(kwargs["conflict_id"])
    if not conflict:
        raise DomainError("conflict_not_found", "冲突单不存在", 404)
    return 200, conflict


def h_resolve_conflict(d, actor, body, _q, kwargs):
    return 200, d.resolve_conflict(kwargs["conflict_id"], body, actor)


def h_trace_conflict(d, actor, body, _q, kwargs):
    return 200, d.trace_conflict(kwargs["conflict_id"])


def h_machine_stops(d, actor, body, query, kwargs):
    day = (query.get("day") or [None])[0]
    before = (query.get("before") or [None])[0]
    plot_code = (query.get("plot_code") or [None])[0]
    stops = d.machine_stops(kwargs["machine_id"], day=day,
                            before_iso=before, plot_code=plot_code)
    return 200, {"machine_id": kwargs["machine_id"], "day": day, "stops": stops}


def h_get_sample(d, actor, body, _q, kwargs):
    return 200, d.sample_view(kwargs["sample_code"])


def h_transfer_sample(d, actor, body, _q, kwargs):
    return 200, d.transfer_sample(kwargs["sample_code"], body, actor)


def h_split_sample(d, actor, body, _q, kwargs):
    return 200, d.split_sample(kwargs["sample_code"], body, actor)


def h_consume_sample(d, actor, body, _q, kwargs):
    return 200, d.consume_sample(kwargs["sample_code"], body, actor)


def h_void_sample(d, actor, body, _q, kwargs):
    return 200, d.void_sample(kwargs["sample_code"], body, actor)


def h_rebuild_plan(d, actor, body, _q, kwargs):
    return 200, d.rebuild_plan(kwargs["code"])


def h_my_work(d, actor, body, _q, _k):
    if actor["role"] == "operator":
        return 200, {"operations": d.operator_workload(actor)}
    return 200, {"operations": [
        d.operator_view(op["id"]) for op in sorted(
            d.operations.values(), key=lambda o: o["created_at"])
    ]}


def h_operation_brief(d, actor, body, _q, kwargs):
    return 200, d.operator_view(kwargs["op_id"], actor)


ROUTES = [
    Route("POST", "/v1/plans", LEAD_ROLES, h_create_plan),
    Route("GET", "/v1/plans/{code}", FIELD_ROLES, h_get_plan),
    Route("GET", "/v1/plans/{code}/rebuild", LEAD_ROLES, h_rebuild_plan),
    Route("POST", "/v1/operations", LEAD_ROLES, h_create_operation),
    Route("GET", "/v1/operations/{op_id}", LEAD_ROLES, h_get_operation),
    Route("GET", "/v1/operations/{op_id}/events",
          FIELD_ROLES, h_list_events, own_op=True),
    Route("POST", "/v1/operations/{op_id}/events",
          FIELD_ROLES, h_submit_event, own_op=True),
    Route("GET", "/v1/operations/{op_id}/brief",
          FIELD_ROLES, h_operation_brief, own_op=True),
    Route("GET", "/v1/conflicts", LEAD_ROLES, h_list_conflicts),
    Route("GET", "/v1/conflicts/{conflict_id}", LEAD_ROLES, h_get_conflict),
    Route("POST", "/v1/conflicts/{conflict_id}/resolve",
          LEAD_ROLES, h_resolve_conflict),
    Route("GET", "/v1/conflicts/{conflict_id}/trace",
          LEAD_ROLES, h_trace_conflict),
    Route("GET", "/v1/machines/{machine_id}/stops", LEAD_ROLES, h_machine_stops),
    Route("GET", "/v1/samples/{sample_code}",
          ("researcher", "admin", "lab"), h_get_sample),
    Route("POST", "/v1/samples/{sample_code}/transfer", LAB_ROLES, h_transfer_sample),
    Route("POST", "/v1/samples/{sample_code}/split", LAB_ROLES, h_split_sample),
    Route("POST", "/v1/samples/{sample_code}/consume", LAB_ROLES, h_consume_sample),
    Route("POST", "/v1/samples/{sample_code}/void", LAB_ROLES, h_void_sample),
    Route("GET", "/v1/me/work", FIELD_ROLES, h_my_work),
]


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default=DATA_DIR)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        ctx = ServiceContext(args.data_dir)
        set_context(ctx)
        assert isinstance(ctx.domain, HarvestService)
        print(f"基础检查通过；数据目录 {ctx.data_dir}；用户 {len(ctx.auth.users)} 个")
        return
    set_context(ServiceContext(args.data_dir))
    ctx = get_context()
    print(f"{SERVICE_NAME} 启动于 :{args.port}，数据目录 {ctx.data_dir}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
