"""非遗影像来源与许可的应用服务。

规则要点：

1. 素材按指纹登记，同一指纹再次导入直接复用已有处理结果。
2. 指纹一致但许可声明不同，自动生成独立复核任务，创作者不能自审。
3. 说法之间可互相佐证（corroborate）或矛盾（contradict）；矛盾必须保留分歧，
   存在未解决矛盾的作品不能发布。
4. 传承人更正说法后：未发布的依赖作品停止流转（blocked）；已公开作品形成
   更正记录、状态转 corrected 并重新判断传播范围；两类作品都进入待处理清单。
5. 每次发布冻结当时采用的证据（说法）与许可（授权），版本不可变。
6. 授权撤回或到期：找全受影响作品（含冻结版本引用该授权者），延续未完成
   的复核任务，已发布作品按剩余授权重判地区与期限。
7. 创作者本人不能关闭自己作品的争议。
"""
from __future__ import annotations

import json
from typing import Any

from .domain import (
    MATERIAL_TYPES,
    Claim,
    ClaimRelation,
    Derivation,
    Dispute,
    LicenseGrant,
    Material,
    Publication,
    Record,
    ReviewTask,
    now_iso,
)
from .store import Store


class ServiceError(ValueError):
    """业务规则错误（输入不合法或当前状态不允许该操作）。"""


def _require(payload: dict[str, Any], *names: str) -> None:
    missing = [name for name in names if str(payload.get(name, "")).strip() == ""]
    if missing:
        raise ServiceError("缺少必要字段：" + "、".join(missing))


def _territories(payload: dict[str, Any], key: str = "territories") -> tuple[str, ...]:
    value = payload.get(key, ())
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    return tuple(dict.fromkeys(str(item) for item in value))


def _at(value: str = "") -> str:
    return value or now_iso()


def _license_active(grant: LicenseGrant, at: str) -> bool:
    if grant.state != "active":
        return False
    if grant.valid_from and at < grant.valid_from:
        return False
    if grant.valid_until and at > grant.valid_until:
        return False
    return True


