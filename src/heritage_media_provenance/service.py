"""非遗影像来源与许可的应用服务。

服务把业务规则集中在一处，核心规则与场景诉求一一对应：

- 同一素材指纹再次导入时复用已有处理；指纹一致但许可声明不同，
  转独立复核人员，不自动覆盖；
- 佐证材料共同支撑结论；矛盾来源必须同时保留分歧；
- 传承人更正事实后，未发布依赖作品停止流转，已公开作品形成更正记录
  并重新判断传播范围；
- 每个公开版本冻结发布当时采用的证据与许可；
- 创作者本人不能关闭自己的争议；
- 许可撤回/到期后找全受影响作品，延续未完成的复核；
- 任一片段都可生成溯源报告：来自哪里、经历过哪些加工、
  在哪些地区与期限可用、当前还有哪些必须处理的工作。
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date

from . import domain as D
from .domain import (
    Authorization, Correction, Dispute, EvidenceLink, Material, Record,
    Relation, ReviewTask, Reviewer, Version, Work,
)
from .store import Store


class ServiceError(ValueError):
    """业务规则冲突。"""


class Service:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # ------------------------------------------------------------------
    # 基础能力（保持兼容）
    # ------------------------------------------------------------------
    def health(self) -> dict[str, str]:
        return {"service": "heritage_media_provenance", "status": "ok"}

    def register(self, payload: dict[str, object]) -> dict[str, object]:
        required = ("record_id", "owner_id", "state")
        missing = [name for name in required if not str(payload.get(name, "")).strip()]
        if missing:
            raise ValueError("缺少必要字段：" + "、".join(missing))
        record = Record(
            record_id=str(payload["record_id"]), owner_id=str(payload["owner_id"]),
            state=str(payload["state"]), revision=int(payload.get("revision", 1)),
        )
        return asdict(self.store.add(record))

    def find(self, record_id: str) -> dict[str, object] | None:
        value = self.store.get(record_id)
        return asdict(value) if value else None

    # ------------------------------------------------------------------
    # 内部小工具
    # ------------------------------------------------------------------
    def _event(self, event_type: str, subject_id: str, detail: str = "") -> None:
        self.store.add_event(D.Event(event_type=event_type, subject_id=subject_id, detail=detail,
                                     created_at=D.now()))

    @staticmethod
    def _as_tuple(value, name: str) -> tuple[str, ...]:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, (list, tuple)):
            raise ServiceError(f"{name} 必须是地区列表")
        return tuple(str(part) for part in value)

    def _require_material(self, material_id: str) -> Material:
        material = self.store.get_material(material_id)
        if material is None:
            raise ServiceError(f"素材不存在：{material_id}")
        return material

    @staticmethod
    def _material_dict(material: Material) -> dict:
        return asdict(material)

    # ------------------------------------------------------------------
    # 素材登记、关系与指纹复用
    # ------------------------------------------------------------------
    def register_material(self, payload: dict) -> dict:
        material_id = str(payload.get("material_id", "")).strip()
        material_type = str(payload.get("material_type", "")).strip()
        if not material_id:
            raise ServiceError("缺少必要字段：material_id")
        if material_type not in D.MATERIAL_TYPES:
            raise ServiceError(f"未知素材类型：{material_type}")
        if self.store.get_material(material_id) is not None:
            raise ServiceError(f"素材编号已存在：{material_id}")

        fingerprint = D.normalize_fingerprint(payload["fingerprint"]) if payload.get("fingerprint") else ""
        material = Material(
            material_id=material_id, material_type=material_type,
            title=str(payload.get("title", "")),
            fingerprint=fingerprint,
            owner_id=str(payload.get("owner_id", "")),
            holder_id=str(payload.get("holder_id", "")),
            content_note=str(payload.get("content_note", "")),
        )
        self.store.add_material(material)

        reused_from = ""
        mismatched_license = None
        if fingerprint:
            prior = self.store.find_by_fingerprint(fingerprint)
            prior = [item for item in prior if item.material_id != material_id]
            if prior:
                # 同一素材指纹再次导入：复用已有处理，不新建加工/复核流程。
                reused_from = prior[0].material_id
                self._event(D.EVENT_FINGERPRINT_REUSE, material_id,
                            f"fingerprint={fingerprint}; reused_from={reused_from}")
                # 指纹一致但许可声明不同：交给独立人员复核。
                new_declaration = str(payload.get("license_declaration", "")).strip()
                if new_declaration:
                    prior_declarations = {
                        lic.declaration
                        for source in prior
                        for lic in self.store.authorizations_for(source.material_id)
                        if lic.declaration
                    }
                    if prior_declarations and new_declaration not in prior_declarations:
                        mismatched_license = {
                            "fingerprint": fingerprint,
                            "existing_material_id": reused_from,
                            "incoming_material_id": material_id,
                            "declaration_incoming": new_declaration,
                            "declarations_existing": sorted(prior_declarations),
                        }
                        task = self.store.add_task(ReviewTask(
                            task_id=f"task-mismatch-{material_id}",
                            task_type=D.TASK_FINGERPRINT_MISMATCH,
                            subject_id=material_id,
                            reason=(f"指纹 {fingerprint} 与 {reused_from} 一致，"
                                    f"但许可声明不同：{new_declaration}"),
                        ))
                        self._event(D.EVENT_MISMATCH_FLAGGED, material_id, task.task_id)

        result = self._material_dict(self.store.get_material(material_id))  # type: ignore[arg-type]
        result["reused_from"] = reused_from
        result["license_declaration_mismatch"] = mismatched_license
        return result

    def add_relation(self, payload: dict) -> dict:
        source_id = str(payload.get("source_id", ""))
        target_id = str(payload.get("target_id", ""))
        relation = str(payload.get("relation", ""))
        if not source_id or not target_id:
            raise ServiceError("关系必须包含 source_id 与 target_id")
        if relation not in D.RELATION_TYPES:
            raise ServiceError(f"未知关系类型：{relation}")
        self._require_material(source_id)
        self._require_material(target_id)
        saved = self.store.add_relation(Relation(
            source_id=source_id, target_id=target_id, relation=relation,
            detail=str(payload.get("detail", "")),
        ))
        return asdict(saved)

    def list_graph(self) -> dict:
        materials = self.store.list_materials()
        relations: list = []
        for material in materials:
            relations.extend(self.store.relations_from(material.material_id))
        return {
            "materials": [asdict(item) for item in materials],
            "relations": [asdict(item) for item in relations],
        }

    # ------------------------------------------------------------------
    # 结论与证据：佐证共同支撑，矛盾必须保留
    # ------------------------------------------------------------------
    def add_conclusion(self, payload: dict) -> dict:
        conclusion_id = str(payload.get("conclusion_id", "")).strip()
        if not conclusion_id:
            raise ServiceError("缺少必要字段：conclusion_id")
        if self.store.get_conclusion(conclusion_id) is not None:
            raise ServiceError(f"结论已存在：{conclusion_id}")
        self.store.add_conclusion(
            conclusion_id, str(payload.get("subject_id", "")),
            str(payload.get("statement", "")), D.now(),
        )
        return {"conclusion_id": conclusion_id}

    def add_evidence(self, payload: dict) -> dict:
        conclusion_id = str(payload.get("conclusion_id", ""))
        material_id = str(payload.get("material_id", ""))
        stance = str(payload.get("stance", ""))
        if self.store.get_conclusion(conclusion_id) is None:
            raise ServiceError(f"结论不存在：{conclusion_id}")
        self._require_material(material_id)
        if stance not in D.STANCES:
            raise ServiceError(f"立场必须是 {sorted(D.STANCES)}")
        link = self.store.add_evidence(EvidenceLink(
            conclusion_id=conclusion_id, material_id=material_id, stance=stance,
            detail=str(payload.get("detail", "")),
        ))
        return self.evidence_report(conclusion_id)

    def evidence_report(self, conclusion_id: str) -> dict:
        """同一结论下的佐证与分歧；两种立场都返回，分歧不许删除。"""
        if self.store.get_conclusion(conclusion_id) is None:
            raise ServiceError(f"结论不存在：{conclusion_id}")
        links = self.store.evidence_for(conclusion_id)
        supports = [asdict(link) for link in links if link.stance == D.STANCE_SUPPORTS]
        contradicts = [asdict(link) for link in links if link.stance == D.STANCE_CONTRADICTS]
        return {
            "conclusion_id": conclusion_id,
            "supports": supports,
            "contradictions": contradicts,
            "has_unresolved_contradiction": bool(contradicts),
        }

    # ------------------------------------------------------------------
    # 传承人授权（地区与期限）
    # ------------------------------------------------------------------
    def register_authorization(self, payload: dict) -> dict:
        license_id = str(payload.get("license_id", "")).strip()
        holder_id = str(payload.get("holder_id", "")).strip()
        material_id = str(payload.get("material_id", "")).strip()
        if not license_id or not holder_id or not material_id:
            raise ServiceError("授权必须包含 license_id、holder_id、material_id")
        self._require_material(material_id)
        if self.store.get_authorization(license_id) is not None:
            raise ServiceError(f"授权编号已存在：{license_id}")
        valid_until = str(payload.get("valid_until", ""))
        territories = self._as_tuple(payload.get("territories", ()), "territories")
        license_ = Authorization(
            license_id=license_id, holder_id=holder_id, material_id=material_id,
            territories=territories,
            valid_from=str(payload.get("valid_from", "")),
            valid_until=valid_until,
            declaration=str(payload.get("declaration", "")),
        )
        saved = self.store.add_authorization(license_)
        # 登记即按当前日期判定是否已到期。
        result = asdict(saved)
        result["effective_today"] = self._license_effective(saved, self._today())
        return result

    @staticmethod
    def _today() -> date:
        return date.today()

    @staticmethod
    def _parse_date(value: str) -> date | None:
        if not value:
            return None
        return date.fromisoformat(value[:10])

    def _license_effective(self, license_: Authorization, today: date) -> bool:
        if license_.status != D.LICENSE_ACTIVE:
            return False
        start = self._parse_date(license_.valid_from)
        end = self._parse_date(license_.valid_until)
        if start and today < start:
            return False
        if end and today > end:
            return False
        return True

    def availability(self, material_id: str, today: date | None = None) -> dict:
        """素材在哪些地区、什么期限内可用（取上游授权的并集）。"""
        self._require_material(material_id)
        today = today or self._today()
        entries = []
        territories: set[str] = set()
        for lic in self.store.covering_authorizations(material_id):
            effective = self._license_effective(lic, today)
            if effective:
                territories.update(lic.territories)
            entries.append({
                "license_id": lic.license_id,
                "holder_id": lic.holder_id,
                "material_id": lic.material_id,
                "territories": list(lic.territories),
                "valid_from": lic.valid_from,
                "valid_until": lic.valid_until,
                "status": lic.status,
                "effective": effective,
            })
        return {
            "material_id": material_id,
            "as_of": today.isoformat(),
            "territories_available": sorted(territories),
            "licenses": entries,
        }

    # ------------------------------------------------------------------
    # 派生作品、公开版本（冻结证据与许可）
    # ------------------------------------------------------------------
    def register_work(self, payload: dict) -> dict:
        work_id = str(payload.get("work_id", "")).strip()
        creator_id = str(payload.get("creator_id", "")).strip()
        if not work_id or not creator_id:
            raise ServiceError("作品必须包含 work_id 与 creator_id")
        status = str(payload.get("status", D.WORK_DRAFT))
        if status not in D.WORK_STATUSES:
            raise ServiceError(f"未知作品状态：{status}")
        if self.store.get_work(work_id) is not None:
            raise ServiceError(f"作品编号已存在：{work_id}")
        # 作品同时作为图谱中的派生作品节点，授权与更正沿 uses/derives 边传播到它。
        if self.store.get_material(work_id) is None:
            self.store.add_material(Material(
                material_id=work_id, material_type=D.DERIVED_WORK,
                title=str(payload.get("title", "")), owner_id=creator_id,
            ))
        work = Work(
            work_id=work_id, creator_id=creator_id, status=status,
            region_scope=self._as_tuple(payload.get("region_scope", ()), "region_scope"),
        )
        return asdict(self.store.add_work(work))

    def publish_version(self, payload: dict) -> dict:
        """发布公开版本：冻结当时实际采用的证据与当时生效的许可。"""
        work_id = str(payload.get("work_id", ""))
        version_id = str(payload.get("version_id", ""))
        work = self.store.get_work(work_id)
        if work is None:
            raise ServiceError(f"作品不存在：{work_id}")
        if self.store.get_version(version_id) is not None:
            raise ServiceError(f"版本编号已存在：{version_id}")

        evidence_ids = self._as_tuple(payload.get("evidence_material_ids", ()), "evidence_material_ids")
        for material_id in evidence_ids:
            material = self.store.get_material(material_id)
            if material is None:
                raise ServiceError(f"证据素材不存在：{material_id}")
            if material.fact_status != D.FACT_ACTIVE:
                raise ServiceError(f"证据已被更正，不能冻结进公开版本：{material_id}")

        today = self._today()
        license_rows = []
        license_ids_seen: set[str] = set()
        covered_territories: set[str] = set()
        for material_id in evidence_ids:
            for lic in self.store.covering_authorizations(material_id):
                if lic.license_id in license_ids_seen:
                    continue
                license_ids_seen.add(lic.license_id)
                effective = self._license_effective(lic, today)
                row = {
                    "license_id": lic.license_id,
                    "holder_id": lic.holder_id,
                    "material_id": lic.material_id,
                    "territories": list(lic.territories),
                    "valid_from": lic.valid_from,
                    "valid_until": lic.valid_until,
                    "status": lic.status,
                    "frozen_effective": effective,
                }
                license_rows.append(row)
                if effective:
                    covered_territories.update(lic.territories)

        sequence = len(self.store.versions_for(work_id)) + 1
        version = Version(
            version_id=version_id, work_id=work_id, sequence=sequence,
            evidence_frozen=tuple(sorted(evidence_ids)),
            licenses_frozen=tuple(sorted(row["license_id"] for row in license_rows)),
            territories_frozen=tuple(sorted(covered_territories)),
        )
        saved = self.store.add_version(version)
        # 发布即进入公开状态，传播范围按当时授权覆盖地区冻结。
        self.store.update_work(work_id, D.WORK_PUBLISHED, tuple(sorted(covered_territories)))
        return {
            "version": asdict(saved),
            "licenses_frozen_detail": license_rows,
        }

    # ------------------------------------------------------------------
    # 传承人更正事实
    # ------------------------------------------------------------------
    def correct_fact(self, payload: dict) -> dict:
        """传承人更正事实。

        - 旧素材标记为 superseded（内容保留留痕）；
        - 尚未发布的依赖作品停止流转（draft → held）；
        - 已公开作品形成更正记录并重新判断传播范围。
        """
        old_id = str(payload.get("old_material_id", ""))
        new_id = str(payload.get("new_material_id", ""))
        holder_id = str(payload.get("holder_id", ""))
        old = self._require_material(old_id)
        new = self._require_material(new_id)
        if old.material_id == new.material_id:
            raise ServiceError("更正前后的素材不能相同")
        if holder_id and new.holder_id and holder_id != new.holder_id:
            raise ServiceError("只有传承人本人可以更正其口述事实")

        self.store.update_material_fact(old_id, D.FACT_SUPERSEDED, new_id)
        self._event(D.EVENT_CORRECTION, old_id, f"superseded_by={new_id}; by={holder_id}")

        # 延续：此前围绕旧事实、尚未完成的复核任务必须延续到新事实。
        carried = self._carry_pending_tasks(old_id, new_id, D.TASK_FACT_CORRECTION,
                                            f"事实更正 {old_id} → {new_id}")

        affected = sorted(self.store.descendant_ids(old_id))
        affected_works = [self.store.get_work(mid) for mid in affected]
        affected_works = [w for w in affected_works if w is not None]

        held_works: list[str] = []
        corrections: list[str] = []
        for work in affected_works:
            if work.status == D.WORK_PUBLISHED:
                corrections.append(self._record_correction_for_published(work, old_id, new_id,
                                                                          str(payload.get("note", ""))))
            else:
                # 尚未发布（draft 或已被停）：停止流转。
                self.store.update_work(work.work_id, D.WORK_HELD, work.region_scope)
                self.store.add_task(ReviewTask(
                    task_id=f"task-correct-{work.work_id}-{old_id}",
                    task_type=D.TASK_FACT_CORRECTION, subject_id=work.work_id,
                    reason=f"依赖的 {old_id} 已被传承人更正为 {new_id}，未发布作品停止流转",
                ))
                held_works.append(work.work_id)
                self._event(D.EVENT_HOLD, work.work_id, f"held_due_to={old_id}")

        return {
            "old_material_id": old_id,
            "new_material_id": new_id,
            "held_unpublished_works": held_works,
            "correction_ids": corrections,
            "carried_tasks": carried,
        }

    def _record_correction_for_published(self, work: Work, old_id: str, new_id: str, note: str) -> str:
        versions = self.store.versions_for(work.work_id)
        version = versions[-1]
        correction_id = f"corr-{work.work_id}-{version.version_id}-{old_id}"
        self.store.add_correction(Correction(
            correction_id=correction_id, work_id=work.work_id, version_id=version.version_id,
            old_material_id=old_id, new_material_id=new_id, note=note,
        ))
        self.store.add_task(ReviewTask(
            task_id=f"task-rejudge-{work.work_id}-{old_id}",
            task_type=D.TASK_REJUDGE_SCOPE, subject_id=work.work_id,
            reason=f"已公开作品所依据的 {old_id} 已更正，需形成更正记录并重新判断传播范围",
        ))
        return correction_id

    def rejudge_work_scope(self, work_id: str, today: date | None = None) -> dict:
        """更正记录形成后重新判断传播范围：以新事实链路上现行有效的授权为准。"""
        work = self.store.get_work(work_id)
        if work is None:
            raise ServiceError(f"作品不存在：{work_id}")
        today = today or self._today()
        versions = self.store.versions_for(work_id)
        if not versions:
            raise ServiceError(f"作品尚无公开版本：{work_id}")
        latest = versions[-1]

        # 以最新更正链路替换旧证据，再求当前有效授权覆盖的地区。
        effective_evidence: set[str] = set(latest.evidence_frozen)
        for corr in self.store.corrections_for(work_id):
            effective_evidence.discard(corr.old_material_id)
            new_material = self.store.get_material(corr.new_material_id)
            if new_material and new_material.fact_status == D.FACT_ACTIVE:
                effective_evidence.add(corr.new_material_id)

        license_ids: set[str] = set()
        territories: set[str] = set()
        blocked: list[str] = []
        for material_id in sorted(effective_evidence):
            material = self.store.get_material(material_id)
            if material is not None and material.fact_status != D.FACT_ACTIVE:
                blocked.append(f"证据 {material_id} 已被更正")
                continue
            for lic in self.store.covering_authorizations(material_id):
                if not self._license_effective(lic, today):
                    blocked.append(f"授权 {lic.license_id} 当前不可用（{lic.status or '超出期限'}）")
                    continue
                license_ids.add(lic.license_id)
                territories.update(lic.territories)

        new_scope = tuple(sorted(territories))
        self.store.update_work(work_id, D.WORK_PUBLISHED, new_scope)
        for corr in self.store.corrections_for(work_id):
            self.store.mark_correction_rejudged(corr.correction_id)
        for task in self.store.pending_tasks(work_id, D.TASK_REJUDGE_SCOPE):
            self.store.complete_task(task.task_id, D.now())
        self._event(D.EVENT_REJUDGE, work_id,
                    f"scope={list(new_scope)}; removed={sorted(set(work.region_scope) - set(new_scope))}")
        return {
            "work_id": work_id,
            "previous_scope": list(work.region_scope),
            "rejudged_scope": list(new_scope),
            "removed_territories": sorted(set(work.region_scope) - set(new_scope)),
            "blocked_reasons": sorted(set(blocked)),
            "license_ids": sorted(license_ids),
        }

    # ------------------------------------------------------------------
    # 许可撤回 / 到期：找全受影响作品，延续未完成复核
    # ------------------------------------------------------------------
    def _license_impacted_works(self, license_: Authorization) -> list[Work]:
        roots = {license_.material_id}
        roots.update(self.store.descendant_ids(license_.material_id))
        works = []
        for work in self.store.list_works():
            if work.work_id in roots:
                works.append(work)
                continue
            # 冻结在公开版本里的许可同样受影响——许可历史状态改变也要追溯。
            for version in self.store.versions_for(work.work_id):
                if license_.license_id in version.licenses_frozen:
                    works.append(work)
                    break
        return works

    def withdraw_authorization(self, payload: dict) -> dict:
        return self._change_authorization(payload, D.LICENSE_WITHDRAWN, D.EVENT_LICENSE_WITHDRAWN,
                                          D.TASK_LICENSE_WITHDRAWN, "授权撤回")

    def expire_authorizations(self, today: date | None = None) -> dict:
        """按日期把已到期但仍标记 active 的授权批量转为 expired。"""
        today = today or self._today()
        expired = []
        # 扫描全部授权：到期的许可即使只冻结在历史公开版本里也要找出来。
        rows = self.store.connection.execute("SELECT license_id FROM authorizations").fetchall()
        all_license_ids = [row["license_id"] for row in rows]
        impacted_works: set[str] = set()
        task_ids: list[str] = []
        for license_id in sorted(all_license_ids):
            lic = self.store.get_authorization(license_id)
            if lic is None or lic.status != D.LICENSE_ACTIVE:
                continue
            end = self._parse_date(lic.valid_until)
            if end is None or today <= end:
                continue
            result = self._change_authorization(
                {"license_id": license_id, "note": f"到期日 {lic.valid_until}"},
                D.LICENSE_EXPIRED, D.EVENT_LICENSE_EXPIRED, D.TASK_LICENSE_EXPIRED, "授权到期",
                today=today,
            )
            expired.append(license_id)
            impacted_works.update(result["impacted_works"])
            task_ids.extend(result["task_ids"])
        return {"as_of": today.isoformat(), "expired_licenses": expired,
                "impacted_works": sorted(impacted_works), "task_ids": task_ids}

    def _change_authorization(self, payload: dict, new_status: str, event_type: str,
                              task_type: str, label: str, today: date | None = None) -> dict:
        license_id = str(payload.get("license_id", ""))
        lic = self.store.get_authorization(license_id)
        if lic is None:
            raise ServiceError(f"授权不存在：{license_id}")
        today = today or self._today()
        self.store.update_authorization_status(license_id, new_status)
        note = str(payload.get("note", ""))
        self._event(event_type, license_id, f"{label}; material={lic.material_id}; {note}")

        # 找全受影响作品：授权素材的下游作品 + 冻结过该许可的公开版本。
        impacted = self._license_impacted_works(lic)
        task_ids: list[str] = []
        carried: list[str] = []
        for work in impacted:
            if work.status == D.WORK_PUBLISHED:
                # 已公开：形成更正/重判任务，重新判断传播范围。
                task = self.store.add_task(ReviewTask(
                    task_id=f"task-{new_status}-{work.work_id}-{license_id}",
                    task_type=D.TASK_REJUDGE_SCOPE, subject_id=work.work_id,
                    reason=f"{label}（{license_id}）影响已公开作品，需重新判断传播范围",
                ))
                task_ids.append(task.task_id)
            else:
                self.store.update_work(work.work_id, D.WORK_HELD, work.region_scope)
                task = self.store.add_task(ReviewTask(
                    task_id=f"task-{new_status}-{work.work_id}-{license_id}",
                    task_type=task_type, subject_id=work.work_id,
                    reason=f"{label}（{license_id}），未发布作品停止流转",
                ))
                task_ids.append(task.task_id)
                self._event(D.EVENT_HOLD, work.work_id, f"held_due_to_license={license_id}")
            # 延续该作品上尚未完成的旧复核，不许因许可状态变化而丢失。
            carried.extend(self._carry_pending_tasks(work.work_id, work.work_id, task_type,
                                                     f"{label}后延续未完成复核"))
        return {"license_id": license_id, "status": new_status,
                "impacted_works": sorted(work.work_id for work in impacted),
                "task_ids": task_ids, "carried_tasks": carried}

    def _carry_pending_tasks(self, old_subject: str, new_subject: str, task_type: str,
                             reason: str) -> list[str]:
        """把挂在旧对象上、尚未完成的复核任务延续到新对象。

        主体不变（例如许可撤回后作品仍是同一作品）时无需搬运——任务本身保持
        pending 即可被找全；只有主体迁移（旧事实→更正后的新事实）才生成延续任务。
        """
        if old_subject == new_subject:
            return []
        carried = []
        for pending in self.store.pending_tasks(old_subject):
            new_id = f"{pending.task_id}->carried:{new_subject}"
            if self.store.get_task(new_id) is not None:
                continue
            saved = self.store.add_task(ReviewTask(
                task_id=new_id, task_type=pending.task_type, subject_id=new_subject,
                reason=f"{reason}；原任务 {pending.task_id}（{pending.reason}）",
                assignee_id=pending.assignee_id, carried_from=pending.task_id,
            ))
            carried.append(saved.task_id)
            self._event(D.EVENT_TASK_CARRIED, new_subject,
                        f"from={pending.task_id}; to={saved.task_id}")
        return carried

    # ------------------------------------------------------------------
    # 指纹声明冲突：独立人员复核
    # ------------------------------------------------------------------
    def register_reviewer(self, payload: dict) -> dict:
        reviewer_id = str(payload.get("reviewer_id", "")).strip()
        if not reviewer_id:
            raise ServiceError("缺少必要字段：reviewer_id")
        saved = self.store.add_reviewer(Reviewer(
            reviewer_id=reviewer_id, identity=str(payload.get("identity", "person")),
        ))
        return asdict(saved)

    def assign_review(self, payload: dict) -> dict:
        task_id = str(payload.get("task_id", ""))
        reviewer_id = str(payload.get("reviewer_id", ""))
        task = self.store.get_task(task_id)
        reviewer = self.store.get_reviewer(reviewer_id)
        if task is None:
            raise ServiceError(f"复核任务不存在：{task_id}")
        if reviewer is None:
            raise ServiceError(f"复核人员未登记：{reviewer_id}")
        # 独立性：指纹/许可声明冲突必须交给与当事创作者无关的人员。
        subject = self.store.get_material(task.subject_id)
        if task.task_type == D.TASK_FINGERPRINT_MISMATCH and subject is not None:
            if reviewer.identity != "team" and reviewer_id == subject.owner_id:
                raise ServiceError("指纹/许可声明冲突必须由独立人员复核，不能指派给创作者本人")
        self.store.assign_task(task_id, reviewer_id)
        return asdict(self.store.get_task(task_id))

    def resolve_fingerprint_mismatch(self, payload: dict) -> dict:
        """独立人员对“指纹一致、许可声明不同”作出复核结论。"""
        task_id = str(payload.get("task_id", ""))
        reviewer_id = str(payload.get("reviewer_id", ""))
        decision = str(payload.get("decision", ""))  # accept_incoming | keep_existing
        task = self.store.get_task(task_id)
        if task is None or task.task_type != D.TASK_FINGERPRINT_MISMATCH:
            raise ServiceError(f"指纹声明冲突任务不存在：{task_id}")
        reviewer = self.store.get_reviewer(reviewer_id)
        if reviewer is None:
            raise ServiceError("必须由登记在案的独立人员复核")
        subject = self.store.get_material(task.subject_id)
        if subject is not None and reviewer.identity != "team" and reviewer_id == subject.owner_id:
            raise ServiceError("创作者本人不能复核自己的指纹/许可声明冲突")
        if decision not in {"accept_incoming", "keep_existing"}:
            raise ServiceError("decision 必须是 accept_incoming 或 keep_existing")
        self.store.complete_task(task_id, D.now())
        return {"task_id": task_id, "resolved_by": reviewer_id, "decision": decision}

    # ------------------------------------------------------------------
    # 争议：创作者本人不能关闭自己的争议
    # ------------------------------------------------------------------
    def open_dispute(self, payload: dict) -> dict:
        dispute_id = str(payload.get("dispute_id", "")).strip()
        subject_id = str(payload.get("subject_id", "")).strip()
        opened_by = str(payload.get("opened_by", "")).strip()
        if not dispute_id or not subject_id or not opened_by:
            raise ServiceError("争议必须包含 dispute_id、subject_id、opened_by")
        dispute = self.store.add_dispute(Dispute(
            dispute_id=dispute_id, subject_id=subject_id, opened_by=opened_by,
            reason=str(payload.get("reason", "")),
        ))
        self._event(D.EVENT_DISPUTE_OPENED, dispute_id, f"subject={subject_id}; by={opened_by}")
        return asdict(dispute)

    def close_dispute(self, payload: dict) -> dict:
        dispute_id = str(payload.get("dispute_id", ""))
        closed_by = str(payload.get("closed_by", ""))
        dispute = self.store.get_dispute(dispute_id)
        if dispute is None:
            raise ServiceError(f"争议不存在：{dispute_id}")
        if dispute.status == D.DISPUTE_CLOSED and closed_by != self._creator_of(dispute.subject_id):
            return asdict(dispute)
        # 创作者本人不能关闭自己的争议（即使争议已关闭，也无权执行关闭动作）。
        creator_id = self._creator_of(dispute.subject_id)
        if creator_id and closed_by == creator_id:
            self._event(D.EVENT_DISPUTE_CLOSE_REJECTED, dispute_id,
                        f"rejected_closer={closed_by}; reason=creator_self_close")
            raise ServiceError("创作者本人不能关闭自己的争议，须由独立人员处理")
        if dispute.status == D.DISPUTE_CLOSED:
            return asdict(dispute)
        self.store.close_dispute(dispute_id, closed_by, D.now())
        self._event(D.EVENT_DISPUTE_CLOSED, dispute_id, f"closed_by={closed_by}")
        return asdict(self.store.get_dispute(dispute_id))

    def _creator_of(self, subject_id: str) -> str:
        material = self.store.get_material(subject_id)
        if material is not None:
            return material.owner_id
        work = self.store.get_work(subject_id)
        return work.creator_id if work is not None else ""

    # ------------------------------------------------------------------
    # 待处理工作
    # ------------------------------------------------------------------
    def pending_review_tasks(self, subject_id: str | None = None) -> dict:
        tasks = self.store.pending_tasks(subject_id)
        return {"count": len(tasks), "tasks": [asdict(task) for task in tasks]}

    def complete_review_task(self, payload: dict) -> dict:
        task_id = str(payload.get("task_id", ""))
        task = self.store.get_task(task_id)
        if task is None:
            raise ServiceError(f"复核任务不存在：{task_id}")
        self.store.complete_task(task_id, D.now())
        return asdict(self.store.get_task(task_id))

    # ------------------------------------------------------------------
    # 片段溯源报告：来自哪里 / 经历过哪些加工 / 在哪可用 / 还要处理什么
    # ------------------------------------------------------------------
    def trace(self, fragment_id: str, today: date | None = None) -> dict:
        today = today or self._today()
        # 作品同时在素材图中登记为 derived_work 节点；先按作品识别。
        work = self.store.get_work(fragment_id)
        material = None if work is not None else self.store.get_material(fragment_id)
        if material is None and work is None:
            raise ServiceError(f"未找到片段或作品：{fragment_id}")

        upstream_edges = self.store.lineage(fragment_id, "up")
        downstream_edges = self.store.lineage(fragment_id, "down")

        def edge_dict(edge: Relation) -> dict:
            return {"source_id": edge.source_id, "target_id": edge.target_id,
                    "relation": edge.relation, "detail": edge.detail}

        node_ids = {fragment_id}
        for edge in upstream_edges:
            node_ids.add(edge.source_id)
        for edge in downstream_edges:
            node_ids.add(edge.target_id)
        nodes = {mid: self._material_dict(self.store.get_material(mid))  # type: ignore[arg-type]
                 for mid in node_ids if self.store.get_material(mid) is not None}

        # 源头：在上游链中作为来源出现、却不作为任何上游边目标的素材。
        upstream_sources = {e.source_id for e in upstream_edges}
        upstream_targets = {e.target_id for e in upstream_edges}
        root_ids = upstream_sources - upstream_targets
        if not upstream_edges:
            root_ids = {fragment_id} if material is not None else set()

        availability = self.availability(fragment_id, today) if material is not None else None

        versions = []
        corrections = []
        if work is not None:
            versions = [asdict(v) for v in self.store.versions_for(fragment_id)]
            corrections = [asdict(c) for c in self.store.corrections_for(fragment_id)]

        pending = [asdict(t) for t in self.store.pending_tasks(fragment_id)]
        # 任一片段都必须能看到下游作品上尚未处理的工作。
        downstream_ids = {e.target_id for e in downstream_edges}
        downstream_pending = []
        for node_id in sorted(downstream_ids):
            for task in self.store.pending_tasks(node_id):
                row = asdict(task)
                row["on_downstream"] = node_id
                downstream_pending.append(row)
        # 片段上游事实若已被更正，也属于当前必须处理的事项。
        supersession_chain = []
        if material is not None:
            current = material
            seen = set()
            while current and current.fact_status == D.FACT_SUPERSEDED and current.supersedes \
                    and current.supersedes not in seen:
                seen.add(current.material_id)
                newer = self.store.get_material(current.supersedes)
                supersession_chain.append({
                    "old_material_id": current.material_id,
                    "new_material_id": current.supersedes,
                    "new_exists": newer is not None,
                })
                current = newer  # type: ignore[assignment]

        return {
            "fragment_id": fragment_id,
            "kind": "material" if material is not None else "work",
            "record": self._material_dict(material) if material is not None else asdict(work),
            "origin": {
                "upstream_edges": [edge_dict(e) for e in upstream_edges],
                "root_material_ids": sorted(root_ids),
            },
            "processing": {
                "upstream_processing_chain": [edge_dict(e) for e in upstream_edges],
                "downstream_edges": [edge_dict(e) for e in downstream_edges],
            },
            "nodes": nodes,
            "availability": availability,
            "public_versions": versions,
            "corrections": corrections,
            "supersession_chain": supersession_chain,
            "pending_review_tasks": pending,
            "downstream_pending_tasks": downstream_pending,
            "must_handle_count": len(pending) + len(downstream_pending),
        }
