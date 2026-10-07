"""非遗影像来源与许可使用的领域模型。

模型围绕一张可追溯图谱组织：

- 素材 :class:`Material` —— 原始访谈、手艺步骤、参考文献、翻译说明、
  剪辑片段、创作者声明、派生作品等一切可被引用的对象；
- 关系 :class:`Relation` —— 素材之间的加工边（剪辑、引用、翻译、
  派生、写进译注……），边携带来源标记，构成溯源链；
- 授权 :class:`Authorization` —— 传承人授权，限定地区与期限；
- 证据 :class:`EvidenceLink` —— 素材对结论的佐证／矛盾立场，矛盾必须保留；
- 公开版本 :class:`Version` —— 冻结发布当时采用的证据与许可；
- 争议 :class:`Dispute` 与复核任务 :class:`ReviewTask` ——
  指纹声明冲突、事实更正、许可撤回/到期后的待处理工作。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

#: 素材类型
INTERVIEW = "interview"            # 原始访谈
CRAFT_STEP = "craft_step"          # 手艺步骤
REFERENCE = "reference"            # 参考文献
TRANSLATION_NOTE = "translation_note"  # 翻译说明/译注
CLIP = "clip"                      # 剪辑片段
CREATOR_STATEMENT = "creator_statement"  # 创作者声明
AUTHORIZATION_DOC = "authorization_doc"  # 传承人授权凭据（登记在素材图谱中）
DERIVED_WORK = "derived_work"      # 派生作品

MATERIAL_TYPES = frozenset({
    INTERVIEW, CRAFT_STEP, REFERENCE, TRANSLATION_NOTE, CLIP,
    CREATOR_STATEMENT, AUTHORIZATION_DOC, DERIVED_WORK,
})

#: 加工关系类型
REL_DERIVES = "derives_from"       # 通用派生
REL_EDITS = "edits"                # 剪辑（含义可能被改变）
REL_CITES = "cites"                # 引用
REL_TRANSLATES = "translates"      # 翻译
REL_ANNOTATES = "annotates"        # 译注/整理（AI 整理材料写入）
REL_STATES = "statement_about"     # 创作者声明针对的素材
REL_AUTHORIZES = "authorizes"      # 授权凭据针对的素材/作品
REL_USES = "uses"                  # 作品使用某素材

RELATION_TYPES = frozenset({
    REL_DERIVES, REL_EDITS, REL_CITES, REL_TRANSLATES,
    REL_ANNOTATES, REL_STATES, REL_AUTHORIZES, REL_USES,
})

#: 证据立场：彼此佐证共同支撑结论；内容矛盾必须保留分歧
STANCE_SUPPORTS = "supports"
STANCE_CONTRADICTS = "contradicts"
STANCES = frozenset({STANCE_SUPPORTS, STANCE_CONTRADICTS})

#: 事实更正后，来源/证据的当前状态
FACT_ACTIVE = "active"             # 仍可采用
FACT_SUPERSEDED = "superseded"     # 已被传承人更正，内容保留留痕
FACT_STATUSES = frozenset({FACT_ACTIVE, FACT_SUPERSEDED})

#: 授权状态
LICENSE_ACTIVE = "active"
LICENSE_WITHDRAWN = "withdrawn"    # 传承人撤回
LICENSE_EXPIRED = "expired"        # 到期

#: 作品流转状态
WORK_DRAFT = "draft"               # 尚未发布：停止流转
WORK_PUBLISHED = "published"       # 已公开：形成更正记录并重新判断传播范围
WORK_HELD = "held"                 # 被停止流转的未发布作品
WORK_STATUSES = frozenset({WORK_DRAFT, WORK_PUBLISHED, WORK_HELD})

#: 争议状态
DISPUTE_OPEN = "open"
DISPUTE_CLOSED = "closed"

#: 复核任务类型
TASK_FINGERPRINT_MISMATCH = "fingerprint_mismatch"  # 指纹一致、许可声明不同
TASK_FACT_CORRECTION = "fact_correction"            # 传承人更正事实
TASK_LICENSE_WITHDRAWN = "license_withdrawn"        # 授权撤回
TASK_LICENSE_EXPIRED = "license_expired"            # 授权到期
TASK_REJUDGE_SCOPE = "rejudge_scope"                # 已发布作品重新判断传播范围
TASK_TYPES = frozenset({
    TASK_FINGERPRINT_MISMATCH, TASK_FACT_CORRECTION,
    TASK_LICENSE_WITHDRAWN, TASK_LICENSE_EXPIRED, TASK_REJUDGE_SCOPE,
})

TASK_PENDING = "pending"
TASK_DONE = "done"
TASK_STATUSES = frozenset({TASK_PENDING, TASK_DONE})

#: 事件日志类型
EVENT_FINGERPRINT_REUSE = "fingerprint_reuse"
EVENT_MISMATCH_FLAGGED = "fingerprint_mismatch_flagged"
EVENT_HOLD = "work_held"
EVENT_CORRECTION = "correction_recorded"
EVENT_REJUDGE = "scope_rejudged"
EVENT_LICENSE_WITHDRAWN = "license_withdrawn"
EVENT_LICENSE_EXPIRED = "license_expired"
EVENT_DISPUTE_OPENED = "dispute_opened"
EVENT_DISPUTE_CLOSE_REJECTED = "dispute_close_rejected"
EVENT_DISPUTE_CLOSED = "dispute_closed"
EVENT_TASK_CARRIED = "review_task_carried"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def now() -> str:
    """统一的 UTC 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


def normalize_fingerprint(value: str) -> str:
    """指纹统一为去空白后的小写串；传入值本身即视为素材指纹。"""
    return re.sub(r"\s+", "", str(value)).lower()


