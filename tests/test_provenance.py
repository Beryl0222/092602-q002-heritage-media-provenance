"""来源与许可图谱业务规则测试。

覆盖：
指纹复用/声明冲突独立复核、佐证与矛盾保留、版本冻结、
事实更正（未发布停流/已发布更正记录+重判范围）、
许可撤回与到期找全受影响作品并延续复核、
创作者不能关闭自己的争议、片段溯源、命令行端到端。
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

from heritage_media_provenance import cli
from heritage_media_provenance.service import Service, ServiceError
from heritage_media_provenance.store import Store


def new_service() -> Service:
    return Service(Store())


def build_chain(service: Service, *, license_until: str = "2027-12-31",
                territories=("CN", "SG"), work_status: str = "draft") -> str:
    """构造最小链：iv(interview) → cl(clip) → wk(work)，授权挂在 iv 上。"""
    service.register_material({
        "material_id": "iv", "material_type": "interview",
        "title": "原始访谈", "fingerprint": "fp-1",
        "owner_id": "creator-a", "holder_id": "holder-h",
    })
    service.register_material({
        "material_id": "cl", "material_type": "clip",
        "title": "剪辑片段", "owner_id": "creator-a", "holder_id": "holder-h",
    })
    service.add_relation({"source_id": "iv", "target_id": "cl", "relation": "edits"})
    service.register_authorization({
        "license_id": "lic-1", "holder_id": "holder-h", "material_id": "iv",
        "territories": list(territories), "valid_from": "2025-01-01",
        "valid_until": license_until, "declaration": "partial territories",
    })
    service.register_work({"work_id": "wk", "creator_id": "creator-a",
                           "status": work_status})
    service.add_relation({"source_id": "cl", "target_id": "wk", "relation": "uses"})
    return "wk"


class 指纹与声明冲突测试(unittest.TestCase):
    def test_同指纹再次导入复用处理(self):
        service = new_service()
        service.register_material({
            "material_id": "m1", "material_type": "interview",
            "fingerprint": "ABC", "owner_id": "creator-a",
        })
        second = service.register_material({
            "material_id": "m2", "material_type": "interview",
            "fingerprint": "abc",  # 归一化后相同
            "owner_id": "creator-a", "license_declaration": "same declaration",
        })
        self.assertEqual(second["reused_from"], "m1")
        # m1 没有任何授权声明，无法构成声明差异，不产生冲突任务
        self.assertEqual(service.pending_review_tasks("m2")["count"], 0)

    def test_指纹一致但许可声明不同触发独立复核(self):
        service = new_service()
        service.register_material({
            "material_id": "m1", "material_type": "interview",
            "fingerprint": "FP-X", "owner_id": "creator-a",
        })
        service.register_authorization({
            "license_id": "l1", "holder_id": "h1", "material_id": "m1",
            "territories": ["CN"], "declaration": "仅限中国大陆",
        })
        result = service.register_material({
            "material_id": "m2", "material_type": "interview",
            "fingerprint": "FP-X", "owner_id": "creator-a",
            "license_declaration": "全球永久",
        })
        self.assertTrue(result["license_declaration_mismatch"])
        tasks = service.pending_review_tasks("m2")
        self.assertEqual(tasks["count"], 1)
        self.assertEqual(tasks["tasks"][0]["task_type"], "fingerprint_mismatch")
        task_id = tasks["tasks"][0]["task_id"]

        service.register_reviewer({"reviewer_id": "r1", "identity": "person"})
        service.register_reviewer({"reviewer_id": "creator-a", "identity": "person"})
        # 创作者本人不能复核自己的冲突
        with self.assertRaises(ServiceError):
            service.assign_review({"task_id": task_id, "reviewer_id": "creator-a"})
        # 独立人员可以指派并作出结论
        self.assertEqual(service.assign_review({"task_id": task_id, "reviewer_id": "r1"})
                         ["assignee_id"], "r1")
        resolved = service.resolve_fingerprint_mismatch({
            "task_id": task_id, "reviewer_id": "r1", "decision": "keep_existing",
        })
        self.assertEqual(resolved["decision"], "keep_existing")
        self.assertEqual(service.pending_review_tasks("m2")["count"], 0)

    def test_团队身份不受创作者身份限制(self):
        service = new_service()
        service.register_material({"material_id": "m1", "material_type": "interview",
                                   "fingerprint": "F", "owner_id": "u1"})
        service.register_authorization({"license_id": "l1", "holder_id": "h",
                                        "material_id": "m1", "territories": ["CN"],
                                        "declaration": "d1"})
        service.register_material({"material_id": "m2", "material_type": "interview",
                                   "fingerprint": "F", "owner_id": "u1",
                                   "license_declaration": "d2"})
        task_id = service.pending_review_tasks("m2")["tasks"][0]["task_id"]
        service.register_reviewer({"reviewer_id": "u1", "identity": "team"})
        # team 身份不视为创作者自审
        self.assertEqual(service.assign_review({"task_id": task_id, "reviewer_id": "u1"})
                         ["assignee_id"], "u1")

    def test_复核结论必须由登记在案人员作出(self):
        service = new_service()
        service.register_material({"material_id": "m1", "material_type": "interview",
                                   "fingerprint": "F", "owner_id": "u1"})
        service.register_authorization({"license_id": "l1", "holder_id": "h",
                                        "material_id": "m1", "territories": ["CN"],
                                        "declaration": "d1"})
        service.register_material({"material_id": "m2", "material_type": "interview",
                                   "fingerprint": "F", "owner_id": "u1",
                                   "license_declaration": "d2"})
        task_id = service.pending_review_tasks("m2")["tasks"][0]["task_id"]
        with self.assertRaises(ServiceError):
            service.resolve_fingerprint_mismatch(
                {"task_id": task_id, "reviewer_id": "ghost", "decision": "keep_existing"})


class 证据与结论测试(unittest.TestCase):
    def test_佐证共同支撑且矛盾必须保留(self):
        service = new_service()
        for mid, mtype in [("a", "interview"), ("b", "craft_step"), ("c", "clip")]:
            service.register_material({"material_id": mid, "material_type": mtype})
        service.add_conclusion({"conclusion_id": "k", "subject_id": "a",
                                "statement": "寓意为丰收"})
        service.add_evidence({"conclusion_id": "k", "material_id": "a", "stance": "supports"})
        service.add_evidence({"conclusion_id": "k", "material_id": "b", "stance": "supports"})
        report = service.add_evidence({"conclusion_id": "k", "material_id": "c",
                                       "stance": "contradicts"})
        self.assertEqual(len(report["supports"]), 2)
        self.assertEqual(len(report["contradictions"]), 1)
        self.assertTrue(report["has_unresolved_contradiction"])
        # 矛盾来源不会因重复登记而消失或膨胀
        again = service.add_evidence({"conclusion_id": "k", "material_id": "c",
                                      "stance": "contradicts", "detail": "再次登记"})
        self.assertEqual(len(again["contradictions"]), 1)
        self.assertEqual(again["contradictions"][0]["detail"], "再次登记")


class 版本冻结与更正处置测试(unittest.TestCase):
    def test_公开版本冻结证据与许可且范围限于授权地区(self):
        service = new_service()
        build_chain(service)
        published = service.publish_version({
            "work_id": "wk", "version_id": "v1",
            "evidence_material_ids": ["iv", "cl"],
        })
        version = published["version"]
        self.assertEqual(set(version["evidence_frozen"]), {"iv", "cl"})
        self.assertIn("lic-1", version["licenses_frozen"])
        self.assertEqual(set(version["territories_frozen"]), {"CN", "SG"})
        self.assertTrue(version["freeze_hash"])
        # 撤回授权不改变已冻结记录，但产生受影响作品的重判任务
        service.withdraw_authorization({"license_id": "lic-1"})
        self.assertIn("lic-1", service.store.get_version("v1").licenses_frozen)
        self.assertEqual(service.pending_review_tasks("wk")["count"], 1)

    def test_未发布依赖作品在事实更正后停止流转(self):
        service = new_service()
        build_chain(service, work_status="draft")
        service.register_material({"material_id": "iv2", "material_type": "interview",
                                   "holder_id": "holder-h", "owner_id": "creator-a"})
        result = service.correct_fact({"old_material_id": "iv", "new_material_id": "iv2",
                                       "holder_id": "holder-h"})
        self.assertEqual(result["held_unpublished_works"], ["wk"])
        self.assertEqual(service.store.get_work("wk").status, "held")
        self.assertTrue(any(t["task_type"] == "fact_correction"
                            for t in service.pending_review_tasks("wk")["tasks"]))
        # 旧素材留痕为已更正，不被删除
        self.assertEqual(service.store.get_material("iv").fact_status, "superseded")

    def test_已发布作品更正后形成更正记录并重新判断传播范围(self):
        service = new_service()
        build_chain(service, work_status="published")
        service.publish_version({"work_id": "wk", "version_id": "v1",
                                 "evidence_material_ids": ["iv", "cl"]})
        service.register_material({"material_id": "iv2", "material_type": "interview",
                                   "holder_id": "holder-h", "owner_id": "creator-a"})
        result = service.correct_fact({"old_material_id": "iv", "new_material_id": "iv2",
                                       "holder_id": "holder-h", "note": "传承人更正"})
        self.assertEqual(len(result["correction_ids"]), 1)
        correction = service.store.corrections_for("wk")[0]
        self.assertEqual((correction.old_material_id, correction.new_material_id),
                         ("iv", "iv2"))
        self.assertFalse(correction.rejudged)
        # 重新判断传播范围：新说法沿用原授权，范围不变，重判任务关闭
        report = service.rejudge_work_scope("wk")
        self.assertEqual(set(report["rejudged_scope"]), {"CN", "SG"})
        self.assertTrue(service.store.corrections_for("wk")[0].rejudged)
        self.assertEqual(service.pending_review_tasks("wk")["count"], 0)
        # 历史版本冻结内容不被改写
        self.assertIn("lic-1", service.store.get_version("v1").licenses_frozen)

    def test_只有传承人可以更正自己的事实(self):
        service = new_service()
        build_chain(service)
        service.register_material({"material_id": "iv2", "material_type": "interview",
                                   "holder_id": "holder-h", "owner_id": "creator-a"})
        with self.assertRaises(ServiceError):
            service.correct_fact({"old_material_id": "iv", "new_material_id": "iv2",
                                  "holder_id": "other-holder"})

    def test_已被更正的证据不能冻结进新版本(self):
        service = new_service()
        build_chain(service)
        service.register_material({"material_id": "iv2", "material_type": "interview",
                                   "holder_id": "holder-h", "owner_id": "creator-a"})
        service.correct_fact({"old_material_id": "iv", "new_material_id": "iv2",
                              "holder_id": "holder-h"})
        with self.assertRaises(ServiceError):
            service.publish_version({"work_id": "wk", "version_id": "bad",
                                     "evidence_material_ids": ["iv"]})

    def test_作品登记时同步进入图谱节点(self):
        service = new_service()
        build_chain(service)
        node = service.store.get_material("wk")
        self.assertIsNotNone(node)
        self.assertEqual(node.material_type, "derived_work")

    def test_新素材沿用原授权覆盖重判(self):
        service = new_service()
        build_chain(service, work_status="published")
        service.publish_version({"work_id": "wk", "version_id": "v1",
                                 "evidence_material_ids": ["iv"]})
        service.register_material({"material_id": "iv2", "material_type": "interview",
                                   "holder_id": "holder-h", "owner_id": "creator-a"})
        service.correct_fact({"old_material_id": "iv", "new_material_id": "iv2",
                              "holder_id": "holder-h"})
        report = service.rejudge_work_scope("wk")
        self.assertEqual(set(report["license_ids"]), {"lic-1"})
        self.assertEqual(set(report["rejudged_scope"]), {"CN", "SG"})


class 许可变更找全作品测试(unittest.TestCase):
    def test_撤回授权找全下游作品且不波及无关作品(self):
        service = new_service()
        build_chain(service, work_status="draft")
        service.register_work({"work_id": "wk2", "creator_id": "creator-a"})
        service.add_relation({"source_id": "cl", "target_id": "wk2", "relation": "uses"})
        # 与本次撤回完全无关的第三部作品
        service.register_material({"material_id": "other", "material_type": "interview"})
        service.register_work({"work_id": "wk3", "creator_id": "creator-b"})
        service.add_relation({"source_id": "other", "target_id": "wk3", "relation": "uses"})

        result = service.withdraw_authorization({"license_id": "lic-1"})
        self.assertEqual(set(result["impacted_works"]), {"wk", "wk2"})
        self.assertNotIn("wk3", result["impacted_works"])
        for wid in ("wk", "wk2"):
            self.assertEqual(service.store.get_work(wid).status, "held")
        self.assertEqual(service.store.get_work("wk3").status, "draft")

    def test_撤回已发布作品产生重判任务且不自动改范围(self):
        service = new_service()
        build_chain(service, work_status="published")
        service.publish_version({"work_id": "wk", "version_id": "v1",
                                 "evidence_material_ids": ["iv", "cl"]})
        result = service.withdraw_authorization({"license_id": "lic-1"})
        self.assertEqual(result["impacted_works"], ["wk"])
        self.assertTrue(any(t["task_type"] == "rejudge_scope"
                            for t in service.pending_review_tasks("wk")["tasks"]))
        # 重判前作品维持原范围
        self.assertEqual(set(service.store.get_work("wk").region_scope), {"CN", "SG"})
        # 重判后范围清空
        report = service.rejudge_work_scope("wk")
        self.assertEqual(report["rejudged_scope"], [])
        self.assertEqual(set(report["removed_territories"]), {"CN", "SG"})

    def test_冻结过被撤回许可的历史版本也算受影响作品(self):
        service = new_service()
        build_chain(service, work_status="published")
        service.publish_version({"work_id": "wk", "version_id": "v1",
                                 "evidence_material_ids": ["iv", "cl"]})
        # 作品改用新素材链路，与 iv 不再有图谱边，但 v1 冻结过 lic-1
        service.register_material({"material_id": "fresh", "material_type": "interview",
                                   "owner_id": "creator-a"})
        result = service.withdraw_authorization({"license_id": "lic-1"})
        self.assertIn("wk", result["impacted_works"])

    def test_撤回不存在的授权报错(self):
        service = new_service()
        with self.assertRaises(ServiceError):
            service.withdraw_authorization({"license_id": "nope"})


class 许可期限测试(unittest.TestCase):
    def test_登记时按当前日期判定生效(self):
        service = new_service()
        service.register_material({"material_id": "m", "material_type": "interview"})
        saved = service.register_authorization({
            "license_id": "l", "holder_id": "h", "material_id": "m",
            "territories": ["CN"], "valid_from": "2025-01-01", "valid_until": "2026-06-30",
        })
        # 测试运行日 2026-10-07 已晚于到期日
        self.assertFalse(saved["effective_today"])

    def test_到期批量处理并为受影响已发布作品建任务(self):
        service = new_service()
        build_chain(service, work_status="published")
        service.publish_version({"work_id": "wk", "version_id": "v1",
                                 "evidence_material_ids": ["iv"]})
        # 2026-03-01 时授权有效
        self.assertEqual(set(service.availability("iv", date(2026, 3, 1))
                             ["territories_available"]), {"CN", "SG"})
        # lic-1 2027 年才到期
        self.assertEqual(service.expire_authorizations(date(2026, 10, 7))["expired_licenses"], [])
        # 到 2028 年批量到期
        result = service.expire_authorizations(date(2028, 1, 1))
        self.assertEqual(result["expired_licenses"], ["lic-1"])
        self.assertIn("wk", result["impacted_works"])
        self.assertTrue(any(t["task_type"] == "rejudge_scope"
                            for t in service.pending_review_tasks("wk")["tasks"]))
        # 二次执行幂等
        self.assertEqual(service.expire_authorizations(date(2028, 1, 1))["expired_licenses"], [])

    def test_授权必填校验(self):
        service = new_service()
        with self.assertRaises(ServiceError):
            service.register_authorization({"license_id": "x"})


class 争议规则测试(unittest.TestCase):
    def test_创作者不能关闭自己的争议(self):
        service = new_service()
        build_chain(service)
        service.open_dispute({"dispute_id": "d1", "subject_id": "wk",
                              "opened_by": "holder-h", "reason": "剪辑反转原意"})
        with self.assertRaises(ServiceError):
            service.close_dispute({"dispute_id": "d1", "closed_by": "creator-a"})

    def test_非创作者可以关闭争议且创作者拒绝留有审计事件(self):
        service = new_service()
        build_chain(service)
        service.open_dispute({"dispute_id": "d1", "subject_id": "wk",
                              "opened_by": "holder-h"})
        with self.assertRaises(ServiceError):
            service.close_dispute({"dispute_id": "d1", "closed_by": "creator-a"})
        self.assertIn("dispute_close_rejected",
                      [e.event_type for e in service.store.events_for("d1")])
        # 申诉人本人（非创作者）可以撤回争议
        closed = service.close_dispute({"dispute_id": "d1", "closed_by": "holder-h"})
        self.assertEqual(closed["status"], "closed")

    def test_创作者关闭素材类争议同样被拒(self):
        service = new_service()
        build_chain(service)
        service.open_dispute({"dispute_id": "d2", "subject_id": "cl",
                              "opened_by": "holder-h"})
        with self.assertRaises(ServiceError):
            service.close_dispute({"dispute_id": "d2", "closed_by": "creator-a"})


class 溯源报告测试(unittest.TestCase):
    def test_片段溯源包含来源加工可用地区与待处理(self):
        service = new_service()
        build_chain(service)
        report = service.trace("cl", date(2026, 3, 1))
        self.assertEqual(report["kind"], "material")
        # 来自哪里
        self.assertEqual(report["origin"]["root_material_ids"], ["iv"])
        # 经历过哪些加工
        rels = [(e["source_id"], e["relation"], e["target_id"])
                for e in report["processing"]["upstream_processing_chain"]]
        self.assertIn(("iv", "edits", "cl"), rels)
        # 在哪些地区和期限内可用
        self.assertEqual(set(report["availability"]["territories_available"]), {"CN", "SG"})
        # 当前必须处理的工作（暂无）
        self.assertEqual(report["must_handle_count"], 0)

    def test_作品溯源显示冻结版本更正记录与待处理(self):
        service = new_service()
        build_chain(service, work_status="published")
        service.publish_version({"work_id": "wk", "version_id": "v1",
                                 "evidence_material_ids": ["iv", "cl"]})
        report = service.trace("wk")
        self.assertEqual(report["kind"], "work")
        self.assertEqual(len(report["public_versions"]), 1)
        self.assertEqual(set(report["public_versions"][0]["licenses_frozen"]), {"lic-1"})
        # 撤回后溯源仍显示冻结版本，但出现必须处理的任务
        service.withdraw_authorization({"license_id": "lic-1"})
        report2 = service.trace("wk")
        self.assertGreaterEqual(report2["must_handle_count"], 1)

    def test_钻石形合并的递归去重与断环(self):
        service = new_service()
        for mid in ("r", "a", "b", "d"):
            service.register_material({"material_id": mid, "material_type": "interview"})
        service.add_relation({"source_id": "r", "target_id": "a", "relation": "derives_from"})
        service.add_relation({"source_id": "r", "target_id": "b", "relation": "derives_from"})
        service.add_relation({"source_id": "a", "target_id": "d", "relation": "derives_from"})
        service.add_relation({"source_id": "b", "target_id": "d", "relation": "derives_from"})
        # 额外边构成环，递归必须终止
        service.add_relation({"source_id": "d", "target_id": "r", "relation": "derives_from"})
        self.assertEqual(len(service.store.lineage("d", "up")), 4)
        self.assertEqual(len(service.store.lineage("r", "down")), 4)

    def test_找不到片段时报错(self):
        service = new_service()
        with self.assertRaises(ServiceError):
            service.trace("ghost")

    def test_片段溯源能看到下游作品必须处理的工作(self):
        service = new_service()
        build_chain(service, work_status="published")
        service.publish_version({"work_id": "wk", "version_id": "v1",
                                 "evidence_material_ids": ["iv", "cl"]})
        # 片段 cl 本身没有任务，但下游作品 wk 因授权撤回必须重判
        service.withdraw_authorization({"license_id": "lic-1"})
        report = service.trace("cl")
        self.assertEqual(report["pending_review_tasks"], [])
        self.assertEqual(len(report["downstream_pending_tasks"]), 1)
        self.assertEqual(report["downstream_pending_tasks"][0]["on_downstream"], "wk")
        self.assertEqual(report["must_handle_count"], 1)


class CLI端到端测试(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "graph.db")

    def _run(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_validate与health(self):
        path = Path(self.tmp.name) / "sample.json"
        path.write_text(json.dumps({"record_id": "x", "owner_id": "o", "state": "collected"}),
                        encoding="utf-8")
        code, out, _ = self._run("validate", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["state"], "collected")
        code, out, _ = self._run("health")
        self.assertEqual(json.loads(out)["status"], "ok")

    def test_scenario_then_trace_tasks_persist(self):
        code, _, err = self._run("scenario", "data/heritage_scenario.json", "--db", self.db)
        self.assertEqual(code, 0, err)
        code, out, err = self._run("trace", "clip-cut", "--db", self.db)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn("interview-orig", data["origin"]["root_material_ids"])
        self.assertTrue(data["processing"]["upstream_processing_chain"])
        code, out, err = self._run("tasks", "--db", self.db)
        self.assertEqual(code, 0, err)
        self.assertGreater(json.loads(out)["count"], 0)

    def test_trace作品与创作者关闭争议被拒(self):
        code, _, err = self._run("scenario", "data/heritage_scenario.json", "--db", self.db)
        self.assertEqual(code, 0, err)
        code, out, err = self._run("trace", "work-gallery", "--db", self.db)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["kind"], "work")
        # 争议此前已被独立人员关闭；创作者再关仍被拒绝
        code, _, err = self._run("close-dispute", "--dispute", "dispute-1",
                                 "--by", "creator-wang", "--db", self.db)
        self.assertEqual(code, 2)
        self.assertIn("不能关闭", err)

    def test_expire_cli_as_of(self):
        code, _, err = self._run("scenario", "data/heritage_scenario.json", "--db", self.db)
        self.assertEqual(code, 0, err)
        # 场景装载时已按 2026-10-07 执行过到期处置，lic-ref-1 应为 expired 且幂等
        code, out, err = self._run("expire", "--as-of", "2026-10-07", "--db", self.db)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["expired_licenses"], [])
        from heritage_media_provenance.store import Store
        self.assertEqual(Store(self.db).get_authorization("lic-ref-1").status, "expired")


if __name__ == "__main__":
    unittest.main()
