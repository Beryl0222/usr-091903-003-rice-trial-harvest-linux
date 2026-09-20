"""水稻试验收获隔离的运行入口与 HTTP 适配层。

路由概览（详见 README）：
  POST   /admin/plots                      冻结试验方案（admin）
  GET    /admin/trials/<code>              查看冻结方案（admin/lead）
  POST   /plots/<code>/events              田间上报作业事件（admin/operator）
  GET    /plots/<code>/events              查询地块全部事件（admin/lead）
  GET    /plots/<code>/work-card           机手当前作业卡（admin/operator）
  GET    /operator/queue                   机手待作业队列（admin/operator）
  GET    /client-events/<id>               按客户端事件编号查补传结果
  GET    /conflicts                        冲突单列表（admin/lead）
  POST   /conflicts/<id>/resolve           冲突审核（admin/lead）
  GET    /conflicts/<id>/machine-prior-plots  异常反查机器此前经过的田块
  GET    /machines/<code>/trace?date=YYYY-MM-DD  机器当日轨迹
  GET    /trials/<code>/results            重建有效产量与样本清单（admin/lead）
  POST   /samples/<code>/transfer          样本转交实验室（admin/lead）
  POST   /samples/<code>/split             重分装（lab/admin）
  POST   /samples/<code>/consume           耗用（lab/admin）
  POST   /samples/<code>/void              作废（lab/admin）
  GET    /samples/<code>/lineage           样本谱系
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from harvest_domain import DomainError, HarvestService

SERVICE_ID = "rice-trial-harvest"
SERVICE_NAME = "水稻试验收获隔离"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# 进程级单例：田间设备并发上报，领域服务内部已加锁。
_SERVICE = HarvestService()


def get_service():
    return _SERVICE


def reset_service():
    """供测试重置全部状态。"""
    global _SERVICE
    _SERVICE = HarvestService()
    return _SERVICE


class Handler(BaseHTTPRequestHandler):
    """HTTP 适配：鉴权、路由、JSON 编解码，领域规则全部在 HarvestService。"""

    service = None  # 允许测试注入；None 时使用进程单例

    def _svc(self):
        return self.service or get_service()

    # -- 基础收发 ---------------------------------------------------------- #

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error):
        self._send_json(error.status, error.to_dict())

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DomainError(400, "BAD_JSON", "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise DomainError(400, "BAD_JSON", "请求体必须是 JSON 对象")
        return data

    def _actor(self):
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        return self._svc().authenticate(token)

    def _handle(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                self._send_json(200, health_payload())
                return
            try:
                actor = self._actor()
            except DomainError:
                # 未携带/错误令牌访问根本不存在的路由时仍按 404 处理，
                # 避免路由存在性被未认证调用方探测。
                if not self._route_exists(method, path):
                    self._send_json(404, {"error": "NOT_FOUND", "message": f"未知路由：{method} {path}"})
                    return
                raise
            body = self._read_json() if method == "POST" else {}
            self.route(method, path, query, body, actor)
        except DomainError as error:
            self._send_error(error)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, *_args):
        return

    _ROUTE_PATTERNS = (
        ("POST", r"/admin/plots"),
        ("GET", r"/admin/trials/([^/]+)"),
        ("GET", r"/plots/([^/]+)/events"),
        ("POST", r"/plots/([^/]+)/events"),
        ("GET", r"/plots/([^/]+)/work-card"),
        ("GET", r"/operator/queue"),
        ("GET", r"/client-events/([^/]+)"),
        ("GET", r"/conflicts"),
        ("POST", r"/conflicts/([^/]+)/resolve"),
        ("GET", r"/conflicts/([^/]+)/machine-prior-plots"),
        ("GET", r"/machines/([^/]+)/trace"),
        ("GET", r"/trials/([^/]+)/results"),
        ("POST", r"/samples/([^/]+)/(transfer|split|consume|void)"),
        ("GET", r"/samples/([^/]+)/lineage"),
    )

    def _route_exists(self, method, path):
        return any(method == m and re.fullmatch(p, path) for m, p in self._ROUTE_PATTERNS)

    # -- 路由 -------------------------------------------------------------- #

    def route(self, method, path, query, body, actor):
        svc = self._svc()

        if method == "GET" and path == "/health":
            self._send_json(200, health_payload())
            return

        m = re.fullmatch(r"/admin/plots", path)
        if method == "POST" and m:
            self._send_json(201, svc.freeze_plot(actor, body))
            return

        m = re.fullmatch(r"/admin/trials/([^/]+)", path)
        if method == "GET" and m:
            self._send_json(200, svc.get_trial_plan(actor, m.group(1)))
            return

        m = re.fullmatch(r"/plots/([^/]+)/events", path)
        if method == "POST" and m:
            event, clean = svc.record_event(actor, m.group(1), body)
            self._send_json(200 if clean else 202, {
                "event": event,
                "accepted": clean,
                "conflicts": [
                    svc.get_conflict(cid) for cid in event["conflict_ids"]
                ],
            })
            return
        if method == "GET" and m:
            self._send_json(200, {"events": svc.list_plot_events(actor, m.group(1))})
            return

        m = re.fullmatch(r"/plots/([^/]+)/work-card", path)
        if method == "GET" and m:
            self._send_json(200, svc.work_card(actor, m.group(1)))
            return

        if method == "GET" and path == "/operator/queue":
            self._send_json(200, svc.work_queue(actor))
            return

        m = re.fullmatch(r"/client-events/([^/]+)", path)
        if method == "GET" and m:
            self._send_json(200, svc.get_client_event(actor, m.group(1)))
            return

        if method == "GET" and path == "/conflicts":
            self._send_json(200, {"conflicts": svc.list_conflicts(
                actor,
                status=query.get("status", [None])[0],
                plot_code=query.get("plot", [None])[0],
                machine_code=query.get("machine", [None])[0],
            )})
            return

        m = re.fullmatch(r"/conflicts/([^/]+)/resolve", path)
        if method == "POST" and m:
            self._send_json(200, svc.resolve_conflict(
                actor, m.group(1), body.get("decision"), body.get("note", "")
            ))
            return

        m = re.fullmatch(r"/conflicts/([^/]+)/machine-prior-plots", path)
        if method == "GET" and m:
            self._send_json(200, svc.machine_prior_plots(actor, m.group(1)))
            return

        m = re.fullmatch(r"/machines/([^/]+)/trace", path)
        if method == "GET" and m:
            self._send_json(200, svc.machine_trace(
                actor, m.group(1), query.get("date", [""])[0]
            ))
            return

        m = re.fullmatch(r"/trials/([^/]+)/results", path)
        if method == "GET" and m:
            self._send_json(200, svc.trial_results(actor, m.group(1)))
            return

        m = re.fullmatch(r"/samples/([^/]+)/(transfer|split|consume|void)", path)
        if method == "POST" and m:
            code, action = m.group(1), m.group(2)
            if action == "transfer":
                self._send_json(200, svc.transfer_sample(actor, code, body))
            elif action == "split":
                self._send_json(200, svc.split_sample(actor, code, body))
            elif action == "consume":
                self._send_json(200, svc.consume_sample(actor, code, body))
            else:
                self._send_json(200, svc.void_sample(actor, code, body))
            return

        m = re.fullmatch(r"/samples/([^/]+)/lineage", path)
        if method == "GET" and m:
            self._send_json(200, svc.sample_lineage(actor, m.group(1)))
            return

        self._send_json(404, {"error": "NOT_FOUND", "message": f"未知路由：{method} {path}"})


def make_server(port=0, service=None):
    """供测试构造绑定随机端口的服务器。"""
    handler = Handler
    if service is not None:
        class _BoundHandler(Handler):
            pass
        _BoundHandler.service = service
        handler = _BoundHandler
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        from harvest_domain import STAGES
        assert health_payload()["service"] == SERVICE_ID
        assert len(STAGES) == 7
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
