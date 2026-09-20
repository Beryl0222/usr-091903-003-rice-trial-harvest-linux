"""HTTP 接口端到端测试：鉴权、RBAC、机手最小视图、完整业务闭环。"""

import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from domain import _meters_per_degree, iso as iso_dt
from service import Handler, ServiceContext
from store import AppendStore
from datetime import datetime, timedelta, timezone


def rect(lng, lat, w_m=40.0, h_m=40.0):
    mx, my = _meters_per_degree(lat)
    dlng, dlat = (w_m / 2) / mx, (h_m / 2) / my
    return [
        [lng - dlng, lat - dlat],
        [lng + dlng, lat - dlat],
        [lng + dlng, lat + dlat],
        [lng - dlng, lat + dlat],
    ]


class ApiClient:
    def __init__(self, base_url, tokens):
        self.base_url = base_url
        self.tokens = tokens

    def call(self, method, path, role=None, body=None):
        headers = {"Content-Type": "application/json"}
        if role:
            headers["Authorization"] = f"Bearer {self.tokens[role]}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)


PLAN_BODY = {
    "code": "T2026-HTTP",
    "season": "2026 秋收",
    "sampling_scheme": {"sample_count": 1},
    "plots": [
        {
            "plot_code": "P-1",
            "geometry": rect(104.02, 30.01),
            "cultivar": "川优 6203",
            "cultivar_generation": "F6",
            "sowing_batch": "B-1",
        },
        {
            "plot_code": "P-2",
            "geometry": rect(104.03, 30.01),
            "cultivar": "宜香 2115",
            "cultivar_generation": "F5",
            "sowing_batch": "B-2",
        },
    ],
}


class HttpEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with contextlib.redirect_stdout(io.StringIO()):
            ctx = ServiceContext(cls.tmp.name)
        # 反查角色 -> 令牌
        cls.tokens = {u["role"]: key for key, u in ctx.auth.users.items()}
        service.set_context(ctx)
        cls.ctx = ctx
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.api = ApiClient(cls.base_url, cls.tokens)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()

    def setUp(self):
        # 每个用例独立方案，避免相互干扰
        self.plan_code = f"T-{self._testMethodName[-6:]}"
        body = json.loads(json.dumps(PLAN_BODY))
        body["code"] = self.plan_code
        status, self.plan = self.api.call("POST", "/v1/plans", "researcher", body)
        self.assertEqual(status, 201)
        self.base = datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc)

    def event(self, op_id, idx, stage, payload, when_minutes=0):
        at = self.base + timedelta(minutes=5 * idx + when_minutes)
        return self.api.call("POST", f"/v1/operations/{op_id}/events", "operator", {
            "stage": stage,
            "occurred_at": iso_dt(at),
            "client_event_id": f"{op_id}-{idx}-{stage}",
            "payload": payload,
        })

    def complete_chain(self, op_id, gps, seal, sample, bin_code,
                       machine="M-7"):
        flow = [
            ("maturity_check", {"approved": True, "moisture_pct": 23.8}),
            ("machine_clean", {"cleaned": True}),
            ("first_grain_discard", {"discarded_weight_kg": 11.2}),
            ("harvest", {"machine_id": machine, "gps": gps}),
            ("weighing", {"net_weight_kg": 760.0}),
            ("sealing", {"seal_code": seal, "sample_code": sample}),
            ("storage", {"storage_bin": bin_code}),
        ]
        results = []
        for idx, (stage, payload) in enumerate(flow):
            results.append(self.event(op_id, idx, stage, payload))
        return results

    # -- 鉴权与角色 -------------------------------------------------------

    def test_health_open_and_auth_required(self):
        with urlopen(f"{self.base_url}/health", timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["service"], "rice-trial-harvest")
        status, body = self.api.call("GET", "/v1/conflicts")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "missing_token")

    def test_invalid_token_rejected(self):
        req = Request(f"{self.base_url}/v1/conflicts",
                      headers={"Authorization": "Bearer nope"})
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=2)
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()

    def test_role_boundaries(self):
        # 机手不能冻结方案
        status, body = self.api.call("POST", "/v1/plans", "operator",
                                     {"code": "X", "plots": []})
        self.assertEqual(status, 403)
        # 机手不能看冲突队列
        status, _ = self.api.call("GET", "/v1/conflicts", "operator")
        self.assertEqual(status, 403)
        # 实验室不能登记作业
        status, _ = self.api.call("POST", "/v1/operations", "lab", {
            "plan_code": self.plan_code, "plot_code": "P-1", "machine_id": "M"})
        self.assertEqual(status, 403)
        # 负责人不能做实验室分装
        status, _ = self.api.call("POST", "/v1/samples/whatever/split",
                                  "researcher", {"items": []})
        self.assertIn(status, (403, 404))  # 角色先拦为 403；路由解析后为 404
        # 未知路由
        status, _ = self.api.call("GET", "/nope", "admin")
        self.assertEqual(status, 404)

    def test_operator_only_sees_own_operation(self):
        status, op = self.api.call("POST", "/v1/operations", "researcher", {
            "plan_code": self.plan_code, "plot_code": "P-1",
            "machine_id": "M-7", "operator_id": "u_someone_else"})
        self.assertEqual(status, 201)
        status, body = self.api.call(
            "POST", f"/v1/operations/{op['id']}/events", "operator", {
                "stage": "maturity_check", "occurred_at": iso_dt(self.base),
                "client_event_id": "x", "payload": {"approved": True}})
        self.assertEqual(status, 403)
        status, body = self.api.call(
            "GET", f"/v1/operations/{op['id']}/brief", "operator")
        self.assertEqual(status, 403)

    def test_operator_plan_view_is_minimized(self):
        status, body = self.api.call("GET", f"/v1/plans/{self.plan_code}", "operator")
        self.assertEqual(status, 200)
        self.assertNotIn("plots", body)
        self.assertEqual(body["plot_codes"], ["P-1", "P-2"])

    # -- 完整业务闭环 ------------------------------------------------------

    def test_end_to_end_harvest_conflict_lineage_rebuild_trace(self):
        # 1) 机手登记作业（由负责人分配给自己）
        status, op = self.api.call("POST", "/v1/operations", "researcher", {
            "plan_code": self.plan_code, "plot_code": "P-1",
            "machine_id": "M-7", "operator_id": "u_driver"})
        self.assertEqual(status, 201)
        self.assertEqual(op["frozen_snapshot"]["plot"]["cultivar"], "川优 6203")

        # 2) 机手视图：下一步是成熟度复核，不含其他小区信息
        status, work = self.api.call("GET", "/v1/me/work", "operator")
        self.assertEqual(status, 200)
        self.assertEqual(len(work["operations"]), 1)
        brief = work["operations"][0]
        self.assertEqual(brief["next_stage"], "maturity_check")
        self.assertNotIn("cultivar", brief)

        # 3) 正常完成前三步，收获时 GPS 漂移 → 202
        for idx, (stage, payload) in enumerate([
            ("maturity_check", {"approved": True}),
            ("machine_clean", {"cleaned": True}),
            ("first_grain_discard", {"discarded_weight_kg": 11.2}),
        ]):
            status, _ = self.event(op["id"], idx, stage, payload)
            self.assertEqual(status, 201)
        near_lng = 104.02
        mx, _ = _meters_per_degree(30.01)
        drift_gps = {"lng": near_lng + 28 / mx, "lat": 30.01, "accuracy_m": 8}
        status, quarantined = self.event(op["id"], 3, "harvest",
                                         {"machine_id": "M-7", "gps": drift_gps})
        self.assertEqual(status, 202)
        self.assertEqual(quarantined["outcome"], "quarantined")
        codes = {c["code"] for c in quarantined["conflicts"]}
        self.assertEqual(codes, {"gps_drift"})

        # 4) 后续步骤照传不误，全部挂起且不覆盖
        for idx, (stage, payload) in enumerate([
            ("weighing", {"net_weight_kg": 760.0}),
            ("sealing", {"seal_code": "SEAL-1", "sample_code": "S-1"}),
            ("storage", {"storage_bin": "BIN-1"}),
        ], start=4):
            status, body = self.event(op["id"], idx, stage, payload)
            self.assertEqual(status, 202)
            self.assertEqual(body["outcome"], "quarantined")

        # 5) 负责人冲突队列可解释，采信漂移后续链自动接通
        status, queue = self.api.call("GET", "/v1/conflicts", "researcher")
        self.assertEqual(status, 200)
        drift = next(c for c in queue["conflicts"] if c["code"] == "gps_drift")
        self.assertTrue(drift["explanation"])
        status, resolved = self.api.call(
            "POST", f"/v1/conflicts/{drift['id']}/resolve", "researcher",
            {"action": "accept", "note": "电话核对，机器在 P-1 边界内"})
        self.assertEqual(status, 200)
        status, report = self.api.call(
            "GET", f"/v1/plans/{self.plan_code}/rebuild", "researcher")
        self.assertEqual(status, 200)
        self.assertEqual(report["effective_plot_count"], 1)
        self.assertAlmostEqual(report["plots"][0]["net_weight_kg"], 760.0)
        self.assertEqual(report["plots"][0]["samples"][0]["sample_code"], "S-1")

        # 6) 实验室谱系：转交 → 分装 → 耗用 → 作废
        status, _ = self.api.call("POST", "/v1/samples/S-1/transfer", "lab", {
            "lab": "永丰中心实验室", "handed_to": "周检验"})
        self.assertEqual(status, 200)
        status, split = self.api.call("POST", "/v1/samples/S-1/split", "lab", {
            "items": [
                {"sample_code": "S-1-a", "weight_kg": 300, "seal_code": "SEAL-1a"},
                {"sample_code": "S-1-b", "weight_kg": 200},
            ]})
        self.assertEqual(status, 200)
        status, child = self.api.call("GET", "/v1/samples/S-1-a", "lab")
        self.assertEqual(status, 200)
        chain = [n["sample_code"] for n in child["lineage"]]
        self.assertEqual(chain, ["S-1", "S-1-a"])
        self.assertEqual(child["lineage"][0]["sowing_batch"], "B-1")
        status, _ = self.api.call("POST", "/v1/samples/S-1-a/consume", "lab", {
            "amount_kg": 300, "purpose": "发芽率试验"})
        self.assertEqual(status, 200)
        status, _ = self.api.call("POST", "/v1/samples/S-1-b/void", "lab", {
            "reason": "标签污损无法辨认"})
        self.assertEqual(status, 200)
        status, mother = self.api.call("GET", "/v1/samples/S-1", "lab")
        self.assertEqual(status, 200)
        self.assertAlmostEqual(mother["remaining_weight_kg"], 260.0, places=2)

    def test_trace_finds_prior_plots_from_conflict(self):
        # P-2 先收完
        status, op2 = self.api.call("POST", "/v1/operations", "researcher", {
            "plan_code": self.plan_code, "plot_code": "P-2",
            "machine_id": "M-7", "operator_id": "u_driver"})
        self.complete_chain(op2["id"], {"lng": 104.03, "lat": 30.01},
                            "SEAL-2", "S-2", "BIN-2")
        # P-1 收获时定位飞到远处
        status, op1 = self.api.call("POST", "/v1/operations", "researcher", {
            "plan_code": self.plan_code, "plot_code": "P-1",
            "machine_id": "M-7", "operator_id": "u_driver"})
        for idx, (stage, payload) in enumerate([
            ("maturity_check", {"approved": True}),
            ("machine_clean", {"cleaned": True}),
            ("first_grain_discard", {"discarded_weight_kg": 9}),
        ]):
            self.event(op1["id"], idx, stage, payload)
        # 注意 P-1 收获时间在 P-2 之后
        status, bad = self.api.call(
            "POST", f"/v1/operations/{op1['id']}/events", "operator", {
                "stage": "harvest",
                "occurred_at": iso_dt(self.base + timedelta(minutes=60)),
                "client_event_id": "far-fly",
                "payload": {"machine_id": "M-7",
                            "gps": {"lng": 104.9, "lat": 30.9, "accuracy_m": 5}}})
        self.assertEqual(status, 202)
        outside = next(c for c in bad["conflicts"] if c["code"] == "gps_outside_plot")
        status, trace = self.api.call(
            "GET", f"/v1/conflicts/{outside['id']}/trace", "researcher")
        self.assertEqual(status, 200)
        prior_plots = [s["plot_code"] for s in trace["prior_stops"]]
        self.assertIn("P-2", prior_plots)
        self.assertNotIn("P-1", prior_plots)

    def test_seal_rescan_and_idempotent_replay(self):
        status, op = self.api.call("POST", "/v1/operations", "researcher", {
            "plan_code": self.plan_code, "plot_code": "P-1",
            "machine_id": "M-7", "operator_id": "u_driver"})
        self.complete_chain(op["id"], {"lng": 104.02, "lat": 30.01},
                            "SEAL-DUP", "S-DUP", "BIN-DUP")
        # 同样封签用于 P-2
        status, op2 = self.api.call("POST", "/v1/operations", "researcher", {
            "plan_code": self.plan_code, "plot_code": "P-2",
            "machine_id": "M-7", "operator_id": "u_driver"})
        flow = [
            ("maturity_check", {"approved": True}),
            ("machine_clean", {"cleaned": True}),
            ("first_grain_discard", {"discarded_weight_kg": 9}),
            ("harvest", {"machine_id": "M-7", "gps": {"lng": 104.03, "lat": 30.01}}),
            ("weighing", {"net_weight_kg": 500}),
            ("sealing", {"seal_code": "SEAL-DUP", "sample_code": "S-2DUP"}),
        ]
        for idx, (stage, payload) in enumerate(flow):
            status, body = self.event(op2["id"], idx + 10, stage, payload)
        self.assertEqual(status, 202)
        self.assertIn("seal_already_scanned",
                      {c["code"] for c in body["conflicts"]})
        # 幂等重放：同一事件号同一载荷 → 200 replay，不新增记录
        replay_body = {
            "stage": "maturity_check",
            "occurred_at": iso_dt(self.base + timedelta(minutes=50)),
            "client_event_id": f"{op2['id']}-10-maturity_check",
            "payload": {"approved": True},
        }
        s1, b1 = self.api.call("POST", f"/v1/operations/{op2['id']}/events",
                               "operator", replay_body)
        self.assertEqual(s1, 200)
        self.assertEqual(b1["outcome"], "replay")

    def test_validation_errors_have_envelope(self):
        status, body = self.api.call("POST", "/v1/operations", "researcher", {
            "plan_code": self.plan_code, "plot_code": "P-1"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "missing_fields")

        status, body = self.api.call("POST", "/v1/plans", "researcher", {
            "code": "BAD-GEO", "plots": [{
                "plot_code": "Z", "geometry": [[1, 2], [3, 4]],
                "cultivar": "x", "cultivar_generation": "F1", "sowing_batch": "b"}]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_geometry")

    def test_records_persisted_to_jsonl(self):
        status, op = self.api.call("POST", "/v1/operations", "researcher", {
            "plan_code": self.plan_code, "plot_code": "P-1",
            "machine_id": "M-7", "operator_id": "u_driver"})
        self.event(op["id"], 0, "maturity_check", {"approved": True})
        # 直接读追加流文件验证只追加落盘
        raw = AppendStore(self.tmp.name).all("events")
        self.assertTrue(any(e["op_id"] == op["id"] for e in raw))
        self.assertTrue(all("_seq" in e for e in raw))


if __name__ == "__main__":
    unittest.main()