def content_fingerprint(blob: str | bytes) -> str:
    """对原始内容计算 SHA-256 指纹，供导入时使用。"""
    if isinstance(blob, str):
        blob = blob.encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def freeze(payload: dict) -> str:
    """公开版本冻结：把证据 id 与许可 id 集合规范化为可复现的指纹。"""
    canonical = repr(sorted((str(k), sorted(v)) for k, v in payload.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 数据记录
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Record:
    """早期基础记录，保留以兼容既有登记能力。"""
    record_id: str
    owner_id: str
    state: str
    revision: int = 1
    created_at: str = ""

    def stamped(self) -> "Record":
        value = self.created_at or now()
        return replace(self, created_at=value)


@dataclass(frozen=True)
class Material:
    """图谱节点：任何可被追溯、引用、授权或加工的对象。"""
    material_id: str
    material_type: str
    title: str = ""
    fingerprint: str = ""
    owner_id: str = ""                 # 创作者/上传者
    holder_id: str = ""                # 传承人（口述/手艺的事实来源）
    content_note: str = ""             # 内容摘要或未经核对的标注
    fact_status: str = FACT_ACTIVE
    supersedes: str = ""               # 被本素材更正的旧素材 id
    created_at: str = ""

    def stamped(self) -> "Material":
        return replace(self, created_at=self.created_at or now())


@dataclass(frozen=True)
class Relation:
    """图谱边：target 由 source 加工而来（source → target）。"""
    source_id: str
    target_id: str
    relation: str
    detail: str = ""                   # 例如“剪辑反转原意”
    created_at: str = ""

    def stamped(self) -> "Relation":
        return replace(self, created_at=self.created_at or now())


@dataclass(frozen=True)
class Authorization:
    """传承人授权：限定素材、地区与期限。"""
    license_id: str
    holder_id: str                     # 传承人
    material_id: str                   # 授权覆盖的素材（通常是原始访谈/作品）
    territories: tuple[str, ...] = ()  # 覆盖地区
    valid_from: str = ""
    valid_until: str = ""
    status: str = LICENSE_ACTIVE
    declaration: str = ""              # 导入时随附的许可声明文本
    created_at: str = ""

    def stamped(self) -> "Authorization":
        return replace(self, created_at=self.created_at or now())


@dataclass(frozen=True)
class EvidenceLink:
    """素材对某一结论的立场；矛盾立场同样持久化，不许丢弃。"""
    conclusion_id: str
    material_id: str
    stance: str
    detail: str = ""
    created_at: str = ""

    def stamped(self) -> "EvidenceLink":
        return replace(self, created_at=self.created_at or now())


@dataclass(frozen=True)
class Work:
    """派生作品及其流转状态。"""
    work_id: str
    creator_id: str
    status: str = WORK_DRAFT
    region_scope: tuple[str, ...] = ()
    created_at: str = ""

    def stamped(self) -> "Work":
        return replace(self, created_at=self.created_at or now())


@dataclass(frozen=True)
class Version:
    """公开版本：冻结发布当时采用的证据集合与许可集合。"""
    version_id: str
    work_id: str
    sequence: int
    evidence_frozen: tuple[str, ...] = ()
    licenses_frozen: tuple[str, ...] = ()
    territories_frozen: tuple[str, ...] = ()
    freeze_hash: str = ""
    published_at: str = ""
    created_at: str = ""

    def stamped(self) -> "Version":
        value = self.created_at or now()
        published = self.published_at or value
        freeze_hash = self.freeze_hash or freeze({
            "evidence": list(self.evidence_frozen),
            "licenses": list(self.licenses_frozen),
        })
        return replace(self, created_at=value, published_at=published, freeze_hash=freeze_hash)


@dataclass(frozen=True)
class Correction:
    """已公开作品的更正记录：事实被更正后留痕并触发传播范围重判。"""
    correction_id: str
    work_id: str
    version_id: str
    old_material_id: str
    new_material_id: str
    note: str = ""
    rejudged: int = 0
    created_at: str = ""

    def stamped(self) -> "Correction":
        return replace(self, created_at=self.created_at or now())


@dataclass(frozen=True)
class Dispute:
    """争议：创作者声明不能由创作者本人关闭。"""
    dispute_id: str
    subject_id: str                    # 被争议的素材/作品
    opened_by: str
    reason: str = ""
    status: str = DISPUTE_OPEN
    closed_by: str = ""
    created_at: str = ""
    closed_at: str = ""

    def stamped(self) -> "Dispute":
        value = replace(self, created_at=self.created_at or now())
        return value


@dataclass(frozen=True)
class Reviewer:
    """复核人员：指纹/声明冲突必须交给独立人员。"""
    reviewer_id: str
    identity: str = ""                 # person/team，team 不为任何创作者
    created_at: str = ""

    def stamped(self) -> "Reviewer":
        return replace(self, created_at=self.created_at or now())


@dataclass(frozen=True)
class ReviewTask:
    """复核任务；许可变更后必须能找全受影响作品并延续未完成复核。"""
    task_id: str
    task_type: str
    subject_id: str                    # 受影响的素材/作品 id
    reason: str = ""
    status: str = TASK_PENDING
    assignee_id: str = ""
    created_at: str = ""
    completed_at: str = ""
    carried_from: str = ""             # 由哪一条旧任务延续而来

    def stamped(self) -> "ReviewTask":
        return replace(self, created_at=self.created_at or now())


@dataclass(frozen=True)
class Event:
    """只增的事件日志，保证每一步处置可审计。"""
    event_id: int = 0
    event_type: str = ""
    subject_id: str = ""
    detail: str = ""
    created_at: str = field(default_factory=now)
