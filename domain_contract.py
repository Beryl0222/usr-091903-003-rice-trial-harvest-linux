"""端到端契约：冻结 -> 七步作业 -> 冲突隔离与审核 -> 封样入库
-> 实验室谱系 -> 按方案重建产量 -> 异常反查机器轨迹 -> 角色最小可见。
"""

import json
import threading
import unittest
from datetime import timedelta
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harvest_domain import HarvestService, parse_dt
from service import Handler

ADMIN = "admin-token"
LEAD = "lead-token"
LAB = "lab-token"
OP1 = "op01-token"
OP2 = "op02-token"

# 四块东西相邻的矩形试验小区（[经度, 纬度]），各 10 度见方，共享边界。
PLOT_A_BOUNDARY = [[0, 0], [0, 10], [10, 10], [10, 0]]
PLOT_B_BOUNDARY = [[10, 0], [10, 10], [20, 10], [20, 0]]
PLOT_C_BOUNDARY = [[20, 0], [20, 10], [30, 10], [30, 0]]
PLOT_D_BOUNDARY = [[30, 0], [30, 10], [40, 10], [40, 0]]
TRIAL = "T-2026-R01"


def freeze_body(code, boundary, variety=None):
    return {
        "code": code,
        "trial_code": TRIAL,
        "variety_code": variety or f"V-{code}",
        "variety_name": f"蜀丰{code}",
        "generation": "F6",
        "sowing_batch": "BATCH-0918",
        "boundary": boundary,
        "sampling_plan": {"method": "five_point", "bags": 3, "note": "每点 0.5kg"},
    }


class DomainContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock = [parse_dt("2026-09-20T05:50:00+00:00")]

        class _Handler(Handler):
            pass

        cls.service = HarvestService(clock=lambda: cls.clock[0])
        _Handler.service = cls.service
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, token=None, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = Request(f"{self.base}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as error:
            return error.code, json.load(error)

    def submit(self, plot, stage, at, token=OP1, lag_seconds=20, **fields):
        """模拟设备在发生后 lag_seconds 上传（默认网络良好，不触发离线补传）。"""
        self.clock[0] = parse_dt(at) + timedelta(seconds=lag_seconds)
        payload = {"stage": stage, "occurred_at": at, **fields}
        return self.request("POST", f"/plots/{plot}/events", token, payload)

    # ------------------------------------------------------------------ #

    def test_01_freeze_is_immutable_and_protected(self):
        for code, boundary in (
            ("P-A", PLOT_A_BOUNDARY),
            ("P-B", PLOT_B_BOUNDARY),
            ("P-C", PLOT_C_BOUNDARY),
            ("P-D", PLOT_D_BOUNDARY),
        ):
            status, body = self.request("POST", "/admin/plots", ADMIN, freeze_body(code, boundary))
            self.assertEqual(status, 201, body)
            self.assertEqual(body["frozen_by"], "A01")

        # 冻结后禁止覆盖，即便是管理员。
        status, body = self.request("POST", "/admin/plots", ADMIN, freeze_body("P-A", PLOT_A_BOUNDARY))
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "PLOT_FROZEN")

        # 机手与实验室无权冻结。
        status, _ = self.request("POST", "/admin/plots", OP1, freeze_body("P-X", PLOT_A_BOUNDARY))
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/admin/plots", LAB, freeze_body("P-Y", PLOT_A_BOUNDARY))
        self.assertEqual(status, 403)

        # 非法边界直接拒绝。
        status, body = self.request("POST", "/admin/plots", ADMIN, freeze_body("P-Z", [[0, 0], [1, 1]]))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "BAD_BOUNDARY")

        # 无令牌访问受保护接口 401；未知路由仍是 404。
        status, _ = self.request("GET", f"/admin/trials/{TRIAL}")
        self.assertEqual(status, 401)
        status, _ = self.request("GET", "/nothing/here", token=OP1)
        self.assertEqual(status, 404)

    def test_02_operator_sees_only_the_work_card(self):
        status, queue = self.request("GET", "/operator/queue", OP1)
        self.assertEqual(status, 200)
        cards = {row["plot_code"]: row for row in queue["queue"]}
        self.assertEqual(cards["P-A"]["next_stage"], "maturity_review")
        # 机手队列里不出现品种代次等研究信息。
        self.assertNotIn("variety_code", json.dumps(queue, ensure_ascii=False))

        status, card = self.request("GET", "/plots/P-A/work-card", OP1)
        self.assertEqual(status, 200)
        self.assertEqual(card["next_stage"], "maturity_review")
        self.assertNotIn("variety", json.dumps(card))

        # 机手不能看方案全文、不能看地块事件流水。
        self.assertEqual(self.request("GET", f"/admin/trials/{TRIAL}", OP1)[0], 403)
        self.assertEqual(self.request("GET", "/plots/P-A/events", OP1)[0], 403)

    def test_03_clean_seven_stage_flow_and_idempotent_replay(self):
        at = "2026-09-20T06:00:00+00:00"
        status, body = self.submit("P-A", "maturity_review", at,
                                   maturity_grade="95%", client_event_id="dev-001")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["accepted"])
        first_id = body["event"]["event_id"]

        # 同一设备事件重传：幂等，绝不产生第二条记录。
        status, body = self.submit("P-A", "maturity_review", at,
                                   maturity_grade="95%", client_event_id="dev-001")
        self.assertTrue(body["accepted"])
        self.assertEqual(body["event"]["event_id"], first_id)

        self.submit("P-A", "machine_clean", "2026-09-20T06:05:00+00:00",
                    machine_code="M-07", gps={"lat": 5, "lon": 5})
        self.submit("P-A", "head_discard", "2026-09-20T06:10:00+00:00",
                    machine_code="M-07", gps={"lat": 5, "lon": 5})
        self.submit("P-A", "harvest", "2026-09-20T06:20:00+00:00",
                    machine_code="M-07", gps={"lat": 5, "lon": 5})
        self.submit("P-A", "weighing", "2026-09-20T06:25:00+00:00",
                    weight_kg=100.0, moisture_pct=20.0)
        status, body = self.submit("P-A", "sealing", "2026-09-20T06:28:00+00:00",
                                   seal_code="SEAL-1")
        self.assertEqual(status, 200)
        self.submit("P-A", "storage", "2026-09-20T06:30:00+00:00", bin_code="BIN-7")

        # 作业卡显示全部完成；研究视角的事件流水机手仍不可见。
        status, card = self.request("GET", "/plots/P-A/work-card", OP1)
        self.assertIsNone(card["next_stage"])
        self.assertEqual(len(card["completed_stages"]), 7)

    def test_04_gps_drift_is_quarantined_and_traceable(self):
        self.submit("P-B", "maturity_review", "2026-09-20T07:00:00+00:00",
                    maturity_grade="92%")
        self.submit("P-B", "machine_clean", "2026-09-20T07:05:00+00:00",
                    machine_code="M-07", gps={"lat": 5, "lon": 15})
        self.submit("P-B", "head_discard", "2026-09-20T07:10:00+00:00",
                    machine_code="M-07", gps={"lat": 5, "lon": 15})

        # 上报 P-B 的收获，但坐标实际落在相邻的 P-A —— 典型品种混杂风险。
        status, body = self.submit("P-B", "harvest", "2026-09-20T07:20:00+00:00",
                                   machine_code="M-07", gps={"lat": 5, "lon": 5})
        self.assertEqual(status, 202)
        self.assertFalse(body["accepted"])
        self.assertEqual(body["event"]["status"], "quarantined")
        conflict = body["conflicts"][0]
        self.assertEqual(conflict["reasons"], ["GPS_DRIFT"])
        self.assertEqual(conflict["details"]["positioned_plot"], "P-A")
        self.assertIn("定位漂移", conflict["reason_labels"][0])
        cfl_id = conflict["conflict_id"]

        # 异常反查：这台机器此前当天已在 P-A 完成收获（混杂来源），
        # 当前地块 P-B 的清洁/弃粮也在轨迹中，混杂路径一目了然。
        status, trace = self.request(
            "GET", f"/conflicts/{cfl_id}/machine-prior-plots", LEAD)
        self.assertEqual(status, 200)
        self.assertEqual(trace["machine_code"], "M-07")
        self.assertEqual(trace["prior_plots"], ["P-A", "P-B"])
        prior_harvests = [row for row in trace["trace"] if row["stage"] == "harvest"]
        self.assertEqual([row["plot_code"] for row in prior_harvests], ["P-A"])

        # 机手不能自行审核冲突。
        self.assertEqual(self.request("POST", f"/conflicts/{cfl_id}/resolve",
                                      OP1, {"decision": "accept"})[0], 403)

        # 坐标确实录错：负责人驳回原记录，原始事件保留为 rejected，不被覆盖。
        status, body = self.request("POST", f"/conflicts/{cfl_id}/resolve",
                                    LEAD, {"decision": "reject", "note": "坐标录错，作废重来"})
        self.assertEqual(status, 200)
        self.assertEqual(body["resolution"]["decision"], "reject")
        status, events = self.request("GET", "/plots/P-B/events", LEAD)
        harvests = [e for e in events["events"] if e["stage"] == "harvest"]
        self.assertEqual([e["status"] for e in harvests], ["rejected"])

        # 用正确坐标重新上报，正常生效。
        status, body = self.submit("P-B", "harvest", "2026-09-20T07:25:00+00:00",
                                   machine_code="M-07", gps={"lat": 5, "lon": 15})
        self.assertTrue(body["accepted"])

    def test_05_seal_duplicate_blocks_acceptance(self):
        self.submit("P-B", "weighing", "2026-09-20T07:30:00+00:00",
                    weight_kg=80.0, moisture_pct=18.0)
        # 复用 P-A 的封签 SEAL-1。
        status, body = self.submit("P-B", "sealing", "2026-09-20T07:35:00+00:00",
                                   seal_code="SEAL-1")
        self.assertEqual(status, 202)
        cfl_id = body["conflicts"][0]["conflict_id"]
        self.assertEqual(body["conflicts"][0]["reasons"], ["SEAL_DUPLICATE"])
        self.assertEqual(body["conflicts"][0]["details"]["existing_uses"][0]["plot_code"], "P-A")

        # 原封签样本已生效，放行必须被系统拒绝，不能人工强开。
        status, body = self.request("POST", f"/conflicts/{cfl_id}/resolve",
                                    LEAD, {"decision": "accept"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "SEAL_STILL_CLASHING")

        # 驳回后换封签重新封样。
        self.request("POST", f"/conflicts/{cfl_id}/resolve", LEAD,
                     {"decision": "reject", "note": "封签重复，需换签"})
        status, body = self.submit("P-B", "sealing", "2026-09-20T07:40:00+00:00",
                                   seal_code="SEAL-2")
        self.assertTrue(body["accepted"])
        self.submit("P-B", "storage", "2026-09-20T07:45:00+00:00", bin_code="BIN-8")

    def test_06_offline_late_backfill_can_be_accepted(self):
        # 设备离线 1 小时后补传。
        status, body = self.submit("P-C", "maturity_review",
                                   "2026-09-20T07:00:00+00:00",
                                   lag_seconds=3600, maturity_grade="90%",
                                   device_id="dev-07")
        self.assertEqual(status, 202)
        self.assertEqual(body["conflicts"][0]["reasons"], ["OFFLINE_LATE"])
        self.assertGreaterEqual(body["conflicts"][0]["details"]["delay_seconds"], 3600)
        cfl_id = body["conflicts"][0]["conflict_id"]

        # 核对轨迹确认机手当时确实在 P-C，负责人放行补传记录。
        status, _ = self.request("POST", f"/conflicts/{cfl_id}/resolve",
                                 LEAD, {"decision": "accept", "note": "离线补传，轨迹核对无误"})
        self.assertEqual(status, 200)
        status, events = self.request("GET", "/plots/P-C/events", LEAD)
        self.assertEqual(events["events"][0]["status"], "clean")

    def test_07_stage_duplicate_and_order_gap_are_conflicts(self):
        self.submit("P-C", "machine_clean", "2026-09-20T08:10:00+00:00",
                    machine_code="M-07", gps={"lat": 5, "lon": 25})
        # 重复清洁。
        status, body = self.submit("P-C", "machine_clean", "2026-09-20T08:12:00+00:00",
                                   machine_code="M-07", gps={"lat": 5, "lon": 25})
        self.assertEqual(status, 202)
        self.assertEqual(body["conflicts"][0]["reasons"], ["STAGE_DUPLICATE"])
        dup_id = body["conflicts"][0]["conflict_id"]
        self.request("POST", f"/conflicts/{dup_id}/resolve", LEAD, {"decision": "reject"})

        self.submit("P-C", "head_discard", "2026-09-20T08:20:00+00:00",
                    machine_code="M-07", gps={"lat": 5, "lon": 25})
        self.submit("P-C", "harvest", "2026-09-20T08:30:00+00:00",
                    machine_code="M-07", gps={"lat": 5, "lon": 25})
        self.submit("P-C", "weighing", "2026-09-20T08:35:00+00:00",
                    weight_kg=60.0, moisture_pct=22.0)
        self.submit("P-C", "sealing", "2026-09-20T08:40:00+00:00", seal_code="SEAL-3")
        self.submit("P-C", "storage", "2026-09-20T08:45:00+00:00", bin_code="BIN-9")

        # P-D 未做前置步骤直接称重：顺序缺口。
        status, body = self.submit("P-D", "weighing", "2026-09-20T09:00:00+00:00",
                                   weight_kg=10.0, moisture_pct=20.0)
        self.assertEqual(status, 202)
        self.assertEqual(body["conflicts"][0]["reasons"], ["ORDER_GAP"])
        self.assertEqual(body["conflicts"][0]["details"]["missing_stages"],
                         ["maturity_review", "machine_clean", "head_discard", "harvest"])
        gap_id = body["conflicts"][0]["conflict_id"]
        self.request("POST", f"/conflicts/{gap_id}/resolve", LEAD, {"decision": "reject"})
        # P-D 维持未封样状态，留待产量重建时检验。

    def test_08_trial_results_rebuild_effective_yields(self):
        status, results = self.request("GET", f"/trials/{TRIAL}/results", LEAD)
        self.assertEqual(status, 200)
        by_plot = {row["plot_code"]: row for row in results["plots"]}
        self.assertEqual(results["effective_plot_count"], 3)

        pa = by_plot["P-A"]
        self.assertTrue(pa["effective"])
        self.assertEqual(pa["weight_kg"], 100.0)
        # 折算到 13.5% 标准含水率：100 * (100-20)/(100-13.5)
        self.assertAlmostEqual(pa["standard_weight_kg"], 92.486, places=2)
        self.assertEqual(pa["seal_code"], "SEAL-1")
        self.assertEqual(pa["bin_code"], "BIN-7")
        self.assertEqual(pa["machine_code"], "M-07")

        # P-D 缺封样：不计入有效产量，并给出可解释原因。
        pd = by_plot["P-D"]
        self.assertFalse(pd["effective"])
        self.assertIn("缺少封样生效记录", pd["invalid_reasons"])
        self.assertIsNone(pd["seal_code"])

        # 三个封样入库的根样本出现在清单里，P-D 无样本。
        sample_plots = {s["plot_code"] for s in results["samples"]}
        self.assertEqual(sample_plots, {"P-A", "P-B", "P-C"})

        # 机手无权重建研究产量。
        self.assertEqual(self.request("GET", f"/trials/{TRIAL}/results", OP1)[0], 403)

    def test_09_lab_lineage_split_consume_void(self):
        code = "S-P-A-SEAL-1"
        # 未转交时实验室不能操作。
        status, body = self.request("POST", f"/samples/{code}/split", LAB, {
            "at": "2026-09-20T10:00:00+00:00",
            "parts": [{"quantity_kg": 20, "purpose": "品质检测"}],
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "NOT_TRANSFERRED")

        # 负责人转交实验室，谱系追加转交记录。
        status, sample = self.request("POST", f"/samples/{code}/transfer", LEAD, {
            "at": "2026-09-20T10:05:00+00:00",
            "carrier": "冷链车-川A123", "lab_code": "LAB-01",
        })
        self.assertEqual(status, 200)
        self.assertEqual(sample["status"], "transferred")
        self.assertEqual(sample["holder"], "LAB-01")

        # 重分装：子样本回挂原始谱系，母样本扣减余量。
        status, split = self.request("POST", f"/samples/{code}/split", LAB, {
            "at": "2026-09-20T10:30:00+00:00",
            "parts": [
                {"quantity_kg": 20, "purpose": "品质检测"},
                {"quantity_kg": 30, "purpose": "备份留存", "location": "冷库A"},
            ],
        })
        self.assertEqual(status, 200)
        child_codes = [c["sample_code"] for c in split["children"]]
        self.assertEqual(child_codes, [f"{code}.2", f"{code}.3"])
        self.assertEqual(split["parent"]["remaining_kg"], 50.0)
        for child in split["children"]:
            self.assertEqual(child["root_sample_code"], code)
            self.assertEqual(child["variety_code"], "V-P-A")
            self.assertEqual(child["generation"], "F6")
            self.assertEqual(child["lineage"][0]["action"], "split_from")

        # 超量分装/耗用被拒绝。
        status, body = self.request("POST", f"/samples/{code}/split", LAB, {
            "at": "2026-09-20T10:35:00+00:00",
            "parts": [{"quantity_kg": 999}],
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "INSUFFICIENT_QUANTITY")

        # 耗用检测样。
        status, child = self.request("POST", f"/samples/{child_codes[0]}/consume", LAB, {
            "at": "2026-09-20T11:00:00+00:00",
            "quantity_kg": 20, "purpose": "出米率检测",
        })
        self.assertEqual(status, 200)
        self.assertEqual(child["remaining_kg"], 0)

        # 再次分装，编号继续递增不冲突。
        status, split2 = self.request("POST", f"/samples/{code}/split", LAB, {
            "at": "2026-09-20T11:10:00+00:00",
            "parts": [{"quantity_kg": 10, "purpose": "复检"}],
        })
        self.assertEqual([c["sample_code"] for c in split2["children"]], [f"{code}.4"])
        self.assertEqual(split2["parent"]["remaining_kg"], 40.0)

        # 作废备份样：状态翻转、原因留痕、历史谱系保留。
        status, voided = self.request("POST", f"/samples/{child_codes[1]}/void", LAB, {
            "at": "2026-09-20T11:30:00+00:00",
            "reason": "包装破损受潮",
        })
        self.assertEqual(status, 200)
        self.assertEqual(voided["status"], "voided")
        actions = [row["action"] for row in voided["lineage"]]
        self.assertEqual(actions, ["split_from", "voided"])
        # 已作废不能再转交。
        status, body = self.request("POST", f"/samples/{child_codes[1]}/transfer", LEAD, {
            "at": "2026-09-20T11:35:00+00:00"})
        self.assertEqual(status, 409)

        # 谱系整族可重建，作废样本仍在谱系中可见。
        status, lineage = self.request("GET", f"/samples/{code}/lineage", LEAD)
        self.assertEqual(status, 200)
        codes = [s["sample_code"] for s in lineage["samples"]]
        self.assertEqual(codes, [code, f"{code}.2", f"{code}.3", f"{code}.4"])
        self.assertTrue(any(s["status"] == "voided" for s in lineage["samples"]))

        # 机手不可见样本谱系；负责人产量清单默认隐藏已作废子样本。
        self.assertEqual(self.request("GET", f"/samples/{code}/lineage", OP1)[0], 403)
        results = self.request("GET", f"/trials/{TRIAL}/results", LEAD)[1]
        pa_samples = [s for s in results["samples"] if s["plot_code"] == "P-A"]
        self.assertFalse(any(s["status"] == "voided" for s in pa_samples))

    def test_10_client_event_isolation_and_machine_trace(self):
        # 机手只能查到自己上报的客户端事件。
        status, _ = self.request("GET", "/client-events/dev-001", OP1)
        self.assertEqual(status, 200)
        status, body = self.request("GET", "/client-events/dev-001", OP2)
        self.assertEqual(status, 404)

        # 当日机器轨迹：M-07 顺序经过 P-A、P-B、P-C（含被驳回的漂移尝试）。
        status, trace = self.request(
            "GET", "/machines/M-07/trace?date=2026-09-20", LEAD)
        self.assertEqual(status, 200)
        plots_in_order = []
        for row in trace["trace"]:
            if row["plot_code"] not in plots_in_order:
                plots_in_order.append(row["plot_code"])
        self.assertEqual(plots_in_order, ["P-A", "P-B", "P-C"])

        # 冲突单可按状态过滤，且已全部结案（本场景内）。
        status, pending = self.request("GET", "/conflicts?status=pending", LEAD)
        self.assertEqual(status, 200)
        self.assertEqual(pending["conflicts"], [])
        status, resolved = self.request("GET", "/conflicts?status=resolved", LEAD)
        reasons = {r for c in resolved["conflicts"] for r in c["reasons"]}
        self.assertEqual(
            reasons,
            {"GPS_DRIFT", "SEAL_DUPLICATE", "OFFLINE_LATE", "STAGE_DUPLICATE", "ORDER_GAP"},
        )


if __name__ == "__main__":
    unittest.main()
