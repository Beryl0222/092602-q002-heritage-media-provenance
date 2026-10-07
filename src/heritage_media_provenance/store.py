"""非遗影像来源与许可的 SQLite 存储。

除早期 ``records`` 表外，存储维护一张可追溯图谱：

materials（素材节点）＋ relations（加工边）构成来源链；
authorizations 记录地区与期限受限的传承人授权；
evidence 保留佐证与矛盾两种立场；
works/versions/corrections 管理派生作品、冻结的公开版本与更正记录；
disputes/review_tasks/events 支撑争议、独立复核与只增审计日志。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .domain import (
    Authorization, Correction, Dispute, Event, EvidenceLink, Material,
    Record, Relation, ReviewTask, Reviewer, Version, Work,
)

_SCHEMA = """
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
    title TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    owner_id TEXT NOT NULL DEFAULT '',
    holder_id TEXT NOT NULL DEFAULT '',
    content_note TEXT NOT NULL DEFAULT '',
    fact_status TEXT NOT NULL DEFAULT 'active',
    supersedes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_materials_fingerprint ON materials(fingerprint);
CREATE TABLE IF NOT EXISTS relations (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_id);
CREATE TABLE IF NOT EXISTS conclusions (
    conclusion_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL DEFAULT '',
    statement TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    conclusion_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    stance TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (conclusion_id, material_id, stance)
);
CREATE TABLE IF NOT EXISTS authorizations (
    license_id TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    territories TEXT NOT NULL DEFAULT '[]',
    valid_from TEXT NOT NULL DEFAULT '',
    valid_until TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    declaration TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_material ON authorizations(material_id);
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    creator_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    region_scope TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    version_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    evidence_frozen TEXT NOT NULL DEFAULT '[]',
    licenses_frozen TEXT NOT NULL DEFAULT '[]',
    territories_frozen TEXT NOT NULL DEFAULT '[]',
    freeze_hash TEXT NOT NULL,
    published_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_versions_work ON versions(work_id);
CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    old_material_id TEXT NOT NULL,
    new_material_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    rejudged INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_work ON corrections(work_id);
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    opened_by TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    closed_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    closed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_disputes_subject ON disputes(subject_id);
CREATE TABLE IF NOT EXISTS reviewers (
    reviewer_id TEXT PRIMARY KEY,
    identity TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_tasks (
    task_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    assignee_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    carried_from TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON review_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_subject ON review_tasks(subject_id);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    subject_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def _loads(value: str) -> tuple:
    return tuple(json.loads(value or "[]"))


def _dumps(values) -> str:
    return json.dumps(list(values), ensure_ascii=False)


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    # ------------------------------------------------------------------
    # 早期基础记录（保持兼容）
    # ------------------------------------------------------------------
    def add(self, record: Record) -> Record:
        value = record.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO records(record_id, owner_id, state, revision, created_at) VALUES(?,?,?,?,?)",
                (value.record_id, value.owner_id, value.state, value.revision, value.created_at),
            )
        return value

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id, owner_id, state, revision, created_at FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    # ------------------------------------------------------------------
    # 素材与关系
    # ------------------------------------------------------------------
    def add_material(self, material: Material) -> Material:
        value = material.stamped()
        with self.connection:
            self.connection.execute(
                """INSERT INTO materials(material_id, material_type, title, fingerprint,
                       owner_id, holder_id, content_note, fact_status, supersedes, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (value.material_id, value.material_type, value.title, value.fingerprint,
                 value.owner_id, value.holder_id, value.content_note, value.fact_status,
                 value.supersedes, value.created_at),
            )
        return value

    def get_material(self, material_id: str) -> Material | None:
        row = self.connection.execute(
            "SELECT * FROM materials WHERE material_id=?", (material_id,)
        ).fetchone()
        return self._material(row) if row else None

    def find_by_fingerprint(self, fingerprint: str) -> list[Material]:
        rows = self.connection.execute(
            "SELECT * FROM materials WHERE fingerprint=? ORDER BY created_at", (fingerprint,)
        ).fetchall()
        return [self._material(row) for row in rows]

    def list_materials(self) -> list[Material]:
        rows = self.connection.execute("SELECT * FROM materials ORDER BY created_at").fetchall()
        return [self._material(row) for row in rows]

    def update_material_fact(self, material_id: str, fact_status: str, supersedes: str = "") -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE materials SET fact_status=?, supersedes=? WHERE material_id=?",
                (fact_status, supersedes, material_id),
            )

    @staticmethod
    def _material(row: sqlite3.Row) -> Material:
        data = dict(row)
        return Material(**data)

    def add_relation(self, relation: Relation) -> Relation:
        value = relation.stamped()
        with self.connection:
            self.connection.execute(
                """INSERT INTO relations(source_id, target_id, relation, detail, created_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(source_id, target_id, relation) DO UPDATE SET detail=excluded.detail""",
                (value.source_id, value.target_id, value.relation, value.detail, value.created_at),
            )
        return value

    def relations_into(self, target_id: str) -> list[Relation]:
        rows = self.connection.execute(
            "SELECT source_id, target_id, relation, detail, created_at "
            "FROM relations WHERE target_id=? ORDER BY created_at",
            (target_id,),
        ).fetchall()
        return [Relation(**dict(row)) for row in rows]

    def relations_from(self, source_id: str) -> list[Relation]:
        rows = self.connection.execute(
            "SELECT source_id, target_id, relation, detail, created_at "
            "FROM relations WHERE source_id=? ORDER BY created_at",
            (source_id,),
        ).fetchall()
        return [Relation(**dict(row)) for row in rows]

    def lineage(self, node_id: str, direction: str = "up") -> list[Relation]:
        """沿加工边递归。

        direction='up'  : 返回该节点的全部上游边（它来自哪里）；
        direction='down': 返回该节点的全部下游边（它流向了哪里）。
        """
        if direction == "up":
            sql = """
                WITH RECURSIVE walk(source_id, target_id, relation, detail, depth, path) AS (
                    SELECT source_id, target_id, relation, detail, 1,
                           ',' || target_id || ',' || source_id || ','
                      FROM relations WHERE target_id=?
                    UNION
                    SELECT r.source_id, r.target_id, r.relation, r.detail, w.depth + 1,
                           w.path || r.source_id || ','
                      FROM relations r JOIN walk w ON r.target_id = w.source_id
                     WHERE instr(w.path, ',' || r.source_id || ',') = 0
                )
                SELECT source_id, target_id, relation, detail, depth FROM walk
            """
        else:
            sql = """
                WITH RECURSIVE walk(source_id, target_id, relation, detail, depth, path) AS (
                    SELECT source_id, target_id, relation, detail, 1,
                           ',' || source_id || ',' || target_id || ','
                      FROM relations WHERE source_id=?
                    UNION
                    SELECT r.source_id, r.target_id, r.relation, r.detail, w.depth + 1,
                           w.path || r.target_id || ','
                      FROM relations r JOIN walk w ON r.source_id = w.target_id
                     WHERE instr(w.path, ',' || r.target_id || ',') = 0
                )
                SELECT source_id, target_id, relation, detail, depth FROM walk
            """
        rows = self.connection.execute(sql, (node_id,)).fetchall()
        unique: dict[tuple[str, str, str], Relation] = {}
        for r in rows:
            key = (r["source_id"], r["target_id"], r["relation"])
            unique.setdefault(key, Relation(source_id=r["source_id"], target_id=r["target_id"],
                                           relation=r["relation"], detail=r["detail"]))
        return list(unique.values())

    def ancestor_ids(self, node_id: str) -> set[str]:
        return {edge.source_id for edge in self.lineage(node_id, "up")}

    def descendant_ids(self, node_id: str) -> set[str]:
        return {edge.target_id for edge in self.lineage(node_id, "down")}

    # ------------------------------------------------------------------
    # 结论与证据
    # ------------------------------------------------------------------
    def add_conclusion(self, conclusion_id: str, subject_id: str, statement: str, created_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO conclusions(conclusion_id, subject_id, statement, created_at) VALUES(?,?,?,?)",
                (conclusion_id, subject_id, statement, created_at),
            )

    def get_conclusion(self, conclusion_id: str):
        return self.connection.execute(
            "SELECT * FROM conclusions WHERE conclusion_id=?", (conclusion_id,)
        ).fetchone()

    def add_evidence(self, link: EvidenceLink) -> EvidenceLink:
        value = link.stamped()
        with self.connection:
            self.connection.execute(
                """INSERT INTO evidence(conclusion_id, material_id, stance, detail, created_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(conclusion_id, material_id, stance) DO UPDATE SET detail=excluded.detail""",
                (value.conclusion_id, value.material_id, value.stance, value.detail, value.created_at),
            )
        return value

    def evidence_for(self, conclusion_id: str) -> list[EvidenceLink]:
        rows = self.connection.execute(
            "SELECT conclusion_id, material_id, stance, detail, created_at "
            "FROM evidence WHERE conclusion_id=? ORDER BY created_at",
            (conclusion_id,),
        ).fetchall()
        return [EvidenceLink(**dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # 授权
    # ------------------------------------------------------------------
    def add_authorization(self, license_: Authorization) -> Authorization:
        value = license_.stamped()
        with self.connection:
            self.connection.execute(
                """INSERT INTO authorizations(license_id, holder_id, material_id, territories,
                       valid_from, valid_until, status, declaration, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (value.license_id, value.holder_id, value.material_id, _dumps(value.territories),
                 value.valid_from, value.valid_until, value.status, value.declaration, value.created_at),
            )
        return value

    def get_authorization(self, license_id: str) -> Authorization | None:
        row = self.connection.execute(
            "SELECT * FROM authorizations WHERE license_id=?", (license_id,)
        ).fetchone()
        return self._authorization(row) if row else None

    def authorizations_for(self, material_id: str) -> list[Authorization]:
        """直接挂在该素材上的授权。"""
        rows = self.connection.execute(
            "SELECT * FROM authorizations WHERE material_id=? ORDER BY created_at", (material_id,)
        ).fetchall()
        return [self._authorization(row) for row in rows]

    def covering_authorizations(self, material_id: str) -> list[Authorization]:
        """素材可用的全部授权。

        覆盖三条路径：本素材、任一上游素材，以及被本素材更正替代的旧素材
        （传承人以新说法更正旧说法后，原授权随传承人意思延续到新说法）。
        """
        ids = self.ancestor_ids(material_id)
        ids.add(material_id)
        # 沿 supersession 反向闭包：old.supersedes == new 时，old 上的授权覆盖 new。
        frontier = set(ids)
        while frontier:
            marks = ",".join("?" for _ in frontier)
            rows = self.connection.execute(
                f"SELECT material_id FROM materials WHERE supersedes IN ({marks})",
                tuple(frontier),
            ).fetchall()
            found = {row["material_id"] for row in rows} - ids
            if not found:
                break
            ids.update(found)
            frontier = found
        marks = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT * FROM authorizations WHERE material_id IN ({marks})", tuple(ids)
        ).fetchall()
        return [self._authorization(row) for row in rows]

    def update_authorization_status(self, license_id: str, status: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE authorizations SET status=? WHERE license_id=?", (status, license_id)
            )

    @staticmethod
    def _authorization(row: sqlite3.Row) -> Authorization:
        data = dict(row)
        data["territories"] = _loads(data["territories"])
        return Authorization(**data)

    # ------------------------------------------------------------------
    # 作品、公开版本、更正
    # ------------------------------------------------------------------
    def add_work(self, work: Work) -> Work:
        value = work.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO works(work_id, creator_id, status, region_scope, created_at) VALUES(?,?,?,?,?)",
                (value.work_id, value.creator_id, value.status,
                 _dumps(value.region_scope), value.created_at),
            )
        return value

    def get_work(self, work_id: str) -> Work | None:
        row = self.connection.execute("SELECT * FROM works WHERE work_id=?", (work_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["region_scope"] = _loads(data["region_scope"])
        return Work(**data)

    def list_works(self) -> list[Work]:
        rows = self.connection.execute("SELECT * FROM works ORDER BY created_at").fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["region_scope"] = _loads(data["region_scope"])
            result.append(Work(**data))
        return result

    def update_work(self, work_id: str, status: str, region_scope) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE works SET status=?, region_scope=? WHERE work_id=?",
                (status, _dumps(region_scope), work_id),
            )

    def add_version(self, version: Version) -> Version:
        value = version.stamped()
        with self.connection:
            self.connection.execute(
                """INSERT INTO versions(version_id, work_id, sequence, evidence_frozen,
                       licenses_frozen, territories_frozen, freeze_hash, published_at, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (value.version_id, value.work_id, value.sequence,
                 _dumps(value.evidence_frozen), _dumps(value.licenses_frozen),
                 _dumps(value.territories_frozen), value.freeze_hash,
                 value.published_at, value.created_at),
            )
        return value

    def get_version(self, version_id: str) -> Version | None:
        row = self.connection.execute("SELECT * FROM versions WHERE version_id=?", (version_id,)).fetchone()
        return self._version(row) if row else None

    def versions_for(self, work_id: str) -> list[Version]:
        rows = self.connection.execute(
            "SELECT * FROM versions WHERE work_id=? ORDER BY sequence", (work_id,)
        ).fetchall()
        return [self._version(row) for row in rows]

    def all_versions(self) -> list[Version]:
        rows = self.connection.execute(
            "SELECT * FROM versions ORDER BY work_id, sequence"
        ).fetchall()
        return [self._version(row) for row in rows]

    @staticmethod
    def _version(row: sqlite3.Row) -> Version:
        data = dict(row)
        for key in ("evidence_frozen", "licenses_frozen", "territories_frozen"):
            data[key] = _loads(data[key])
        return Version(**data)

    def add_correction(self, correction: Correction) -> Correction:
        value = correction.stamped()
        with self.connection:
            self.connection.execute(
                """INSERT INTO corrections(correction_id, work_id, version_id, old_material_id,
                       new_material_id, note, rejudged, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (value.correction_id, value.work_id, value.version_id, value.old_material_id,
                 value.new_material_id, value.note, int(value.rejudged), value.created_at),
            )
        return value

    def corrections_for(self, work_id: str) -> list[Correction]:
        rows = self.connection.execute(
            "SELECT * FROM corrections WHERE work_id=? ORDER BY created_at", (work_id,)
        ).fetchall()
        return [Correction(**dict(row)) for row in rows]

    def mark_correction_rejudged(self, correction_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE corrections SET rejudged=1 WHERE correction_id=?", (correction_id,)
            )

    # ------------------------------------------------------------------
    # 争议、复核人员、复核任务
    # ------------------------------------------------------------------
    def add_dispute(self, dispute: Dispute) -> Dispute:
        value = dispute.stamped()
        with self.connection:
            self.connection.execute(
                """INSERT INTO disputes(dispute_id, subject_id, opened_by, reason, status,
                       closed_by, created_at, closed_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (value.dispute_id, value.subject_id, value.opened_by, value.reason, value.status,
                 value.closed_by, value.created_at, value.closed_at),
            )
        return value

    def get_dispute(self, dispute_id: str) -> Dispute | None:
        row = self.connection.execute("SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)).fetchone()
        return Dispute(**dict(row)) if row else None

    def close_dispute(self, dispute_id: str, closed_by: str, closed_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE disputes SET status='closed', closed_by=?, closed_at=? WHERE dispute_id=?",
                (closed_by, closed_at, dispute_id),
            )

    def add_reviewer(self, reviewer: Reviewer) -> Reviewer:
        value = reviewer.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO reviewers(reviewer_id, identity, created_at) VALUES(?,?,?)",
                (value.reviewer_id, value.identity, value.created_at),
            )
        return value

    def get_reviewer(self, reviewer_id: str) -> Reviewer | None:
        row = self.connection.execute("SELECT * FROM reviewers WHERE reviewer_id=?", (reviewer_id,)).fetchone()
        return Reviewer(**dict(row)) if row else None

    def add_task(self, task: ReviewTask) -> ReviewTask:
        value = task.stamped()
        with self.connection:
            self.connection.execute(
                """INSERT INTO review_tasks(task_id, task_type, subject_id, reason, status,
                       assignee_id, created_at, completed_at, carried_from)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO NOTHING""",
                (value.task_id, value.task_type, value.subject_id, value.reason, value.status,
                 value.assignee_id, value.created_at, value.completed_at, value.carried_from),
            )
        return self.get_task(value.task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> ReviewTask | None:
        row = self.connection.execute("SELECT * FROM review_tasks WHERE task_id=?", (task_id,)).fetchone()
        return ReviewTask(**dict(row)) if row else None

    def pending_tasks(self, subject_id: str | None = None, task_type: str | None = None) -> list[ReviewTask]:
        sql = "SELECT * FROM review_tasks WHERE status='pending'"
        params: list[str] = []
        if subject_id is not None:
            sql += " AND subject_id=?"
            params.append(subject_id)
        if task_type is not None:
            sql += " AND task_type=?"
            params.append(task_type)
        sql += " ORDER BY created_at"
        rows = self.connection.execute(sql, params).fetchall()
        return [ReviewTask(**dict(row)) for row in rows]

    def all_tasks(self) -> list[ReviewTask]:
        rows = self.connection.execute("SELECT * FROM review_tasks ORDER BY created_at").fetchall()
        return [ReviewTask(**dict(row)) for row in rows]

    def complete_task(self, task_id: str, completed_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE review_tasks SET status='done', completed_at=? WHERE task_id=?",
                (completed_at, task_id),
            )

    def assign_task(self, task_id: str, assignee_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE review_tasks SET assignee_id=? WHERE task_id=?", (assignee_id, task_id)
            )

    # ------------------------------------------------------------------
    # 事件日志
    # ------------------------------------------------------------------
    def add_event(self, event: Event) -> Event:
        with self.connection:
            cur = self.connection.execute(
                "INSERT INTO events(event_type, subject_id, detail, created_at) VALUES(?,?,?,?)",
                (event.event_type, event.subject_id, event.detail, event.created_at or None),
            )
            event_id = cur.lastrowid
        row = self.connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return Event(**dict(row))

    def events_for(self, subject_id: str) -> list[Event]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE subject_id=? ORDER BY event_id", (subject_id,)
        ).fetchall()
        return [Event(**dict(row)) for row in rows]
