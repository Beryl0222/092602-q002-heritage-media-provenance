"""来源与许可图谱的端到端规则测试。

场景：传承人 master-zhang 的口述访谈被 creator-li 剪辑成片段并发布成片，
覆盖指纹复用、说法矛盾、传承人更正、授权撤回/到期、争议回避与溯源。
"""
import unittest

from heritage_media_provenance.service import Service, ServiceError
from heritage_media_provenance.store import Store

T = "2026-06-01T00:00:00+00:00"


def build_service() -> Service:
    service = Service(Store())
    # 原始访谈（传承人口述）。
    service.import_material({
        "material_id": "itv-001", "material_type": "interview",
        "fingerprint": "fp-itv", "title": "张师傅染色口述",
        "creator_id": "creator-li", "holder_id": "master-zhang",
    })
    service.add_license({
        "license_id": "lic-001", "fingerprint": "fp-itv",
        "holder_id": "master-zhang", "creator_id": "creator-li",
        "territories": ["CN", "SG"], "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_until": "2026-12-31T23:59:59+00:00",
        "declared_terms": "口头授权：完整口述可用于非遗纪录短片",
    })
    service.attach_license({"material_id": "itv-001", "license_id": "lic-001"})
    # 剪辑片段与成片。
    service.import_material({
        "material_id": "clip-001", "material_type": "clip",
        "fingerprint": "fp-clip", "title": "染色片段",
        "creator_id": "creator-li", "holder_id": "master-zhang",
    })
    service.add_derivation({
        "parent_id": "itv-001", "child_id": "clip-001",
        "process": "edit", "actor_id": "creator-li",
        "note": "从访谈中剪出染色段落",
    })
    service.import_material({
        "material_id": "work-001", "material_type": "work",
        "fingerprint": "fp-work", "title": "蓝染技艺短片",
        "creator_id": "creator-li", "holder_id": "master-zhang",
        "state": "draft",
    })
    service.add_derivation({
        "parent_id": "clip-001", "child_id": "work-001",
        "process": "cut", "actor_id": "creator-li",
    })
    # 另一条尚未发布的派生作品（更正时应停止流转）。
    service.import_material({
        "material_id": "work-draft", "material_type": "work",
        "fingerprint": "fp-work-draft", "title": "待发布混剪",
        "creator_id": "creator-li", "holder_id": "master-zhang",
        "state": "draft",
    })
    service.add_derivation({
        "parent_id": "clip-001", "child_id": "work-draft",
        "process": "mix", "actor_id": "creator-li",
    })
    return service


class 指纹与说法规则测试(unittest.TestCase):
    def test_同一指纹再次导入复用已有处理(self):
        service = build_service()
        again = service.import_material({
            "material_id": "itv-other-id", "material_type": "interview",
            "fingerprint": "fp-itv", "creator_id": "creator-li",
        })
        self.assertTrue(again["reused"])
        self.assertEqual(again["material"]["material_id"], "itv-001")

    def test_指纹一致但许可声明不同交独立复核(self):
        service = build_service()
        result = service.add_license({
            "license_id": "lic-009", "fingerprint": "fp-itv",
            "holder_id": "master-zhang", "creator_id": "creator-li",
            "territories": ["CN"], "valid_from": "2026-01-01T00:00:00+00:00",
            "declared_terms": "书面授权：仅限教学使用",  # 声明与 lic-001 不同
        })
        self.assertTrue(result["review_required"])
        # 创作者本人不能复核自己的许可冲突。
        with self.assertRaises(ServiceError):
            service.resolve_review_task({
                "task_id": result["review_task_id"], "closer_id": "creator-li",
            })
        # 独立人员可以复核；再次登记同指纹冲突声明时复核任务延续（不新增）。
        service.add_license({
            "license_id": "lic-010", "fingerprint": "fp-itv",
            "holder_id": "master-zhang", "creator_id": "creator-li",
            "territories": ["CN"], "valid_from": "2026-01-01T00:00:00+00:00",
            "declared_terms": "第三方邮件：禁止商用",
        })
        open_tasks = service.list_review_tasks("open")
        self.assertEqual(len(open_tasks), 1)
        closed = service.resolve_review_task({
            "task_id": result["review_task_id"], "closer_id": "reviewer-chen",
        })
        self.assertEqual(closed["status"], "closed")

    def test_互相佐证可共同支撑_矛盾来源必须保留分歧(self):
        service = build_service()
        service.add_claim({
            "claim_id": "c-oral", "material_id": "itv-001",
            "subject": "染色工艺", "content": "全部手工染色",
            "source_id": "master-zhang",
        })
        service.add_claim({
            "claim_id": "c-ref", "material_id": "itv-001",
            "subject": "染色工艺", "content": "文献记载该工序为手工完成",
        })
        service.relate_claims({
            "from_claim_id": "c-oral", "to_claim_id": "c-ref",
            "relation": "corroborate",
        })
        published = service.publish_work({
            "work_id": "work-001", "published_by": "editorial-board",
            "territories": ["CN"], "published_at": T,
        })
        self.assertEqual(published["version"], 1)
        self.assertIn("c-oral", published["evidence_ids"])

        # 剪辑把含义剪反：新说法与口述矛盾 → 禁止发布，矛盾在溯源中保留。
        service.add_claim({
            "claim_id": "c-flipped", "material_id": "clip-001",
            "subject": "染色工艺", "content": "传承人承认使用机器染色",
            "source_id": "creator-li", "polarity": "negative",
        })
        service.relate_claims({
            "from_claim_id": "c-flipped", "to_claim_id": "c-oral",
            "relation": "contradict", "note": "剪辑后语义与口述相反",
        })
        with self.assertRaises(ServiceError):
            service.publish_work({
                "work_id": "work-draft", "published_by": "editorial-board",
                "territories": ["CN"], "published_at": T,
            })
        traced = service.trace("clip-001")
        self.assertIn(("c-flipped", "c-oral"),
                      [tuple(pair) for pair in traced["contradictions"]])


