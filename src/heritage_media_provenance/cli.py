"""非遗影像来源与许可的命令行入口。

子命令：

  validate <file>                兼容早期基础记录的 JSON 校验/登记
  scenario <file> [--db PATH]    按操作单装载完整业务场景
  trace      <fragment_id>       任一片段：来自哪里、加工链、地区/期限、待处理事项
  tasks      [--subject ID]      列出当前必须处理的复核任务
  revoke     --license ID        撤回授权（找全受影响作品）
  expire     [--as-of DATE]      按日期处理到期授权并找全受影响作品
  rejudge    --work ID           对已发布作品重新判断传播范围
  close-dispute --dispute ID --by USER
                                 关闭争议（创作者本人关闭将被拒绝）
  resolve-mismatch --task ID --reviewer ID --decision ...
                                 独立人员复核“指纹一致但许可声明不同”
  health                         健康检查（无参数时的默认输出）

所有图谱命令用 --db PATH 持久化到同一 SQLite 文件；缺省为内存库。
另支持 --as-of YYYY-MM-DD 把判定日期拨到指定日期。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .scenario import ScenarioLoader
from .service import Service, ServiceError
from .store import Store


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def _service(args: argparse.Namespace) -> Service:
    db_path = getattr(args, "db", None) or ":memory:"
    return Service(Store(db_path))


def _as_of(args: argparse.Namespace) -> date | None:
    value = getattr(args, "as_of", None)
    return date.fromisoformat(value) if value else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="heritage_media_provenance",
                                 description="非遗影像来源与许可图谱")
    sub = parser.add_subparsers(dest="command")

    p_validate = sub.add_parser("validate", help="校验并登记一条基础记录 JSON")
    p_validate.add_argument("file")

    p_scenario = sub.add_parser("scenario", help="按操作单装载业务场景")
    p_scenario.add_argument("file")
    p_scenario.add_argument("--db")

    p_trace = sub.add_parser("trace", help="片段溯源报告")
    p_trace.add_argument("fragment_id")
    p_trace.add_argument("--db")
    p_trace.add_argument("--as-of", dest="as_of")

    p_tasks = sub.add_parser("tasks", help="列出待处理复核任务")
    p_tasks.add_argument("--db")
    p_tasks.add_argument("--subject")

    p_revoke = sub.add_parser("revoke", help="撤回传承人授权")
    p_revoke.add_argument("--db")
    p_revoke.add_argument("--license", dest="license_id", required=True)
    p_revoke.add_argument("--note", default="")

    p_expire = sub.add_parser("expire", help="按日期处理到期授权")
    p_expire.add_argument("--db")
    p_expire.add_argument("--as-of", dest="as_of")

    p_rejudge = sub.add_parser("rejudge", help="重新判断已发布作品的传播范围")
    p_rejudge.add_argument("--db")
    p_rejudge.add_argument("--work", dest="work_id", required=True)
    p_rejudge.add_argument("--as-of", dest="as_of")

    p_close = sub.add_parser("close-dispute", help="关闭争议（创作者本人将被拒绝）")
    p_close.add_argument("--db")
    p_close.add_argument("--dispute", dest="dispute_id", required=True)
    p_close.add_argument("--by", dest="closed_by", required=True)

    p_resolve = sub.add_parser("resolve-mismatch", help="独立人员复核指纹/许可声明冲突")
    p_resolve.add_argument("--db")
    p_resolve.add_argument("--task", dest="task_id", required=True)
    p_resolve.add_argument("--reviewer", dest="reviewer_id", required=True)
    p_resolve.add_argument("--decision", choices=["accept_incoming", "keep_existing"],
                           required=True)

    sub.add_parser("health", help="健康检查")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command

    try:
        if command is None or command == "health":
            _print(Service(Store()).health())
            return 0

        if command == "validate":
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            _print(Service(Store()).register(payload))
            return 0

        if command == "scenario":
            service = _service(args)
            summary = ScenarioLoader(service).load_file(args.file)
            _print(summary)
            return 0

        service = _service(args)
        if command == "trace":
            _print(service.trace(args.fragment_id, _as_of(args)))
        elif command == "tasks":
            _print(service.pending_review_tasks(args.subject))
        elif command == "revoke":
            _print(service.withdraw_authorization(
                {"license_id": args.license_id, "note": args.note}))
        elif command == "expire":
            _print(service.expire_authorizations(_as_of(args)))
        elif command == "rejudge":
            _print(service.rejudge_work_scope(args.work_id, _as_of(args)))
        elif command == "close-dispute":
            _print(service.close_dispute(
                {"dispute_id": args.dispute_id, "closed_by": args.closed_by}))
        elif command == "resolve-mismatch":
            _print(service.resolve_fingerprint_mismatch({
                "task_id": args.task_id, "reviewer_id": args.reviewer_id,
                "decision": args.decision,
            }))
        else:  # pragma: no cover - argparse 已拦截
            parser.error(f"未知命令：{command}")
        return 0
    except (ServiceError, ValueError, FileNotFoundError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
