"""非遗影像来源与许可的 SQLite 存储。

除基线 ``records`` 表外，按来源图谱组织：

- materials：素材/授权/作品节点（按指纹复用）
- derivations：加工边（父 → 子），支持递归上下游遍历
- claims / claim_relations：说法与佐证/矛盾关系
- license_grants：传承人授权（地区 × 期限）
- material_licenses：素材实际采用的授权（多对多）
- disputes / review_tasks：争议与独立复核
- publications / publication_items：公开版本冻结的证据与许可
- corrections：更正记录（事实被更正后对已公开作品形成）
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .domain import (
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


def _loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision > 0),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS materials (
                material_id TEXT PRIMARY KEY,
                material_type TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                creator_id TEXT NOT NULL DEFAULT '',
                holder_id TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_materials_fingerprint
                ON materials(fingerprint);
            CREATE INDEX IF NOT EXISTS idx_materials_creator
                ON materials(creator_id);

            CREATE TABLE IF NOT EXISTS derivations (
                parent_id TEXT NOT NULL REFERENCES materials(material_id),
                child_id TEXT NOT NULL REFERENCES materials(material_id),
                process TEXT NOT NULL,
                actor_id TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (parent_id, child_id, process)
            );
            CREATE INDEX IF NOT EXISTS idx_deriv_child ON derivations(child_id);
            CREATE INDEX IF NOT EXISTS idx_deriv_parent ON derivations(parent_id);

            CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY,
                material_id TEXT NOT NULL REFERENCES materials(material_id),
                subject TEXT NOT NULL,
                content TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                polarity TEXT NOT NULL DEFAULT 'positive',
                state TEXT NOT NULL,
                supersedes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_claims_material ON claims(material_id);
            CREATE INDEX IF NOT EXISTS idx_claims_subject ON claims(subject);

            CREATE TABLE IF NOT EXISTS claim_relations (
                from_claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                to_claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                relation TEXT NOT NULL CHECK(relation IN ('corroborate','contradict')),
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (from_claim_id, to_claim_id)
            );

            CREATE TABLE IF NOT EXISTS license_grants (
                license_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                holder_id TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                territories TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_until TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                declared_terms TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_licenses_fingerprint
                ON license_grants(fingerprint);

            CREATE TABLE IF NOT EXISTS material_licenses (
                material_id TEXT NOT NULL REFERENCES materials(material_id),
                license_id TEXT NOT NULL REFERENCES license_grants(license_id),
                PRIMARY KEY (material_id, license_id)
            );

            CREATE TABLE IF NOT EXISTS disputes (
                dispute_id TEXT PRIMARY KEY,
                material_id TEXT NOT NULL REFERENCES materials(material_id),
                opener_id TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                closer_id TEXT NOT NULL DEFAULT '',
                resolution TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                closed_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS review_tasks (
                task_id TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                material_id TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT '',
                assignee_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                closed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON review_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_fingerprint ON review_tasks(fingerprint);

            CREATE TABLE IF NOT EXISTS publications (
                publication_id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL REFERENCES materials(material_id),
                version INTEGER NOT NULL CHECK(version > 0),
                territories TEXT NOT NULL,
                evidence_ids TEXT NOT NULL,
                license_ids TEXT NOT NULL,
                published_by TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL,
                correction_of TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                UNIQUE(work_id, version)
            );

            CREATE TABLE IF NOT EXISTS corrections (
                correction_id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL REFERENCES materials(material_id),
                publication_id TEXT NOT NULL REFERENCES publications(publication_id),
                old_claim_id TEXT NOT NULL,
                new_claim_id TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    # -- 基线 -------------------------------------------------------------

    def add(self, record: Record) -> Record:
        value = record.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO records(record_id, owner_id, state, revision, created_at)"
                " VALUES(?,?,?,?,?)",
                (value.record_id, value.owner_id, value.state, value.revision, value.created_at),
            )
        return value

    def get_record(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id, owner_id, state, revision, created_at FROM records"
            " WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    # -- 素材 -------------------------------------------------------------

    def add_material(self, material: Material) -> Material:
        value = material.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO materials(material_id, material_type, fingerprint, title,"
                " creator_id, holder_id, state, metadata, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    value.material_id, value.material_type, value.fingerprint, value.title,
                    value.creator_id, value.holder_id, value.state,
                    json.dumps(value.metadata, ensure_ascii=False), value.created_at,
                ),
            )
        return value

    def get_material(self, material_id: str) -> Material | None:
        row = self.connection.execute(
            "SELECT * FROM materials WHERE material_id=?", (material_id,)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["metadata"] = _loads(data.pop("metadata"), {})
        return Material(**data)

    def find_material_by_fingerprint(self, fingerprint: str) -> Material | None:
        row = self.connection.execute(
            "SELECT * FROM materials WHERE fingerprint=? ORDER BY created_at LIMIT 1",
            (fingerprint,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["metadata"] = _loads(data.pop("metadata"), {})
        return Material(**data)

    def list_materials(self, material_type: str = "") -> list[Material]:
        sql = "SELECT * FROM materials"
        params: tuple[Any, ...] = ()
        if material_type:
            sql += " WHERE material_type=?"
            params = (material_type,)
        sql += " ORDER BY created_at, material_id"
        result: list[Material] = []
        for row in self.connection.execute(sql, params):
            data = dict(row)
            data["metadata"] = _loads(data.pop("metadata"), {})
            result.append(Material(**data))
        return result

    def set_material_state(self, material_id: str, state: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE materials SET state=? WHERE material_id=?", (state, material_id)
            )

    # -- 加工边 -----------------------------------------------------------

    def add_derivation(self, derivation: Derivation) -> Derivation:
        value = derivation.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO derivations(parent_id, child_id, process, actor_id,"
                " note, created_at) VALUES(?,?,?,?,?,?)",
                (
                    value.parent_id, value.child_id, value.process, value.actor_id,
                    value.note, value.created_at,
                ),
            )
        return value

    def list_derivations(self) -> list[Derivation]:
        rows = self.connection.execute("SELECT * FROM derivations ORDER BY created_at")
        return [Derivation(**dict(r)) for r in rows]

    def _lineage(self, material_id: str, direction: str) -> list[sqlite3.Row]:
        """递归 CTE：direction='up' 找全部祖先，'down' 找全部后代。"""
        if direction == "up":
            first, second = "child_id", "parent_id"
        else:
            first, second = "parent_id", "child_id"
        sql = f"""
            WITH RECURSIVE walk(depth, node_id, via_id, process, actor_id, note, created_at) AS (
                SELECT 1, {second}, {first}, process, actor_id, note, created_at
                FROM derivations WHERE {first}=?
                UNION ALL
                SELECT w.depth + 1, d.{second}, d.{first}, d.process, d.actor_id, d.note, d.created_at
                FROM derivations d JOIN walk w ON d.{first} = w.node_id
            )
            SELECT * FROM walk ORDER BY depth, created_at
        """
        return list(self.connection.execute(sql, (material_id,)))

    def ancestors(self, material_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._lineage(material_id, "up")]

    def descendants(self, material_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._lineage(material_id, "down")]

    # -- 说法与关系 -------------------------------------------------------

    def add_claim(self, claim: Claim) -> Claim:
        value = claim.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO claims(claim_id, material_id, subject, content, source_id,"
                " polarity, state, supersedes, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    value.claim_id, value.material_id, value.subject, value.content,
                    value.source_id, value.polarity, value.state, value.supersedes,
                    value.created_at,
                ),
            )
        return value

    def get_claim(self, claim_id: str) -> Claim | None:
        row = self.connection.execute(
            "SELECT * FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        return Claim(**dict(row)) if row else None

    def list_claims(self, material_id: str = "") -> list[Claim]:
        if material_id:
            rows = self.connection.execute(
                "SELECT * FROM claims WHERE material_id=? ORDER BY created_at", (material_id,)
            )
        else:
            rows = self.connection.execute("SELECT * FROM claims ORDER BY created_at")
        return [Claim(**dict(r)) for r in rows]

    def claims_for_material_tree(self, material_id: str) -> list[Claim]:
        """该素材及其全部祖先上承载的说法（即作品实际依赖的证据）。"""
        ids = [material_id] + [row["node_id"] for row in self.ancestors(material_id)]
        placeholders = ",".join("?" * len(ids))
        rows = self.connection.execute(
            f"SELECT * FROM claims WHERE material_id IN ({placeholders}) ORDER BY created_at",
            ids,
        )
        return [Claim(**dict(r)) for r in rows]

    def set_claim_state(self, claim_id: str, state: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE claims SET state=? WHERE claim_id=?", (state, claim_id)
            )

    def mark_claim_superseded(self, old_claim_id: str, new_claim_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE claims SET state='corrected', supersedes=? WHERE claim_id=?",
                (new_claim_id, old_claim_id),
            )

    def add_claim_relation(self, relation: ClaimRelation) -> ClaimRelation:
        value = relation.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO claim_relations(from_claim_id, to_claim_id,"
                " relation, note, created_at) VALUES(?,?,?,?,?)",
                (
                    value.from_claim_id, value.to_claim_id, value.relation,
                    value.note, value.created_at,
                ),
            )
        return value

    def list_claim_relations(self, claim_ids: Iterable[str] = ()) -> list[ClaimRelation]:
        ids = tuple(claim_ids)
        if ids:
            placeholders = ",".join("?" * len(ids))
            rows = self.connection.execute(
                f"SELECT * FROM claim_relations WHERE from_claim_id IN ({placeholders})"
                f" OR to_claim_id IN ({placeholders}) ORDER BY created_at",
                ids + ids,
            )
        else:
            rows = self.connection.execute("SELECT * FROM claim_relations ORDER BY created_at")
        return [ClaimRelation(**dict(r)) for r in rows]

    # -- 授权 -------------------------------------------------------------

    def add_license(self, grant: LicenseGrant) -> LicenseGrant:
        value = grant.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO license_grants(license_id, fingerprint, holder_id, creator_id,"
                " territories, valid_from, valid_until, state, declared_terms, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    value.license_id, value.fingerprint, value.holder_id, value.creator_id,
                    json.dumps(list(value.territories), ensure_ascii=False),
                    value.valid_from, value.valid_until, value.state,
                    value.declared_terms, value.created_at,
                ),
            )
        return value

    def get_license(self, license_id: str) -> LicenseGrant | None:
        row = self.connection.execute(
            "SELECT * FROM license_grants WHERE license_id=?", (license_id,)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["territories"] = tuple(_loads(data["territories"], []))
        return LicenseGrant(**data)

    def list_licenses(self, fingerprint: str = "") -> list[LicenseGrant]:
        if fingerprint:
            rows = self.connection.execute(
                "SELECT * FROM license_grants WHERE fingerprint=? ORDER BY created_at",
                (fingerprint,),
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM license_grants ORDER BY created_at"
            )
        result: list[LicenseGrant] = []
        for row in rows:
            data = dict(row)
            data["territories"] = tuple(_loads(data["territories"], []))
            result.append(LicenseGrant(**data))
        return result

    def set_license_state(self, license_id: str, state: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE license_grants SET state=? WHERE license_id=?", (state, license_id)
            )

    def attach_license(self, material_id: str, license_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO material_licenses(material_id, license_id)"
                " VALUES(?,?)",
                (material_id, license_id),
            )

    def licenses_for_material(self, material_id: str) -> list[LicenseGrant]:
        rows = self.connection.execute(
            "SELECT lg.* FROM license_grants lg"
            " JOIN material_licenses ml ON ml.license_id = lg.license_id"
            " WHERE ml.material_id=? ORDER BY lg.created_at",
            (material_id,),
        )
        result: list[LicenseGrant] = []
        for row in rows:
            data = dict(row)
            data["territories"] = tuple(_loads(data["territories"], []))
            result.append(LicenseGrant(**data))
        return result

    # -- 争议 -------------------------------------------------------------

    def add_dispute(self, dispute: Dispute) -> Dispute:
        value = dispute.created_at or now_iso()
        with self.connection:
            self.connection.execute(
                "INSERT INTO disputes(dispute_id, material_id, opener_id, reason, state,"
                " closer_id, resolution, created_at, closed_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    dispute.dispute_id, dispute.material_id, dispute.opener_id,
                    dispute.reason, dispute.state, dispute.closer_id, dispute.resolution,
                    value, dispute.closed_at,
                ),
            )
        return dispute

    def get_dispute(self, dispute_id: str) -> Dispute | None:
        row = self.connection.execute(
            "SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)
        ).fetchone()
        return Dispute(**dict(row)) if row else None

    def list_disputes(self, state: str = "") -> list[Dispute]:
        if state:
            rows = self.connection.execute(
                "SELECT * FROM disputes WHERE state=? ORDER BY created_at", (state,)
            )
        else:
            rows = self.connection.execute("SELECT * FROM disputes ORDER BY created_at")
        return [Dispute(**dict(r)) for r in rows]

    def close_dispute(self, dispute_id: str, closer_id: str, resolution: str,
                      closed_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE disputes SET state='closed', closer_id=?, resolution=?, closed_at=?"
                " WHERE dispute_id=?",
                (closer_id, resolution, closed_at, dispute_id),
            )

    # -- 复核任务 ---------------------------------------------------------

    def add_review_task(self, task: ReviewTask) -> ReviewTask:
        value = task.stamped()
        with self.connection:
            # 幂等：同一任务已存在时不重复创建，仅补全此前缺失的素材/指派人，
            # 从而在授权变动后延续未完成的复核。
            self.connection.execute(
                "INSERT INTO review_tasks(task_id, reason, material_id,"
                " fingerprint, assignee_id, status, created_at, closed_at)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(task_id) DO UPDATE SET"
                " material_id = CASE WHEN review_tasks.material_id=''"
                "     THEN excluded.material_id ELSE review_tasks.material_id END,"
                " fingerprint = CASE WHEN review_tasks.fingerprint=''"
                "     THEN excluded.fingerprint ELSE review_tasks.fingerprint END,"
                " assignee_id = CASE WHEN review_tasks.assignee_id=''"
                "     THEN excluded.assignee_id ELSE review_tasks.assignee_id END",
                (
                    value.task_id, value.reason, value.material_id, value.fingerprint,
                    value.assignee_id, value.status, value.created_at, value.closed_at,
                ),
            )
        return value

    def list_review_tasks(self, status: str = "", fingerprint: str = "") -> list[ReviewTask]:
        sql = "SELECT * FROM review_tasks"
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if fingerprint:
            clauses.append("fingerprint=?")
            params.append(fingerprint)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        return [ReviewTask(**dict(r)) for r in self.connection.execute(sql, params)]

    def close_review_task(self, task_id: str, closed_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE review_tasks SET status='closed', closed_at=? WHERE task_id=?",
                (closed_at, task_id),
            )

    # -- 公开版本 ---------------------------------------------------------

    def add_publication(self, publication: Publication) -> Publication:
        with self.connection:
            self.connection.execute(
                "INSERT INTO publications(publication_id, work_id, version, territories,"
                " evidence_ids, license_ids, published_by, published_at, correction_of, note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    publication.publication_id, publication.work_id, publication.version,
                    json.dumps(list(publication.territories), ensure_ascii=False),
                    json.dumps(list(publication.evidence_ids), ensure_ascii=False),
                    json.dumps(list(publication.license_ids), ensure_ascii=False),
                    publication.published_by, publication.published_at,
                    publication.correction_of, publication.note,
                ),
            )
        return publication

    def get_publication(self, publication_id: str) -> Publication | None:
        row = self.connection.execute(
            "SELECT * FROM publications WHERE publication_id=?", (publication_id,)
        ).fetchone()
        return self._publication_from_row(row) if row else None

    def latest_publication(self, work_id: str) -> Publication | None:
        row = self.connection.execute(
            "SELECT * FROM publications WHERE work_id=? ORDER BY version DESC LIMIT 1",
            (work_id,),
        ).fetchone()
        return self._publication_from_row(row) if row else None

    def list_publications(self, work_id: str = "") -> list[Publication]:
        if work_id:
            rows = self.connection.execute(
                "SELECT * FROM publications WHERE work_id=? ORDER BY version", (work_id,)
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM publications ORDER BY work_id, version"
            )
        return [self._publication_from_row(r) for r in rows]

    @staticmethod
    def _publication_from_row(row: sqlite3.Row) -> Publication:
        data = dict(row)
        data["territories"] = tuple(_loads(data["territories"], []))
        data["evidence_ids"] = tuple(_loads(data["evidence_ids"], []))
        data["license_ids"] = tuple(_loads(data["license_ids"], []))
        return Publication(**data)

    def next_publication_version(self, work_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM publications WHERE work_id=?",
            (work_id,),
        ).fetchone()
        return int(row["v"]) + 1

    # -- 更正记录 ---------------------------------------------------------

    def add_correction(self, correction_id: str, work_id: str, publication_id: str,
                       old_claim_id: str, new_claim_id: str, reason: str,
                       created_at: str) -> None:
        with self.connection:
            # 同一作品版本可能因多次事件触发更正，幂等写入。
            self.connection.execute(
                "INSERT OR IGNORE INTO corrections(correction_id, work_id,"
                " publication_id, old_claim_id, new_claim_id, reason, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (correction_id, work_id, publication_id, old_claim_id, new_claim_id,
                 reason, created_at),
            )

    def list_corrections(self, work_id: str = "") -> list[dict[str, Any]]:
        if work_id:
            rows = self.connection.execute(
                "SELECT * FROM corrections WHERE work_id=? ORDER BY created_at", (work_id,)
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM corrections ORDER BY created_at"
            )
        return [dict(r) for r in rows]