class 更正与发布冻结测试(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.service.add_claim({
            "claim_id": "c-old", "material_id": "itv-001",
            "subject": "染料来源", "content": "板蓝根自种",
            "source_id": "master-zhang",
        })
        self.published = self.service.publish_work({
            "work_id": "work-001", "published_by": "editorial-board",
            "territories": ["CN", "SG"], "published_at": T,
        })

    def test_公开版本冻结当时证据与许可(self):
        self.assertEqual(self.published["evidence_ids"], ["c-old"])
        self.assertEqual(self.published["license_ids"], ["lic-001"])
        self.assertEqual(self.published["territories"], ["CN", "SG"])
        # 冻结记录不可变：再次发布产生新版本。
        again = self.service.publish_work({
            "work_id": "work-001", "published_by": "editorial-board",
            "territories": ["CN"], "published_at": T,
        })
        self.assertEqual(again["version"], 2)
        versions = self.service.list_publications("work-001")
        self.assertEqual([v["version"] for v in versions], [1, 2])

    def test_传承人更正_未发布停转_已发布更正记录并重判(self):
        result = self.service.correct_claim({
            "holder_id": "master-zhang", "old_claim_id": "c-old",
            "claim_id": "c-new", "subject": "染料来源",
            "content": "板蓝根是从邻县合作社购入，并非自种",
            "reason": "传承人核对后更正",
        })
        # 未发布作品停止流转。
        self.assertEqual(result["blocked_works"], ["work-draft"])
        self.assertEqual(self.service.store.get_material("work-draft").state,
                         "blocked")
        # 已发布作品形成更正记录。
        self.assertEqual(len(result["corrected_publications"]), 1)
        entry = result["corrected_publications"][0]
        self.assertEqual(entry["publication_id"], "pub-work-001-v1")
        self.assertEqual(self.service.store.get_material("work-001").state,
                         "corrected")
        corrections = self.service.store.list_corrections("work-001")
        self.assertEqual(corrections[0]["old_claim_id"], "c-old")
        self.assertEqual(corrections[0]["new_claim_id"], "c-new")
        # 旧说法失效，新说法生效；待处理清单包含两部作品。
        self.assertEqual(self.service.store.get_claim("c-old").state, "corrected")
        pending = {item["work_id"] for item in self.service.pending_works()}
        self.assertEqual(pending, {"work-001", "work-draft"})

    def test_只有传承人本人能更正(self):
        with self.assertRaises(ServiceError):
            self.service.correct_claim({
                "holder_id": "creator-li", "old_claim_id": "c-old",
                "claim_id": "c-fake", "subject": "染料来源", "content": "x",
            })


class 授权撤回到期与争议测试(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.service.add_claim({
            "claim_id": "c-1", "material_id": "itv-001",
            "subject": "工序", "content": "浸泡三次",
            "source_id": "master-zhang",
        })
        self.service.publish_work({
            "work_id": "work-001", "published_by": "editorial-board",
            "territories": ["CN"], "published_at": T,
        })

    def test_授权撤回找全受影响作品并延续复核(self):
        result = self.service.revoke_license("lic-001")
        self.assertIn("work-001", result["affected_works"])
        self.assertIn("work-draft", result["affected_works"])
        self.assertEqual(self.service.store.get_material("work-001").state,
                         "withdrawn")
        # 冻结版本仍引用该授权，故已公开作品也必须被找全。
        tasks = {t["material_id"] for t in self.service.list_review_tasks("open")}
        self.assertIn("work-001", tasks)
        # 撤回后该地区不再可用。
        availability = self.service.availability("work-001")
        self.assertEqual(availability["territories"], [])

    def test_授权到期扫描联动下架(self):
        sweep = self.service.sweep_expirations("2026-11-01T00:00:00+00:00")
        self.assertEqual(sweep["expired_licenses"], [])
        sweep = self.service.sweep_expirations("2027-01-01T00:00:00+00:00")
        self.assertEqual(sweep["expired_licenses"], ["lic-001"])
        self.assertIn("work-001", sweep["affected_works"])
        self.assertEqual(self.service.store.get_license("lic-001").state, "expired")

    def test_授权只覆盖部分地区时不得超范围发布(self):
        self.service.add_license({
            "license_id": "lic-002", "fingerprint": "fp-itv",
            "holder_id": "master-zhang", "creator_id": "creator-li",
            "territories": ["JP"], "valid_from": "2026-01-01T00:00:00+00:00",
            "declared_terms": "同 lic-001",
        })
        self.service.attach_license({"material_id": "itv-001",
                                     "license_id": "lic-002"})
        # 仅 SG 授权已随 lic-001 撤回 → 超范围发布被拒。
        self.service.revoke_license("lic-001")
        with self.assertRaises(ServiceError):
            self.service.publish_work({
                "work_id": "work-draft", "published_by": "editorial-board",
                "territories": ["SG"], "published_at": T,
            })
        # JP 在授权地区交集内可发布（授权期限窗口可追溯）。
        published = self.service.publish_work({
            "work_id": "work-draft", "published_by": "editorial-board",
            "territories": ["JP"], "published_at": T,
        })
        self.assertEqual(published["territories"], ["JP"])

    def test_创作者不能关闭自己的争议(self):
        self.service.open_dispute({
            "dispute_id": "d-001", "material_id": "clip-001",
            "opener_id": "viewer-zhao", "reason": "疑似断章取义",
        })
        with self.assertRaises(ServiceError):
            self.service.close_dispute({
                "dispute_id": "d-001", "closer_id": "creator-li",
                "resolution": "没问题",
            })
        # 争议未决时作品不能发布。
        with self.assertRaises(ServiceError):
            self.service.publish_work({
                "work_id": "work-draft", "published_by": "editorial-board",
                "territories": ["CN"], "published_at": T,
            })
        closed = self.service.close_dispute({
            "dispute_id": "d-001", "closer_id": "reviewer-chen",
            "resolution": "独立复核后维持原处理",
        })
        self.assertEqual(closed["state"], "closed")


class 溯源与可用性测试(unittest.TestCase):
    def test_trace_展示来源加工许可与待办(self):
        service = build_service()
        service.add_claim({
            "claim_id": "c-1", "material_id": "itv-001",
            "subject": "工序", "content": "浸泡三次",
            "source_id": "master-zhang",
        })
        service.publish_work({
            "work_id": "work-001", "published_by": "editorial-board",
            "territories": ["CN", "SG"], "published_at": T,
        })
        traced = service.trace("clip-001")
        source_ids = [s["material"]["material_id"] for s in traced["sources"]]
        self.assertEqual(source_ids, ["itv-001"])
        work_ids = [w["material_id"] for w in traced["downstream_works"]]
        self.assertIn("work-001", work_ids)
        license_ids = [item["license_id"] for item in traced["licenses"]]
        self.assertEqual(license_ids, ["lic-001"])

        work_trace = service.trace("work-001")
        self.assertEqual(work_trace["availability"]["territories"], ["CN", "SG"])
        window = work_trace["availability"]["windows"]["CN"]
        self.assertEqual(window["valid_from"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(window["valid_until"], "2026-12-31T23:59:59+00:00")
        self.assertEqual(len(work_trace["publications"]), 1)
        self.assertEqual(work_trace["processing_chain"][0]["process"], "edit")


if __name__ == "__main__":
    unittest.main()
