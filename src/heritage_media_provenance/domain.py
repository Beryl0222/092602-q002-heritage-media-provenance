"""非遗影像来源与许可的领域模型。

图谱中的素材节点统一用 :class:`Material` 表示，``material_type`` 区分：

``interview``   原始访谈（传承人口述音视频/逐字稿）
``step``        手艺步骤
``reference``   参考文献
``translation`` 翻译说明（译注）
``clip``        剪辑片段
``statement``   创作者声明
``license``     传承人授权
``work``        派生作品（成片、译注稿等，可能被发布/冻结为版本）

节点之间用 :class:`Derivation` 记录加工关系（父素材 → 加工后的素材）；
:class:`Claim` 是素材承载的“说法”，说法之间可互相佐证或彼此矛盾；
:class:`Publication` 冻结某一作品在公开时刻采用的证据与许可。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

MATERIAL_TYPES = (
    "interview",
    "step",
    "reference",
    "translation",
    "clip",
    "statement",
    "license",
    "work",
)

# 说法状态：active 有效；corrected 已被传承人更正；disputed 存在未决争议。
CLAIM_STATES = ("active", "corrected", "disputed")
# 素材流转状态：draft 未发布流转中；blocked 被停止流转；published 已有公开版本；
# withdrawn 因授权撤回/到期而下架；corrected 公开后收到更正，已形成更正记录。
WORKFLOW_STATES = ("collected", "draft", "blocked", "published", "withdrawn", "corrected")
# 授权状态。
LICENSE_STATES = ("active", "expired", "withdrawn")
# 争议状态：open 未决；closed 已关闭（只能由非创作者关闭）。
DISPUTE_STATES = ("open", "closed")
# 复核任务状态：open 待办；closed 已完成。
REVIEW_STATES = ("open", "closed")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Record:
    """基线记录，保持与最早版本兼容。"""

    record_id: str
    owner_id: str
    state: str
    revision: int = 1
    created_at: str = ""

    def stamped(self) -> "Record":
        value = self.created_at or now_iso()
        return replace(self, created_at=value)


@dataclass(frozen=True)
class Material:
    """图谱节点：原始素材、授权或派生作品。"""

    material_id: str
    material_type: str
    fingerprint: str
    title: str = ""
    creator_id: str = ""
    holder_id: str = ""  # 传承人，授权/更正由本人作出
    state: str = "collected"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def stamped(self) -> "Material":
        return replace(self, created_at=self.created_at or now_iso())


@dataclass(frozen=True)
class Derivation:
    """加工边：parent 经 process（剪辑/AI 整理/翻译/引用…）产出 child。"""

    parent_id: str
    child_id: str
    process: str
    actor_id: str = ""
    note: str = ""
    created_at: str = ""

    def stamped(self) -> "Derivation":
        return replace(self, created_at=self.created_at or now_iso())


@dataclass(frozen=True)
class Claim:
    """素材承载的说法（含语义 polarity：正面/反面表述，用于发现含义被剪反）。"""

    claim_id: str
    material_id: str
    subject: str
    content: str
    source_id: str = ""  # 说法实际来自谁（如传承人 holder_id）
    polarity: str = "positive"  # positive / negative
    state: str = "active"
    supersedes: str = ""  # 更正时指向被更正的旧说法
    created_at: str = ""

    def stamped(self) -> "Claim":
        return replace(self, created_at=self.created_at or now_iso())


@dataclass(frozen=True)
class ClaimRelation:
    """说法之间的关系：corroborate 互相佐证 / contradict 内容矛盾。"""

    from_claim_id: str
    to_claim_id: str
    relation: str  # corroborate / contradict
    note: str = ""
    created_at: str = ""

    def stamped(self) -> "ClaimRelation":
        return replace(self, created_at=self.created_at or now_iso())


@dataclass(frozen=True)
class LicenseGrant:
    """传承人授权：授权人、被授权创作者、地区列表、起止期限、状态。"""

    license_id: str
    fingerprint: str  # 授权随素材指纹登记
    holder_id: str
    creator_id: str
    territories: tuple[str, ...]
    valid_from: str
    valid_until: str = ""  # 空表示长期有效
    state: str = "active"
    declared_terms: str = ""  # 许可声明原文/摘要，指纹相同声明不同需复核
    created_at: str = ""

    def stamped(self) -> "LicenseGrant":
        return replace(self, created_at=self.created_at or now_iso())


@dataclass(frozen=True)
class Dispute:
    """争议：创作者本人不能关闭自己发起或涉及作品的争议。"""

    dispute_id: str
    material_id: str
    opener_id: str
    reason: str = ""
    state: str = "open"
    closer_id: str = ""
    resolution: str = ""
    created_at: str = ""
    closed_at: str = ""


@dataclass(frozen=True)
class ReviewTask:
    """独立复核任务：指纹冲突、未完成复核在授权变动后必须延续。"""

    task_id: str
    reason: str
    material_id: str = ""
    fingerprint: str = ""
    assignee_id: str = ""  # 独立人员（非创作者本人）
    status: str = "open"
    created_at: str = ""
    closed_at: str = ""

    def stamped(self) -> "ReviewTask":
        return replace(self, created_at=self.created_at or now_iso())


@dataclass(frozen=True)
class Publication:
    """公开版本：冻结发布时采用的证据（说法/素材）与许可（授权）。"""

    publication_id: str
    work_id: str
    version: int
    territories: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    license_ids: tuple[str, ...]
    published_by: str = ""
    published_at: str = ""
    correction_of: str = ""  # 若是更正重发，指向上一版本
    note: str = ""
