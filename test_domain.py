"""领域核心的端到端测试：冻结、阶段机、冲突、谱系、重建、反查、重启恢复。"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from domain import (
    HarvestService,
    SEVERITY_HIGH,
    _meters_per_degree,
)
from store import AppendStore

ADMIN = {"user_id": "u_admin", "name": "管理员", "role": "admin"}
LEAD = {"user_id": "u_lead", "name": "负责人", "role": "researcher"}
LAB = {"user_id": "u_lab", "name": "实验室", "role": "lab"}
DRIVER = {"user_id": "u_driver", "name": "机手", "role": "operator"}

BASE_TIME = datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start=BASE_TIME):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, minutes=1):
        self.t += timedelta(minutes=minutes)
        return self.t


def iso(dt):
    from domain import iso as _iso
    return _iso(dt)


def rect(lng, lat, w_m=40.0, h_m=40.0):
    """以 (lng, lat) 为中心的矩形地块（经纬度）。"""
    mx, my = _meters_per_degree(lat)
    dlng, dlat = (w_m / 2) / mx, (h_m / 2) / my
    return [
        [lng - dlng, lat - dlat],
        [lng + dlng, lat - dlat],
        [lng + dlng, lat + dlat],
        [lng - dlng, lat + dlat],
    ]


def offset_point(lng, lat, north_m=0.0, east_m=0.0):
    mx, my = _meters_per_degree(lat)
    return {"lng": lng + east_m / mx, "lat": lat + north_m / my}


def make_plan_body(code="T2026-YF", plots=None, scheme=None):
    plots = plots or [
        {
            "plot_code": "P-A",
            "geometry": rect(104.00, 30.00),
            "cultivar": "川优 6203",
            "cultivar_generation": "F6",
            "sowing_batch": "B2026-04-01",
        }
    ]
    return {
        "code": code,
        "season": "2026 秋收",
        "sampling_scheme": scheme or {"sample_count": 1, "purpose": "品种纯度与产量"},
        "plots": plots,
    }


def two_plot_plan():
    return make_plan_body(plots=[
        {
            "plot_code": "P-A",
            "geometry": rect(104.00, 30.00),
            "cultivar": "川优 6203",
            "cultivar_generation": "F6",
            "sowing_batch": "B2026-04-01",
        },
        {
            "plot_code": "P-B",
            "geometry": rect(104.01, 30.00),
            "cultivar": "宜香 2115",
            "cultivar_generation": "F5",
            "sowing_batch": "B2026-04-02",
        },
    ])


def stage_payloads(machine_id, seal_code="SEAL-A1", sample_code="SMP-A1",
                   storage_bin="A-01", gps=None):
    return [
        ("maturity_check", {"approved": True, "moisture_pct": 24.1}),
        ("machine_clean", {"cleaned": True, "method": "高压气吹+人工复检"}),
        ("first_grain_discard", {"discarded_weight_kg": 12.5}),
        ("harvest", {"machine_id": machine_id, "gps": gps}),
        ("weighing", {"gross_weight_kg": 842.0, "net_weight_kg": 820.5}),
        ("sealing", {"seal_code": seal_code, "sample_code": sample_code}),
        ("storage", {"storage_bin": storage_bin}),
    ]


def run_stages(svc, op, actor, clock, gps=None, **kwargs):
    outcomes = []
    for i, (stage, payload) in enumerate(
        stage_payloads(op["machine_id"], gps=gps, **kwargs)
    ):
        clock.advance(5)
        outcome, _ = svc.submit_event(op["id"], {
            "stage": stage,
            "occurred_at": iso(clock.t),
            "client_event_id": f"{op['id']}-{i}",
            "payload": payload,
        }, actor)
        outcomes.append(outcome)
    return outcomes


def fresh_service(tmpdir):
    clock = FakeClock()
    store = AppendStore(tmpdir)
    return HarvestService(store, clock=clock), clock, store


class HarvestDomainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc, self.clock, self.store = fresh_service(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def start_operation(self, body=None, actor=LEAD):
        self.svc.create_plan(two_plot_plan(), actor)
        body = body or {"plan_code": "T2026-YF", "plot_code": "P-A",
                        "machine_id": "M-1", "operator_id": "u_driver"}
        return self.svc.create_operation(body, actor)

    # -- 正常流程 ---------------------------------------------------------

    def test_full_happy_path_and_rebuild(self):
        op = self.start_operation()
        center = {"lng": 104.00, "lat": 30.00, "accuracy_m": 5.0}
        outcomes = run_stages(self.svc, op, DRIVER, self.clock, gps=center)
        self.assertEqual(outcomes, ["accepted"] * 7)

        report = self.svc.rebuild_plan("T2026-YF")
        self.assertEqual(report["effective_plot_count"], 1)
        plot_row = report["plots"][0]
        self.assertAlmostEqual(plot_row["net_weight_kg"], 820.5)
        self.assertTrue(plot_row["stored"])
        self.assertEqual(plot_row["samples"][0]["sample_code"], "SMP-A1")
        self.assertEqual(plot_row["cultivar"], "川优 6203")
        self.assertEqual(plot_row["cultivar_generation"], "F6")
        # 40m × 40m = 1600 m² ≈ 2.4 亩
        self.assertAlmostEqual(plot_row["area_mu"], 2.4, places=1)
        self.assertGreater(plot_row["yield_kg_per_mu"], 300)
        # P-B 无作业，进入排除清单而非静默丢弃
        self.assertEqual(report["excluded"][0]["plot_code"], "P-B")
        self.assertEqual(report["excluded"][0]["reason"], "no_operation")

    def test_plan_is_frozen_and_snapshot_used(self):
        self.svc.create_plan(make_plan_body(), LEAD)
        from domain import DomainError
        with self.assertRaises(DomainError) as ctx:
            self.svc.create_plan(make_plan_body(), LEAD)
        self.assertEqual(ctx.exception.code, "plan_exists")

        op = self.svc.create_operation(
            {"plan_code": "T2026-YF", "plot_code": "P-A",
             "machine_id": "M-1", "operator_id": "u_driver"}, LEAD)
        # 作业内的地块数据是开工时刻快照
        self.assertEqual(op["frozen_snapshot"]["plot"]["cultivar"], "川优 6203")
        self.assertEqual(op["frozen_snapshot"]["plot"]["sowing_batch"], "B2026-04-01")
        self.assertEqual(op["frozen_snapshot"]["sampling_scheme"]["sample_count"], 1)

    def test_geometry_tolerance_band(self):
        from domain import distance_to_polygon_m
        polygon = rect(104.00, 30.00, w_m=40, h_m=40)
        inside = offset_point(104.00, 30.00)
        near = offset_point(104.00, 30.00, north_m=30)   # 边外 10 m
        far = offset_point(104.00, 30.00, north_m=120)   # 边外 100 m
        self.assertEqual(distance_to_polygon_m(inside["lng"], inside["lat"], polygon), 0.0)
        self.assertLess(distance_to_polygon_m(near["lng"], near["lat"], polygon), 15)
        self.assertGreater(distance_to_polygon_m(far["lng"], far["lat"], polygon), 15)

    # -- 阶段机 -----------------------------------------------------------

    def test_stage_skip_is_quarantined(self):
        op = self.start_operation()
        self.clock.advance(5)
        outcome, record = self.svc.submit_event(op["id"], {
            "stage": "harvest",
            "occurred_at": iso(self.clock.t),
            "client_event_id": "skip-1",
            "payload": {"machine_id": "M-1"},
        }, DRIVER)
        self.assertEqual(outcome, "quarantined")
        codes = {c["code"] for c in record["conflicts"]}
        self.assertIn("stage_skip", codes)
        self.assertIn("plot_not_released", codes)
        # 事件不生效：尚未完成任何阶段，下一步仍是成熟度复核
        brief = self.svc.operator_view(op["id"])
        self.assertEqual(brief["next_stage"], "maturity_check")

    def test_maturity_not_approved_blocks_harvest(self):
        op = self.start_operation()
        for stage, payload in [
            ("maturity_check", {"approved": False}),
            ("machine_clean", {"cleaned": True}),
            ("first_grain_discard", {"discarded_weight_kg": 10}),
        ]:
            self.clock.advance(5)
            outcome, _ = self.svc.submit_event(op["id"], {
                "stage": stage, "occurred_at": iso(self.clock.t),
                "client_event_id": f"c-{stage}", "payload": payload,
            }, DRIVER)
            self.assertEqual(outcome, "accepted")
        self.clock.advance(5)
        outcome, record = self.svc.submit_event(op["id"], {
            "stage": "harvest", "occurred_at": iso(self.clock.t),
            "client_event_id": "h-blocked",
            "payload": {"machine_id": "M-1"},
        }, DRIVER)
        self.assertEqual(outcome, "quarantined")
        self.assertIn("plot_not_released", {c["code"] for c in record["conflicts"]})

    def test_stage_repeated_cannot_overwrite(self):
        op = self.start_operation()
        self.clock.advance(5)
        self.svc.submit_event(op["id"], {
            "stage": "maturity_check", "occurred_at": iso(self.clock.t),
            "client_event_id": "m1", "payload": {"approved": True},
        }, DRIVER)
        self.clock.advance(5)
        outcome, record = self.svc.submit_event(op["id"], {
            "stage": "maturity_check", "occurred_at": iso(self.clock.t),
            "client_event_id": "m2", "payload": {"approved": False},
        }, DRIVER)
        self.assertEqual(outcome, "quarantined")
        self.assertEqual(record["conflicts"][0]["code"], "stage_repeated")
        # 既有记录未被覆盖
        accepted = self.svc._accepted_by_stage(op["id"])
        self.assertTrue(accepted["maturity_check"]["payload"]["approved"])

    def test_machine_mismatch_is_high_severity(self):
        op = self.start_operation()
        run_stages(self.svc, op, DRIVER, self.clock,
                   gps={"lng": 104.00, "lat": 30.00})
        # 第二个小区用别的机器号上报收获
        op_b = self.svc.create_operation(
            {"plan_code": "T2026-YF", "plot_code": "P-B",
             "machine_id": "M-1", "operator_id": "u_driver"}, LEAD)
        for i, (stage, payload) in enumerate([
            ("maturity_check", {"approved": True}),
            ("machine_clean", {"cleaned": True}),
            ("first_grain_discard", {"discarded_weight_kg": 9}),
            ("harvest", {"machine_id": "M-9"}),
        ]):
            self.clock.advance(5)
            outcome, record = self.svc.submit_event(op_b["id"], {
                "stage": stage, "occurred_at": iso(self.clock.t),
                "client_event_id": f"b-{i}", "payload": payload,
            }, DRIVER)
        self.assertEqual(outcome, "quarantined")
        conflict = next(c for c in record["conflicts"] if c["code"] == "machine_mismatch")
        self.assertEqual(conflict["severity"], SEVERITY_HIGH)

    # -- 三类重点冲突 ------------------------------------------------------

    def test_gps_drift_accept_then_event_takes_effect(self):
        op = self.start_operation()
        near = offset_point(104.00, 30.00, north_m=28)  # 边外约 8 m
        near["accuracy_m"] = 12
        outcomes = run_stages(self.svc, op, DRIVER, self.clock, gps=near)
        # 收获挂起后，称重/封样/入库各以 stage_skip 挂起等待，不生效也不丢失
        self.assertEqual(
            outcomes,
            ["accepted", "accepted", "accepted",
             "quarantined", "quarantined", "quarantined", "quarantined"],
        )
        open_conflicts = self.svc.list_conflicts(op["id"], "open")
        codes = {c["code"] for c in open_conflicts}
        self.assertEqual(codes, {"gps_drift", "stage_skip"})
        cid = next(c["id"] for c in open_conflicts if c["code"] == "gps_drift")
        # 冲突单必须可解释
        self.assertTrue(open_conflicts[0]["explanation"])
        self.svc.resolve_conflict(cid, {
            "action": "accept", "note": "田间复核为边界漂移，实际在 P-A 内作业"}, LEAD)
        stages = {e["stage"]: e["status"]
                  for e in self.svc.events_by_op[op["id"]]}
        self.assertTrue(all(v == "accepted" for v in stages.values()))
        # 自动续链留痕
        auto = [c for c in self.svc.list_conflicts(op["id"], "all")
                if c["resolution"] and c["resolution"]["action"] == "auto_accept"]
        self.assertEqual(len(auto), 3)
        report = self.svc.rebuild_plan("T2026-YF")
        self.assertEqual(report["effective_plot_count"], 1)

    def test_gps_far_outside_reject_excludes_yield(self):
        op = self.start_operation()
        far = offset_point(104.00, 30.00, north_m=120)
        far["accuracy_m"] = 8
        outcomes = run_stages(self.svc, op, DRIVER, self.clock, gps=far)
        self.assertEqual(outcomes[3], "quarantined")
        conflict = next(c for c in self.svc.list_conflicts(op["id"])
                        if c["code"] == "gps_outside_plot")
        self.svc.resolve_conflict(conflict["id"], {
            "action": "reject", "note": "机器误入相邻小区，收获数据作废"}, LEAD)
        # 链断裂：后续仅因顺序挂起的事件自动关闭，全部留在审计轨迹里
        statuses = {e["stage"]: e["status"] for e in self.svc.events_by_op[op["id"]]}
        self.assertTrue(all(v == "rejected" for v in statuses.values()
                            if v != "accepted"))
        report = self.svc.rebuild_plan("T2026-YF")
        self.assertEqual(report["effective_plot_count"], 0)
        excluded = next(e for e in report["excluded"] if e["plot_code"] == "P-A")
        self.assertEqual(excluded["reason"], "chain_incomplete")
        self.assertIn("正式收获", excluded["missing_stages"])

    def test_gps_low_accuracy_flagged(self):
        op = self.start_operation()
        # 中心点但精度差：只报低精度，不应当作越界
        self.clock.advance(5)
        outcome, record = self.svc.submit_event(op["id"], {
            "stage": "maturity_check", "occurred_at": iso(self.clock.t),
            "client_event_id": "m-gps",
            "payload": {"approved": True,
                        "gps": {"lng": 104.00, "lat": 30.00, "accuracy_m": 50}},
        }, DRIVER)
        self.assertEqual(outcome, "quarantined")
        codes = {c["code"] for c in record["conflicts"]}
        self.assertEqual(codes, {"gps_low_accuracy"})

    def test_offline_backfill_late_arrival(self):
        op = self.start_operation()
        # 先按真实顺序完成成熟度、清洁
        for i, (stage, payload) in enumerate([
            ("maturity_check", {"approved": True}),
            ("machine_clean", {"cleaned": True}),
        ]):
            self.clock.advance(5)
            self.svc.submit_event(op["id"], {
                "stage": stage, "occurred_at": iso(self.clock.t),
                "client_event_id": f"live-{i}", "payload": payload,
            }, DRIVER)
        # 设备离线：弃粮发生在清洁之前，补传到达更晚
        late = self.clock.t - timedelta(minutes=8)
        outcome, record = self.svc.submit_event(op["id"], {
            "stage": "first_grain_discard", "occurred_at": iso(late),
            "received_note": "设备离线补传",
            "client_event_id": "backfill-1",
            "payload": {"discarded_weight_kg": 11},
        }, DRIVER)
        self.assertEqual(outcome, "quarantined")
        conflict = record["conflicts"][0]
        self.assertEqual(conflict["code"], "late_arrival")
        self.assertGreater(conflict["related"]["backfill_lag_seconds"], 0)
        # 原始记录保留 received 晚于 occurred，不覆盖任何东西
        self.svc.resolve_conflict(conflict["id"], {
            "action": "accept", "note": "核对平板离线日志，补采信"}, LEAD)
        self.assertIn("first_grain_discard",
                      {e["stage"] for e in self.svc.accepted_events(op["id"])})

    def test_repeated_seal_scan_is_quarantined_and_rejectable(self):
        op_a = self.start_operation()
        run_stages(self.svc, op_a, DRIVER, self.clock,
                   gps={"lng": 104.00, "lat": 30.00})
        op_b = self.svc.create_operation(
            {"plan_code": "T2026-YF", "plot_code": "P-B",
             "machine_id": "M-1", "operator_id": "u_driver"}, LEAD)
        outcomes = run_stages(
            self.svc, op_b, DRIVER, self.clock,
            gps={"lng": 104.01, "lat": 30.00},
            seal_code="SEAL-A1",  # 重复封签
            sample_code="SMP-B1",
            storage_bin="B-01",
        )
        sealing_index = 5
        self.assertEqual(outcomes[sealing_index], "quarantined")
        conflicts = self.svc.list_conflicts(op_b["id"])
        seal_conflict = next(c for c in conflicts if c["code"] == "seal_already_scanned")
        self.assertEqual(seal_conflict["related"]["existing_sample_id"][:4], "smp_")
        self.svc.resolve_conflict(seal_conflict["id"], {
            "action": "reject", "note": "封签确属 P-A 样本，重新封签"}, LEAD)
        # 驳回后可用新封签重新上报
        self.clock.advance(5)
        outcome, _ = self.svc.submit_event(op_b["id"], {
            "stage": "sealing", "occurred_at": iso(self.clock.t),
            "client_event_id": "reseal-b",
            "payload": {"seal_code": "SEAL-B2", "sample_code": "SMP-B1"},
        }, DRIVER)
        self.assertEqual(outcome, "accepted")

    def test_idempotent_replay_returns_same_event(self):
        op = self.start_operation()
        self.clock.advance(5)
        body = {
            "stage": "maturity_check", "occurred_at": iso(self.clock.t),
            "client_event_id": "idem-1", "payload": {"approved": True},
        }
        outcome1, rec1 = self.svc.submit_event(op["id"], body, DRIVER)
        self.clock.advance(30)
        outcome2, rec2 = self.svc.submit_event(op["id"], body, DRIVER)
        self.assertEqual(outcome1, "accepted")
        self.assertEqual(outcome2, "replay")
        self.assertEqual(rec1["id"], rec2["event"]["id"])
        self.assertEqual(len(self.svc.accepted_events(op["id"])), 1)

    def test_same_client_id_different_payload_is_conflict(self):
        op = self.start_operation()
        self.clock.advance(5)
        self.svc.submit_event(op["id"], {
            "stage": "maturity_check", "occurred_at": iso(self.clock.t),
            "client_event_id": "dup", "payload": {"approved": True},
        }, DRIVER)
        self.clock.advance(5)
        outcome, record = self.svc.submit_event(op["id"], {
            "stage": "maturity_check", "occurred_at": iso(self.clock.t),
            "client_event_id": "dup", "payload": {"approved": False},
        }, DRIVER)
        self.assertEqual(outcome, "quarantined")
        self.assertIn("duplicate_event", {c["code"] for c in record["conflicts"]})

    def test_reject_cascades_to_other_conflicts_on_same_event(self):
        op = self.start_operation()
        # 离线补传 + 漂移同时出现
        self.clock.advance(10)
        self.svc.submit_event(op["id"], {
            "stage": "maturity_check", "occurred_at": iso(self.clock.t),
            "client_event_id": "a1", "payload": {"approved": True},
        }, DRIVER)
        near = offset_point(104.00, 30.00, north_m=28)
        late = self.clock.t - timedelta(minutes=5)
        outcome, record = self.svc.submit_event(op["id"], {
            "stage": "machine_clean", "occurred_at": iso(late),
            "client_event_id": "a2",
            "payload": {"cleaned": True, "gps": near},
        }, DRIVER)
        self.assertEqual(outcome, "quarantined")
        self.assertGreaterEqual(len(record["conflicts"]), 2)
        first = record["conflicts"][0]
        self.svc.resolve_conflict(first["id"], {
            "action": "reject", "note": "清洁记录不可信"}, LEAD)
        for c in self.svc.list_conflicts(op["id"], "all"):
            self.assertNotEqual(c["status"], "open")

    def test_high_severity_accept_requires_lead_or_admin(self):
        from domain import DomainError
        op = self.start_operation()
        self.clock.advance(5)
        _, record = self.svc.submit_event(op["id"], {
            "stage": "maturity_check", "occurred_at": iso(self.clock.t),
            "client_event_id": "x",
            "payload": {"approved": True,
                        "gps": {"lng": 104.5, "lat": 30.5, "accuracy_m": 5}},
        }, DRIVER)
        conflict = next(c for c in record["conflicts"]
                        if c["code"] == "gps_outside_plot")
        with self.assertRaises(DomainError) as ctx:
            self.svc.resolve_conflict(conflict["id"], {
                "action": "accept", "note": "强行采信"}, DRIVER)
        self.assertEqual(ctx.exception.code, "forbidden_override")

    def test_accept_requires_note(self):
        from domain import DomainError
        op = self.start_operation()
        near = offset_point(104.00, 30.00, north_m=28)
        self.clock.advance(5)
        _, record = self.svc.submit_event(op["id"], {
            "stage": "maturity_check", "occurred_at": iso(self.clock.t),
            "client_event_id": "y",
            "payload": {"approved": True, "gps": near},
        }, DRIVER)
        with self.assertRaises(DomainError) as ctx:
            self.svc.resolve_conflict(record["conflicts"][0]["id"],
                                      {"action": "accept"}, LEAD)
        self.assertEqual(ctx.exception.code, "missing_resolution_note")

    # -- 样本谱系 ----------------------------------------------------------

    def _one_sealed_sample(self):
        op = self.start_operation()
        run_stages(self.svc, op, DRIVER, self.clock,
                   gps={"lng": 104.00, "lat": 30.00})
        return self.svc.sample_view("SMP-A1")

    def test_sample_lineage_after_transfer_split_consume(self):
        sample = self._one_sealed_sample()
        self.assertEqual(sample["status"], "sealed")
        self.svc.transfer_sample("SMP-A1", {
            "lab": "永丰区域中心实验室", "handed_to": "周检验"}, LAB)
        view = self.svc.split_sample("SMP-A1", {
            "items": [
                {"sample_code": "SMP-A1-a", "weight_kg": 300,
                 "seal_code": "SEAL-A1a", "note": "发芽率检测份"},
                {"sample_code": "SMP-A1-b", "weight_kg": 200},
            ]}, LAB)
        codes = {c["sample_code"] for c in view["children"]}
        self.assertEqual(codes, {"SMP-A1-a", "SMP-A1-b"})
        self.assertAlmostEqual(view["remaining_weight_kg"], 820.5 - 500, places=3)

        child = self.svc.sample_view("SMP-A1-a")
        chain = [n["sample_code"] for n in child["lineage"]]
        self.assertEqual(chain, ["SMP-A1", "SMP-A1-a"])
        origin = child["lineage"][0]
        self.assertEqual(origin["plot_code"], "P-A")
        self.assertEqual(origin["cultivar_generation"], "F6")
        self.assertEqual(origin["sowing_batch"], "B2026-04-01")

        self.svc.consume_sample("SMP-A1-a", {
            "amount_kg": 120, "purpose": "发芽率试验"}, LAB)
        self.assertAlmostEqual(
            self.svc.sample_view("SMP-A1-a")["remaining_weight_kg"], 180, places=3)

    def test_split_beyond_remaining_rejected(self):
        from domain import DomainError
        self._one_sealed_sample()
        self.svc.transfer_sample("SMP-A1", {"lab": "L", "handed_to": "周"}, LAB)
        with self.assertRaises(DomainError) as ctx:
            self.svc.split_sample("SMP-A1", {"items": [
                {"sample_code": "X", "weight_kg": 9999}]}, LAB)
        self.assertEqual(ctx.exception.code, "insufficient_sample")

    def test_consume_beyond_remaining_rejected(self):
        from domain import DomainError
        self._one_sealed_sample()
        self.svc.transfer_sample("SMP-A1", {"lab": "L", "handed_to": "周"}, LAB)
        with self.assertRaises(DomainError) as ctx:
            self.svc.consume_sample("SMP-A1", {
                "amount_kg": 10000, "purpose": "x"}, LAB)
        self.assertEqual(ctx.exception.code, "insufficient_sample")

    def test_lab_actions_require_prior_transfer(self):
        from domain import DomainError
        self._one_sealed_sample()
        with self.assertRaises(DomainError) as ctx:
            self.svc.consume_sample("SMP-A1", {"amount_kg": 1, "purpose": "x"}, LAB)
        self.assertEqual(ctx.exception.code, "sample_not_transferred")

    def test_void_with_active_children_blocked_then_allowed(self):
        from domain import DomainError
        self._one_sealed_sample()
        self.svc.transfer_sample("SMP-A1", {"lab": "L", "handed_to": "周"}, LAB)
        self.svc.split_sample("SMP-A1", {"items": [
            {"sample_code": "C1", "weight_kg": 10}]}, LAB)
        with self.assertRaises(DomainError) as ctx:
            self.svc.void_sample("SMP-A1", {"reason": "霉变"}, LAB)
        self.assertEqual(ctx.exception.code, "children_still_active")
        self.svc.void_sample("C1", {"reason": "分装件标签污损"}, LAB)
        result = self.svc.void_sample("SMP-A1", {"reason": "霉变"}, LAB)
        self.assertEqual(result["status"], "voided")
        # 作废样本从有效产量清单剔除
        report = self.svc.rebuild_plan("T2026-YF")
        self.assertEqual(report["effective_plot_count"], 0)

    # -- 机器轨迹反查 ------------------------------------------------------

    def test_trace_conflict_back_to_prior_plots(self):
        plan = self.svc.get_plan("T2026-YF") if "T2026-YF" in self.svc.plans_by_code \
            else self.svc.create_plan(two_plot_plan(), LEAD)
        op_a = self.svc.create_operation(
            {"plan_code": "T2026-YF", "plot_code": "P-A",
             "machine_id": "M-1", "operator_id": "u_driver"}, LEAD)
        flow_a = stage_payloads("M-1", gps={"lng": 104.00, "lat": 30.00})
        for i, (stage, payload) in enumerate(flow_a):
            self.clock.advance(5)
            self.svc.submit_event(op_a["id"], {
                "stage": stage, "occurred_at": iso(self.clock.t),
                "client_event_id": f"a-{i}", "payload": payload}, DRIVER)
        op_b = self.svc.create_operation(
            {"plan_code": "T2026-YF", "plot_code": "P-B",
             "machine_id": "M-1", "operator_id": "u_driver"}, LEAD)
        # B 地块：复核、清洁正常，收获时定位飞点
        for i, (stage, payload) in enumerate([
            ("maturity_check", {"approved": True}),
            ("machine_clean", {"cleaned": True}),
            ("first_grain_discard", {"discarded_weight_kg": 8}),
        ]):
            self.clock.advance(5)
            self.svc.submit_event(op_b["id"], {
                "stage": stage, "occurred_at": iso(self.clock.t),
                "client_event_id": f"b-pre-{i}", "payload": payload}, DRIVER)
        self.clock.advance(5)
        _, record = self.svc.submit_event(op_b["id"], {
            "stage": "harvest", "occurred_at": iso(self.clock.t),
            "client_event_id": "b-h",
            "payload": {"machine_id": "M-1",
                        "gps": {"lng": 104.5, "lat": 30.5, "accuracy_m": 5}}}, DRIVER)
        cid = next(c["id"] for c in record["conflicts"]
                   if c["code"] == "gps_outside_plot")
        trace = self.svc.trace_conflict(cid)
        prior_plots = [s["plot_code"] for s in trace["prior_stops"]]
        # 能看到机器当天此前清仓/收获过的 A、B 田块，且严格按时间排列
        self.assertIn("P-A", prior_plots)
        self.assertTrue(
            all(trace["prior_stops"][i]["at"] <= trace["prior_stops"][i + 1]["at"]
                for i in range(len(trace["prior_stops"]) - 1)))
        self.assertEqual(trace["anchor_plot_code"], "P-B")

    # -- 存储重启恢复 ------------------------------------------------------

    def test_state_survives_restart_including_resolutions(self):
        op = self.start_operation()
        near = offset_point(104.00, 30.00, north_m=28)
        run_stages(self.svc, op, DRIVER, self.clock, gps=near)
        cid = self.svc.list_conflicts(op["id"])[0]["id"]
        self.svc.resolve_conflict(cid, {"action": "reject", "note": "作废"}, LEAD)
        # 封样因收获驳回而没有样本；重建前重启：状态必须由裁决流派生
        reopened = HarvestService(self.store, clock=self.clock)
        events = reopened.events_by_op[op["id"]]
        harvest = next(e for e in events if e["stage"] == "harvest")
        self.assertEqual(harvest["status"], "rejected")
        conflict = reopened.conflicts[cid]
        self.assertEqual(conflict["status"], "resolved")
        self.assertEqual(reopened.rebuild_plan("T2026-YF")["effective_plot_count"], 0)

    def test_happy_path_survives_restart(self):
        op = self.start_operation()
        run_stages(self.svc, op, DRIVER, self.clock,
                   gps={"lng": 104.00, "lat": 30.00})
        reopened = HarvestService(self.store, clock=self.clock)
        sample = reopened.sample_view("SMP-A1")
        self.assertAlmostEqual(sample["net_weight_kg"], 820.5, places=3)
        report = reopened.rebuild_plan("T2026-YF")
        self.assertEqual(report["effective_plot_count"], 1)

    # -- 其他规则 ----------------------------------------------------------

    def test_duplicate_storage_bin_flagged(self):
        op_a = self.start_operation()
        run_stages(self.svc, op_a, DRIVER, self.clock,
                   gps={"lng": 104.00, "lat": 30.00})
        op_b = self.svc.create_operation(
            {"plan_code": "T2026-YF", "plot_code": "P-B",
             "machine_id": "M-1", "operator_id": "u_driver"}, LEAD)
        outcomes = run_stages(self.svc, op_b, DRIVER, self.clock,
                              gps={"lng": 104.01, "lat": 30.00},
                              seal_code="SEAL-B1", sample_code="SMP-B1",
                              storage_bin="A-01")  # 与 P-A 同货位
        self.assertEqual(outcomes[-1], "quarantined")
        self.assertEqual(
            self.svc.list_conflicts(op_b["id"])[0]["code"], "duplicate_storage_bin")

    def test_parallel_operation_on_same_plot_rejected(self):
        from domain import DomainError
        self.start_operation()
        with self.assertRaises(DomainError) as ctx:
            self.svc.create_operation(
                {"plan_code": "T2026-YF", "plot_code": "P-A",
                 "machine_id": "M-2", "operator_id": "u_driver"}, LEAD)
        self.assertEqual(ctx.exception.code, "plot_operation_active")

    def test_harvest_rejection_aborts_and_plot_can_restart(self):
        op = self.start_operation()
        run_stages(self.svc, op, DRIVER, self.clock,
                   gps=offset_point(104.00, 30.00, north_m=120))
        conflict = next(c for c in self.svc.list_conflicts(op["id"])
                        if c["code"] == "gps_outside_plot")
        self.svc.resolve_conflict(conflict["id"],
                                  {"action": "reject", "note": "污染风险"}, LEAD)
        self.assertEqual(self.svc._get_operation(op["id"])["status"], "aborted")
        # 同一地块可重开新作业并完整走通
        op2 = self.svc.create_operation(
            {"plan_code": "T2026-YF", "plot_code": "P-A",
             "machine_id": "M-1", "operator_id": "u_driver"}, LEAD)
        outcomes = run_stages(self.svc, op2, DRIVER, self.clock,
                              gps={"lng": 104.00, "lat": 30.00},
                              seal_code="SEAL-A2", sample_code="SMP-A2",
                              storage_bin="A-02")
        self.assertEqual(outcomes, ["accepted"] * 7)
        self.assertEqual(self.svc._get_operation(op2["id"])["status"], "completed")
        # 重建只取有效作业，旧作业进入排除清单
        report = self.svc.rebuild_plan("T2026-YF")
        self.assertEqual(report["effective_plot_count"], 1)
        self.assertEqual(report["plots"][0]["operation_id"], op2["id"])

    def test_weighing_reject_can_be_corrected_and_chain_resumes(self):
        op = self.start_operation()
        # 前四步正常
        for i, (stage, payload) in enumerate([
            ("maturity_check", {"approved": True}),
            ("machine_clean", {"cleaned": True}),
            ("first_grain_discard", {"discarded_weight_kg": 10}),
            ("harvest", {"machine_id": "M-1"}),
        ]):
            self.clock.advance(5)
            self.svc.submit_event(op["id"], {
                "stage": stage, "occurred_at": iso(self.clock.t),
                "client_event_id": f"ok-{i}", "payload": payload}, DRIVER)
        # 机手重复提交了一次称重（stage_repeated，high），负责人驳回误传记录
        self.clock.advance(5)
        outcome, _ = self.svc.submit_event(op["id"], {
            "stage": "weighing", "occurred_at": iso(self.clock.t),
            "client_event_id": "w-1", "payload": {"net_weight_kg": 100}}, DRIVER)
        self.assertEqual(outcome, "accepted")
        self.clock.advance(5)
        _, w2 = self.svc.submit_event(op["id"], {
            "stage": "weighing", "occurred_at": iso(self.clock.t),
            "client_event_id": "w-2", "payload": {"net_weight_kg": 999}}, DRIVER)
        self.assertEqual(w2["conflicts"][0]["code"], "stage_repeated")
        self.svc.resolve_conflict(w2["conflicts"][0]["id"],
                                  {"action": "reject", "note": "误传"}, LEAD)
        # 称重之后阶段驳回不终止作业：原称重仍有效，封样、入库可继续
        self.assertEqual(self.svc._get_operation(op["id"])["status"], "active")
        for i, (stage, payload) in enumerate([
            ("sealing", {"seal_code": "SEAL-A1", "sample_code": "SMP-A1"}),
            ("storage", {"storage_bin": "A-01"}),
        ]):
            self.clock.advance(5)
            outcome, _ = self.svc.submit_event(op["id"], {
                "stage": stage, "occurred_at": iso(self.clock.t),
                "client_event_id": f"tail-{i}", "payload": payload}, DRIVER)
            self.assertEqual(outcome, "accepted")
        self.assertEqual(self.svc.sample_view("SMP-A1")["net_weight_kg"], 100)
        self.assertEqual(self.svc._get_operation(op["id"])["status"], "completed")


    def test_all_conflict_codes_have_explanations(self):
        from domain import CONFLICT_INFO
        for _code, (severity, text) in CONFLICT_INFO.items():
            self.assertIn(severity, ("low", "medium", "high"))
            self.assertGreater(len(text), 8)


if __name__ == "__main__":
    unittest.main()