class Service:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # -- 基线 -------------------------------------------------------------

    def health(self) -> dict[str, str]:
        return {"service": "heritage_media_provenance", "status": "ok"}

    def register(self, payload: dict[str, object]) -> dict[str, object]:
        _require(payload, "record_id", "owner_id", "state")  # type: ignore[arg-type]
        record = Record(
            record_id=str(payload["record_id"]),
            owner_id=str(payload["owner_id"]),
            state=str(payload["state"]),
            revision=int(payload.get("revision", 1)),  # type: ignore[arg-type]
        )
        return self.store.add(record).__dict__.copy()

    def find(self, record_id: str) -> dict[str, object] | None:
        value = self.store.get_record(record_id)
        return value.__dict__.copy() if value else None

    # -- 素材导入（指纹复用） ---------------------------------------------

    def import_material(self, payload: dict[str, Any]) -> dict[str, Any]:
        """登记素材；指纹已存在时复用已有素材，不重复建处理链路。"""
        _require(payload, "material_id", "fingerprint", "material_type")
        material_type = str(payload["material_type"])
        if material_type not in MATERIAL_TYPES:
            raise ServiceError(f"未知素材类型：{material_type}")
        fingerprint = str(payload["fingerprint"])

        existing = self.store.find_material_by_fingerprint(fingerprint)
        reused = existing is not None
        material = existing or self.store.add_material(
            Material(
                material_id=str(payload["material_id"]),
                material_type=material_type,
                fingerprint=fingerprint,
                title=str(payload.get("title", "")),
                creator_id=str(payload.get("creator_id", "")),
                holder_id=str(payload.get("holder_id", "")),
                state=str(payload.get("state", "collected")),
                metadata=payload.get("metadata") or {},
            )
        )
        result: dict[str, Any] = {
            "material": self._material_dict(material),
            "reused": reused,
            "review_required": False,
        }
        review = self._ensure_license_conflict_review(fingerprint, material.material_id)
        if review:
            result["review_required"] = True
            result["review_task_id"] = review.task_id
        return result

    def add_derivation(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "parent_id", "child_id", "process")
        if not self.store.get_material(str(payload["parent_id"])):
            raise ServiceError("父素材不存在：" + str(payload["parent_id"]))
        if not self.store.get_material(str(payload["child_id"])):
            raise ServiceError("子素材不存在：" + str(payload["child_id"]))
        derivation = self.store.add_derivation(
            Derivation(
                parent_id=str(payload["parent_id"]),
                child_id=str(payload["child_id"]),
                process=str(payload["process"]),
                actor_id=str(payload.get("actor_id", "")),
                note=str(payload.get("note", "")),
            )
        )
        return self._derivation_dict(derivation)

    # -- 说法与佐证/矛盾 --------------------------------------------------

    def add_claim(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "claim_id", "material_id", "subject", "content")
        if not self.store.get_material(str(payload["material_id"])):
            raise ServiceError("素材不存在：" + str(payload["material_id"]))
        claim = self.store.add_claim(
            Claim(
                claim_id=str(payload["claim_id"]),
                material_id=str(payload["material_id"]),
                subject=str(payload["subject"]),
                content=str(payload["content"]),
                source_id=str(payload.get("source_id", "")),
                polarity=str(payload.get("polarity", "positive")),
                state="active",
            )
        )
        return self._claim_dict(claim)

    def relate_claims(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "from_claim_id", "to_claim_id", "relation")
        relation = str(payload["relation"])
        if relation not in ("corroborate", "contradict"):
            raise ServiceError("关系只能是 corroborate 或 contradict")
        if not self.store.get_claim(str(payload["from_claim_id"])):
            raise ServiceError("说法不存在：" + str(payload["from_claim_id"]))
        if not self.store.get_claim(str(payload["to_claim_id"])):
            raise ServiceError("说法不存在：" + str(payload["to_claim_id"]))
        value = self.store.add_claim_relation(
            ClaimRelation(
                from_claim_id=str(payload["from_claim_id"]),
                to_claim_id=str(payload["to_claim_id"]),
                relation=relation,
                note=str(payload.get("note", "")),
            )
        )
        return self._relation_dict(value)

    # -- 授权（地区 × 期限） ----------------------------------------------

    def add_license(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "license_id", "fingerprint", "holder_id", "creator_id",
                 "valid_from")
        territories = _territories(payload)
        if not territories:
            raise ServiceError("授权至少覆盖一个地区")
        grant = self.store.add_license(
            LicenseGrant(
                license_id=str(payload["license_id"]),
                fingerprint=str(payload["fingerprint"]),
                holder_id=str(payload["holder_id"]),
                creator_id=str(payload["creator_id"]),
                territories=territories,
                valid_from=str(payload["valid_from"]),
                valid_until=str(payload.get("valid_until", "")),
                state="active",
                declared_terms=str(payload.get("declared_terms", "")),
            )
        )
        result: dict[str, Any] = {
            "license": self._license_dict(grant),
            "review_required": False,
        }
        # 指纹一致但许可声明不同 → 独立人员复核；已有未完成复核则延续。
        others = [
            item for item in self.store.list_licenses(grant.fingerprint)
            if item.license_id != grant.license_id
        ]
        conflict = any(item.declared_terms != grant.declared_terms for item in others)
        material = self.store.find_material_by_fingerprint(grant.fingerprint)
        material_id = material.material_id if material else ""
        if conflict:
            task = self._ensure_license_conflict_review(
                grant.fingerprint, material_id,
                reviewer_id=str(payload.get("reviewer_id", "")),
                creator_id=grant.creator_id,
            )
            result["review_required"] = True
            result["review_task_id"] = task.task_id if task else ""
        return result

    def attach_license(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "material_id", "license_id")
        material_id, license_id = str(payload["material_id"]), str(payload["license_id"])
        if not self.store.get_material(material_id):
            raise ServiceError("素材不存在：" + material_id)
        grant = self.store.get_license(license_id)
        if not grant:
            raise ServiceError("授权不存在：" + license_id)
        self.store.attach_license(material_id, license_id)
        return {"material_id": material_id, "license_id": license_id, "attached": True}

    def revoke_license(self, license_id: str, at: str = "") -> dict[str, Any]:
        """撤回授权：置 withdrawn，找全受影响作品并延续复核。"""
        grant = self.store.get_license(license_id)
        if not grant:
            raise ServiceError("授权不存在：" + license_id)
        self.store.set_license_state(license_id, "withdrawn")
        affected = self._works_using_license(license_id)
        tasks: list[str] = []
        for work_id in affected:
            work = self.store.get_material(work_id)
            if work and work.state == "published":
                self.store.set_material_state(work_id, "withdrawn")
            task = self._ensure_work_review(
                work_id, "license-withdrawn",
                f"授权 {license_id} 撤回，需复核传播范围",
            )
            tasks.append(task.task_id)
        return {
            "license_id": license_id,
            "state": "withdrawn",
            "affected_works": affected,
            "review_task_ids": tasks,
        }

    def sweep_expirations(self, at: str = "") -> dict[str, Any]:
        """把到期授权置 expired，并对受影响作品延续复核（可定时执行）。"""
        moment = _at(at)
        expired: list[str] = []
        affected: set[str] = set()
        tasks: list[str] = []
        for grant in self.store.list_licenses():
            if grant.state == "active" and grant.valid_until and moment > grant.valid_until:
                self.store.set_license_state(grant.license_id, "expired")
                expired.append(grant.license_id)
                for work_id in self._works_using_license(grant.license_id):
                    affected.add(work_id)
        for work_id in sorted(affected):
            work = self.store.get_material(work_id)
            if work and work.state == "published":
                self.store.set_material_state(work_id, "withdrawn")
            task = self._ensure_work_review(
                work_id, "license-expired",
                "授权到期，需复核传播范围",
            )
            tasks.append(task.task_id)
        return {"at": moment, "expired_licenses": expired,
                "affected_works": sorted(affected), "review_task_ids": tasks}

    # -- 争议（创作者不能自关） -------------------------------------------

    def open_dispute(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "dispute_id", "material_id", "opener_id")
        if not self.store.get_material(str(payload["material_id"])):
            raise ServiceError("素材不存在：" + str(payload["material_id"]))
        dispute = Dispute(
            dispute_id=str(payload["dispute_id"]),
            material_id=str(payload["material_id"]),
            opener_id=str(payload["opener_id"]),
            reason=str(payload.get("reason", "")),
            state="open",
        )
        self.store.add_dispute(dispute)
        # 争议中的说法标记 disputed，依赖作品进入复核视野。
        self._ensure_work_review(
            str(payload["material_id"]), "dispute-open",
            f"争议 {dispute.dispute_id} 待独立处理",
        )
        return self._dispute_dict(dispute)

    def close_dispute(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "dispute_id", "closer_id")
        dispute = self.store.get_dispute(str(payload["dispute_id"]))
        if not dispute:
            raise ServiceError("争议不存在：" + str(payload["dispute_id"]))
        if dispute.state == "closed":
            raise ServiceError("争议已关闭")
        closer = str(payload["closer_id"])
        material = self.store.get_material(dispute.material_id)
        # 创作者本人不能关闭自己作品的争议；争议发起者也不能自行关闭。
        if material and closer == material.creator_id:
            raise ServiceError("创作者本人不能关闭自己作品的争议")
        if closer == dispute.opener_id:
            raise ServiceError("争议发起者不能自行关闭争议")
        self.store.close_dispute(
            dispute.dispute_id, closer, str(payload.get("resolution", "")), now_iso()
        )
        return self._dispute_dict(self.store.get_dispute(dispute.dispute_id))

    # -- 传承人更正 -------------------------------------------------------

    def correct_claim(self, payload: dict[str, Any]) -> dict[str, Any]:
        """传承人更正说法：旧说法失效，下游未发布停转、已发布留更正记录并重判。"""
        _require(payload, "holder_id", "old_claim_id", "claim_id", "subject", "content")
        old = self.store.get_claim(str(payload["old_claim_id"]))
        if not old:
            raise ServiceError("被更正说法不存在：" + str(payload["old_claim_id"]))
        holder = str(payload["holder_id"])
        source_material = self.store.get_material(old.material_id)
        # 只有传承人本人可以更正其说法。
        if not source_material or source_material.holder_id != holder:
            raise ServiceError("只有该说法的传承人本人可以更正")
        if old.state == "corrected":
            raise ServiceError("该说法已被更正")

        new_claim = self.store.add_claim(
            Claim(
                claim_id=str(payload["claim_id"]),
                material_id=old.material_id,
                subject=str(payload["subject"]),
                content=str(payload["content"]),
                source_id=holder,
                polarity=str(payload.get("polarity", "positive")),
                state="active",
                supersedes=old.claim_id,
            )
        )
        self.store.mark_claim_superseded(old.claim_id, new_claim.claim_id)

        blocked_works: list[str] = []
        corrected_publications: list[dict[str, Any]] = []
        tasks: list[str] = []
        seen_works: set[str] = set()
        for node in self.store.descendants(old.material_id):
            work = self.store.get_material(node["node_id"])
            if not work or work.material_type != "work":
                continue
            if work.material_id in seen_works:
                continue
            seen_works.add(work.material_id)
            # 作品证据树确实使用了旧说法才受影响。
            evidence_ids = {c.claim_id for c in
                            self.store.claims_for_material_tree(work.material_id)}
            if old.claim_id not in evidence_ids:
                continue
            publication = self.store.latest_publication(work.material_id)
            if publication is None:
                # 尚未发布：停止流转。
                if work.state != "blocked":
                    self.store.set_material_state(work.material_id, "blocked")
                blocked_works.append(work.material_id)
                task = self._ensure_work_review(
                    work.material_id, "claim-corrected-blocked",
                    f"说法 {old.claim_id} 被传承人更正，未发布作品停止流转",
                )
            else:
                # 已经公开：形成更正记录并重新判断传播范围。
                self.store.set_material_state(work.material_id, "corrected")
                correction_id = f"corr-{work.material_id}-{publication.version}"
                self.store.add_correction(
                    correction_id, work.material_id, publication.publication_id,
                    old.claim_id, new_claim.claim_id,
                    str(payload.get("reason", "")), now_iso(),
                )
                availability = self.availability(work.material_id)
                corrected_publications.append({
                    "work_id": work.material_id,
                    "publication_id": publication.publication_id,
                    "correction_id": correction_id,
                    "reassessed_availability": availability,
                })
                task = self._ensure_work_review(
                    work.material_id, "claim-corrected-republish",
                    f"说法 {old.claim_id} 被更正，需发布更正版本或调整传播范围",
                )
            tasks.append(task.task_id)
        return {
            "old_claim_id": old.claim_id,
            "new_claim_id": new_claim.claim_id,
            "blocked_works": blocked_works,
            "corrected_publications": corrected_publications,
            "review_task_ids": tasks,
        }

    # -- 发布（冻结证据与许可） -------------------------------------------

    def publish_work(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "work_id", "published_by")
        work_id = str(payload["work_id"])
        work = self.store.get_material(work_id)
        if not work or work.material_type != "work":
            raise ServiceError("作品不存在：" + work_id)
        requested = _territories(payload)
        if not requested:
            raise ServiceError("发布至少指定一个地区")
        moment = _at(str(payload.get("published_at", "")))

        evidence = [c for c in self.store.claims_for_material_tree(work_id)
                    if c.state == "active"]
        contradictions = self._contradictions_among({c.claim_id for c in evidence})
        if contradictions:
            raise ServiceError(
                "证据中存在矛盾来源，必须保留分歧、不得共同支撑结论："
                + "；".join(f"{a}↔{b}" for a, b in contradictions)
            )
        open_disputes = [
            d.dispute_id for d in self.store.list_disputes("open")
            if d.material_id in {m for m in [work_id] +
                                 [n["node_id"] for n in self.store.ancestors(work_id)]}
        ]
        if open_disputes:
            raise ServiceError("存在未决争议，不能发布：" + "、".join(open_disputes))

        availability = self.availability(work_id, moment)
        usable = set(availability["territories"])
        beyond = [t for t in requested if t not in usable]
        if beyond:
            raise ServiceError(
                "以下地区没有覆盖发布时点的有效授权：" + "、".join(beyond)
            )

        license_ids = self._tree_license_ids(work_id)
        version = self.store.next_publication_version(work_id)
        publication = self.store.add_publication(Publication(
            publication_id=f"pub-{work_id}-v{version}",
            work_id=work_id,
            version=version,
            territories=tuple(requested),
            evidence_ids=tuple(c.claim_id for c in evidence),
            license_ids=tuple(license_ids),
            published_by=str(payload["published_by"]),
            published_at=moment,
            correction_of=str(payload.get("correction_of", "")),
            note=str(payload.get("note", "")),
        ))
        self.store.set_material_state(work_id, "published")
        return self._publication_dict(publication)

    def list_publications(self, work_id: str = "") -> list[dict[str, Any]]:
        return [self._publication_dict(p)
                for p in self.store.list_publications(work_id)]

    # -- 传播范围（地区 × 期限） ------------------------------------------

    def availability(self, work_id: str, at: str = "") -> dict[str, Any]:
        """按作品证据树上每个素材指纹的有效授权，求地区交集与可用窗口。"""
        moment = _at(at)
        work = self.store.get_material(work_id)
        if not work:
            raise ServiceError("作品不存在：" + work_id)
        tree_ids = [work_id] + [n["node_id"] for n in self.store.ancestors(work_id)]
        # 授权随加工链向下继承：素材自身没有授权时，回溯其直接父素材的有效授权。
        # 每个非作品源节点都必须在授权链上解析出至少一条当前有效授权，否则整体
        # 不可用（如剪辑片段继承原始访谈“只覆盖部分地区”的授权）。
        def direct_grants(node_id: str) -> list[LicenseGrant]:
            node = self.store.get_material(node_id)
            if not node:
                return []
            return [
                g for g in self.store.licenses_for_material(node_id)
                if g.creator_id == work.creator_id and _license_active(g, moment)
                and g.fingerprint == node.fingerprint
            ]

        def effective_grants(node_id: str, _seen: set[str] | None = None
                             ) -> list[LicenseGrant]:
            _seen = _seen or set()
            direct = direct_grants(node_id)
            if direct:
                return direct
            parents = [n["node_id"] for n in self.store.ancestors(node_id)
                       if n["depth"] == 1 and n["node_id"] not in _seen]
            inherited: list[LicenseGrant] = []
            for parent_id in parents:
                _seen.add(parent_id)
                inherited.extend(effective_grants(parent_id, _seen))
            deduped: dict[str, LicenseGrant] = {}
            for grant in inherited:
                deduped.setdefault(grant.license_id, grant)
            return list(deduped.values())

        # 每个源节点（非作品）作为一个授权覆盖单元，作品节点自身不另持授权。
        coverage: dict[str, list[LicenseGrant]] = {}
        for node_id in tree_ids:
            material = self.store.get_material(node_id)
            if not material or material.material_type == "work":
                continue
            grants = effective_grants(node_id)
            if not grants:
                return {
                    "work_id": work_id, "work_state": work.state, "at": moment,
                    "territories": [], "windows": {},
                    "blocked_reason": f"源素材 {node_id}（指纹"
                                      f" {material.fingerprint}）沿加工链找不到"
                                      "当前有效的传承人授权",
                }
            coverage[node_id] = grants

        windows: dict[str, dict[str, Any]] = {}
        if coverage:
            common: set[str] | None = None
            for grants in coverage.values():
                territories: set[str] = set()
                for grant in grants:
                    territories.update(grant.territories)
                common = territories if common is None else common & territories
            for territory in sorted(common or set()):
                starts, ends, license_ids = [], [], []
                for grants in coverage.values():
                    covering = [g for g in grants if territory in g.territories]
                    starts.append(max(g.valid_from for g in covering))
                    ends.append(min(g.valid_until or "9999-12-31T23:59:59+00:00"
                                    for g in covering))
                    license_ids.extend(g.license_id for g in covering)
                end = min(ends)
                windows[territory] = {
                    "valid_from": max(starts),
                    "valid_until": "" if end.startswith("9999-") else end,
                    "license_ids": sorted(set(license_ids)),
                }
        return {
            "work_id": work_id,
            "work_state": work.state,
            "at": moment,
            "territories": sorted(windows),
            "windows": windows,
        }

    # -- 复核任务 ---------------------------------------------------------

    def list_review_tasks(self, status: str = "open") -> list[dict[str, Any]]:
        return [self._task_dict(t) for t in self.store.list_review_tasks(status)]

    def resolve_review_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "task_id", "closer_id")
        task_id = str(payload["task_id"])
        task = next((t for t in self.store.list_review_tasks("")
                     if t.task_id == task_id), None)
        if not task:
            raise ServiceError("复核任务不存在：" + task_id)
        if task.status == "closed":
            raise ServiceError("复核任务已关闭")
        closer = str(payload["closer_id"])
        task = next((t for t in self.store.list_review_tasks("")
                     if t.task_id == task_id), None)
        material = (self.store.get_material(task.material_id)
                    if task and task.material_id else None)
        if material and material.creator_id and closer == material.creator_id:
            raise ServiceError("指纹/许可冲突必须由独立人员复核，创作者不能自审")
        self.store.close_review_task(task_id, now_iso())
        closed = next(t for t in self.store.list_review_tasks("")
                      if t.task_id == task_id)
        return self._task_dict(closed)

    # -- 溯源查询 ---------------------------------------------------------

    def trace(self, material_id: str) -> dict[str, Any]:
        """任一片段：来自哪里、经历过哪些加工、在哪些地区/期限可用、待处理作品。"""
        material = self.store.get_material(material_id)
        if not material:
            raise ServiceError("素材不存在：" + material_id)

        # 加工链按“从源头到当前素材”的方向排列（祖先深度倒序）。
        chain_rows = sorted(
            self.store.ancestors(material_id),
            key=lambda node: (-node["depth"], node["created_at"]),
        )
        chain, chain_seen = [], set()
        for node in chain_rows:
            if node["node_id"] in chain_seen:
                continue
            chain_seen.add(node["node_id"])
            parent = self.store.get_material(node["node_id"])
            chain.append({
                "material": self._material_dict(parent) if parent else None,
                "process": node["process"],
                "actor_id": node["actor_id"],
                "note": node["note"],
                "depth": node["depth"],
            })

        descendants, seen_d = [], set()
        for node in self.store.descendants(material_id):
            if node["node_id"] in seen_d:
                continue
            seen_d.add(node["node_id"])
            child = self.store.get_material(node["node_id"])
            descendants.append({
                "material": self._material_dict(child) if child else None,
                "process": node["process"],
                "actor_id": node["actor_id"],
                "note": node["note"],
                "depth": node["depth"],
            })
        claims = self.store.claims_for_material_tree(material_id)
        claim_ids = [c.claim_id for c in claims]
        relations = self.store.list_claim_relations(claim_ids)

        licenses: list[LicenseGrant] = []
        for node_id in [material_id] + [n["node_id"] for n in
                                        self.store.ancestors(material_id)]:
            for grant in self.store.licenses_for_material(node_id):
                if grant.license_id not in {g.license_id for g in licenses}:
                    licenses.append(grant)

        publications = (self.store.list_publications(material_id)
                        if material.material_type == "work" else [])
        # 下游作品的公开版本也列出，便于追查片段流向。
        downstream_works = [d["material"] for d in descendants
                            if d["material"] and d["material"]["material_type"] == "work"]
        availability = (self.availability(material_id)
                        if material.material_type == "work" else None)

        pending_works = self._pending_works_for(material_id)
        return {
            "material": self._material_dict(material),
            "sources": list(reversed(chain)),
            "processing_chain": chain,
            "derivatives": descendants,
            "claims": [self._claim_dict(c) for c in claims],
            "claim_relations": [self._relation_dict(r) for r in relations],
            "contradictions": self._contradictions_among(set(claim_ids)),
            "licenses": [self._license_dict(g) for g in licenses],
            "availability": availability,
            "publications": [self._publication_dict(p) for p in publications],
            "corrections": self.store.list_corrections(material_id),
            "disputes": [
                self._dispute_dict(d) for d in self.store.list_disputes()
                if d.material_id in {material_id} | {n["node_id"] for n in
                                                     self.store.ancestors(material_id)}
            ],
            "downstream_works": downstream_works,
            "pending_works": pending_works,
            "open_review_tasks": [
                self._task_dict(t) for t in self.store.list_review_tasks("open")
                if t.material_id in {material_id}
                | {w["material_id"] for w in downstream_works}
                | (set(self._descendant_work_ids(material_id)))
            ],
        }

    def pending_works(self) -> list[dict[str, Any]]:
        """全局：当前必须处理的作品（停转/待更正/被撤回/挂起复核）。"""
        result: dict[str, dict[str, Any]] = {}
        open_tasks = self.store.list_review_tasks("open")
        for task in open_tasks:
            work_id = task.material_id
            work = self.store.get_material(work_id)
            if not work or work.material_type != "work":
                continue
            entry = result.setdefault(work_id, {
                "work_id": work_id, "state": work.state, "reasons": [],
                "task_ids": [],
            })
            entry["reasons"].append(task.reason)
            entry["task_ids"].append(task.task_id)
        for work in self.store.list_materials("work"):
            if work.state in ("blocked", "corrected", "withdrawn"):
                entry = result.setdefault(work.material_id, {
                    "work_id": work.material_id, "state": work.state,
                    "reasons": [], "task_ids": [],
                })
                entry["reasons"].append(f"work-state:{work.state}")
        return sorted(result.values(), key=lambda item: item["work_id"])

    # -- 内部辅助 ---------------------------------------------------------

    def _descendant_work_ids(self, material_id: str) -> list[str]:
        result: list[str] = []
        for node in self.store.descendants(material_id):
            work = self.store.get_material(node["node_id"])
            if work and work.material_type == "work":
                result.append(work.material_id)
        return result

    def _pending_works_for(self, material_id: str) -> list[dict[str, Any]]:
        work_ids = set(self._descendant_work_ids(material_id))
        material = self.store.get_material(material_id)
        if material and material.material_type == "work":
            work_ids.add(material_id)
        open_task_ids = {t.material_id for t in self.store.list_review_tasks("open")}
        result: list[dict[str, Any]] = []
        for work_id in sorted(work_ids):
            work = self.store.get_material(work_id)
            if not work:
                continue
            reasons: list[str] = []
            if work.state in ("blocked", "corrected", "withdrawn"):
                reasons.append(f"work-state:{work.state}")
            if work_id in open_task_ids:
                reasons.append("open-review")
            if reasons:
                result.append({"work_id": work_id, "state": work.state,
                               "reasons": reasons})
        return result

    def _tree_license_ids(self, work_id: str) -> list[str]:
        ids: set[str] = set()
        for node_id in [work_id] + [n["node_id"] for n in
                                    self.store.ancestors(work_id)]:
            ids.update(g.license_id for g in self.store.licenses_for_material(node_id))
        return sorted(ids)

    def _works_using_license(self, license_id: str) -> list[str]:
        """找全受授权影响的作品：树中采用该授权 + 冻结版本引用该授权。"""
        affected: set[str] = set()
        for work in self.store.list_materials("work"):
            if license_id in self._tree_license_ids(work.material_id):
                affected.add(work.material_id)
        for publication in self.store.list_publications():
            if license_id in publication.license_ids:
                affected.add(publication.work_id)
        return sorted(affected)

    def _contradictions_among(self, claim_ids: set[str]) -> list[tuple[str, str]]:
        result: set[tuple[str, str]] = set()
        for relation in self.store.list_claim_relations(claim_ids):
            if (relation.relation == "contradict"
                    and relation.from_claim_id in claim_ids
                    and relation.to_claim_id in claim_ids):
                result.add(tuple(sorted((relation.from_claim_id,
                                         relation.to_claim_id))))  # type: ignore[arg-type]
        return sorted(result)  # type: ignore[return-value]

    def _ensure_license_conflict_review(
        self, fingerprint: str, material_id: str,
        reviewer_id: str = "", creator_id: str = "",
    ) -> ReviewTask | None:
        grants = self.store.list_licenses(fingerprint)
        terms = {g.declared_terms for g in grants}
        if len(terms) <= 1:
            return None
        if reviewer_id and creator_id and reviewer_id == creator_id:
            raise ServiceError("许可声明冲突必须由独立人员复核，不能指派给创作者本人")
        task_id = f"review:license-conflict:{fingerprint}"
        task = ReviewTask(
            task_id=task_id,
            reason="同一素材指纹的许可声明不一致，需独立复核",
            material_id=material_id,
            fingerprint=fingerprint,
            assignee_id=reviewer_id,
        )
        self.store.add_review_task(task)  # 幂等：未完成复核自动延续
        return self.store.list_review_tasks("", fingerprint)[0]

    def _ensure_work_review(self, material_id: str, reason_key: str,
                            reason: str) -> ReviewTask:
        task_id = f"review:{reason_key}:{material_id}"
        material = self.store.get_material(material_id)
        fingerprint = material.fingerprint if material else ""
        self.store.add_review_task(ReviewTask(
            task_id=task_id, reason=reason, material_id=material_id,
            fingerprint=fingerprint,
        ))
        return next(t for t in self.store.list_review_tasks("")
                    if t.task_id == task_id)

    # -- 序列化 -----------------------------------------------------------

    @staticmethod
    def _material_dict(value: Material) -> dict[str, Any]:
        return {
            "material_id": value.material_id,
            "material_type": value.material_type,
            "fingerprint": value.fingerprint,
            "title": value.title,
            "creator_id": value.creator_id,
            "holder_id": value.holder_id,
            "state": value.state,
            "metadata": value.metadata,
            "created_at": value.created_at,
        }

    @staticmethod
    def _derivation_dict(value: Derivation) -> dict[str, Any]:
        return {
            "parent_id": value.parent_id, "child_id": value.child_id,
            "process": value.process, "actor_id": value.actor_id,
            "note": value.note, "created_at": value.created_at,
        }

    @staticmethod
    def _claim_dict(value: Claim) -> dict[str, Any]:
        return {
            "claim_id": value.claim_id, "material_id": value.material_id,
            "subject": value.subject, "content": value.content,
            "source_id": value.source_id, "polarity": value.polarity,
            "state": value.state, "supersedes": value.supersedes,
            "created_at": value.created_at,
        }

    @staticmethod
    def _relation_dict(value: ClaimRelation) -> dict[str, Any]:
        return {
            "from_claim_id": value.from_claim_id, "to_claim_id": value.to_claim_id,
            "relation": value.relation, "note": value.note,
            "created_at": value.created_at,
        }

    @staticmethod
    def _license_dict(value: LicenseGrant) -> dict[str, Any]:
        return {
            "license_id": value.license_id, "fingerprint": value.fingerprint,
            "holder_id": value.holder_id, "creator_id": value.creator_id,
            "territories": list(value.territories),
            "valid_from": value.valid_from, "valid_until": value.valid_until,
            "state": value.state, "declared_terms": value.declared_terms,
            "created_at": value.created_at,
        }

    @staticmethod
    def _dispute_dict(value: Dispute) -> dict[str, Any]:
        return {
            "dispute_id": value.dispute_id, "material_id": value.material_id,
            "opener_id": value.opener_id, "reason": value.reason,
            "state": value.state, "closer_id": value.closer_id,
            "resolution": value.resolution, "created_at": value.created_at,
            "closed_at": value.closed_at,
        }

    @staticmethod
    def _task_dict(value: ReviewTask) -> dict[str, Any]:
        return {
            "task_id": value.task_id, "reason": value.reason,
            "material_id": value.material_id, "fingerprint": value.fingerprint,
            "assignee_id": value.assignee_id, "status": value.status,
            "created_at": value.created_at, "closed_at": value.closed_at,
        }

    @staticmethod
    def _publication_dict(value: Publication) -> dict[str, Any]:
        return {
            "publication_id": value.publication_id, "work_id": value.work_id,
            "version": value.version, "territories": list(value.territories),
            "evidence_ids": list(value.evidence_ids),
            "license_ids": list(value.license_ids),
            "published_by": value.published_by, "published_at": value.published_at,
            "correction_of": value.correction_of, "note": value.note,
        }


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
