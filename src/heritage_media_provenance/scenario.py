"""场景装载器：按顺序执行 JSON 操作单，驱动完整业务故事。

操作单中的 ``op`` 字段对应服务方法；``{"op": "clock", "date": ...}``
可把系统时钟拨到指定日期，用于演示授权期限与到期处置。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .service import Service


class ScenarioLoader:
    def __init__(self, service: Service) -> None:
        self.service = service
        self.timeline: list[dict] = []

    def load_file(self, path: str | Path) -> dict:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return self.run(payload)

    def run(self, payload: dict) -> dict:
        operations = payload.get("operations", [])
        if payload.get("as_of"):
            self._set_clock(str(payload["as_of"]))
        for index, operation in enumerate(operations):
            op = str(operation.get("op", ""))
            handler = getattr(self, f"op_{op}", None)
            if handler is None:
                raise ValueError(f"未知场景操作：{op}（第 {index + 1} 步）")
            result = handler(operation)
            self.timeline.append({"step": index + 1, "op": op,
                                  "label": operation.get("label", ""), "result": result})
        summary = {
            "description": payload.get("名称", payload.get("description", "")),
            "steps_executed": len(self.timeline),
            "timeline": self.timeline,
            "final_pending_tasks": self.service.pending_review_tasks(),
        }
        traces = payload.get("trace_at_end", [])
        if traces:
            summary["final_traces"] = {fragment: self.service.trace(fragment) for fragment in traces}
        return summary

    # ------------------------------------------------------------------
    def _set_clock(self, value: str) -> None:
        day = date.fromisoformat(value)
        self.service._today = lambda: day  # type: ignore[method-assign]

    def _record(self, name: str, result) -> dict:
        return {"op": name, "result": result}

    def op_clock(self, operation: dict) -> dict:
        self._set_clock(str(operation["date"]))
        return self._record("clock", {"as_of": operation["date"]})

    def op_reviewer(self, operation: dict) -> dict:
        return self.service.register_reviewer(operation)

    def op_material(self, operation: dict) -> dict:
        return self.service.register_material(operation)

    def op_relation(self, operation: dict) -> dict:
        return self.service.add_relation(operation)

    def op_conclusion(self, operation: dict) -> dict:
        return self.service.add_conclusion(operation)

    def op_evidence(self, operation: dict) -> dict:
        return self.service.add_evidence(operation)

    def op_authorization(self, operation: dict) -> dict:
        return self.service.register_authorization(operation)

    def op_work(self, operation: dict) -> dict:
        return self.service.register_work(operation)

    def op_publish(self, operation: dict) -> dict:
        return self.service.publish_version(operation)

    def op_dispute_open(self, operation: dict) -> dict:
        return self.service.open_dispute(operation)

    def op_dispute_close(self, operation: dict) -> dict:
        try:
            return self.service.close_dispute(operation)
        except ValueError as exc:
            return {"rejected": True, "reason": str(exc)}

    def op_mismatch_assign(self, operation: dict) -> dict:
        try:
            return self.service.assign_review(operation)
        except ValueError as exc:
            return {"rejected": True, "reason": str(exc)}

    def op_mismatch_resolve(self, operation: dict) -> dict:
        return self.service.resolve_fingerprint_mismatch(operation)

    def op_withdraw(self, operation: dict) -> dict:
        return self.service.withdraw_authorization(operation)

    def op_expire(self, operation: dict) -> dict:
        return self.service.expire_authorizations()

    def op_correct(self, operation: dict) -> dict:
        return self.service.correct_fact(operation)

    def op_rejudge(self, operation: dict) -> dict:
        return self.service.rejudge_work_scope(str(operation["work_id"]))

    def op_trace(self, operation: dict) -> dict:
        return self.service.trace(str(operation["fragment_id"]))

    def op_tasks(self, operation: dict) -> dict:
        subject = operation.get("subject_id")
        return self.service.pending_review_tasks(subject)
