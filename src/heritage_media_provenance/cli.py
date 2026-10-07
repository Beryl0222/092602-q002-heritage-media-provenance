"""非遗影像来源与许可的命令行入口。

所有命令共用一个 SQLite 库文件，可用 ``--db`` 或环境变量 ``HMP_DB`` 指定，
默认写入当前目录的 ``heritage_provenance.db``。

写入类命令的载荷可以是 JSON 文件路径，也可以直接给出内联 JSON（以 ``{`` 开头）。

示例：
    python -m heritage_media_provenance.cli trace clip-001
    python -m heritage_media_provenance.cli import payloads/interview.json
    python -m heritage_media_provenance.cli revoke lic-001
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .service import Service, ServiceError, dumps
from .store import Store

DEFAULT_DB = "heritage_provenance.db"


def _store(args: argparse.Namespace) -> Store:
    path = args.db or os.environ.get("HMP_DB") or DEFAULT_DB
    return Store(path)


def _payload(value: str) -> dict[str, Any]:
    text = value if value.lstrip().startswith("{") else Path(value).read_text(
        encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("载荷必须是 JSON 对象")
    return data


def _print(payload: Any) -> None:
    print(dumps(payload))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heritage_media_provenance",
        description="非遗影像来源与许可图谱：登记、追溯、争议、更正与传播范围管理",
    )
    parser.add_argument("--db", default="", help="SQLite 库路径（默认 HMP_DB 或 "
                                                 + DEFAULT_DB + "）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="健康检查")

    p = sub.add_parser("validate", help="校验并登记一条基线记录")
    p.add_argument("payload", help="JSON 文件")

    p = sub.add_parser("import", help="按指纹导入素材（同指纹自动复用）")
    p.add_argument("payload")

    p = sub.add_parser("derive", help="登记一条加工关系（父素材 → 子素材）")
    p.add_argument("payload")

    p = sub.add_parser("claim", help="给素材登记一条说法")
    p.add_argument("payload")

    p = sub.add_parser("relate", help="登记说法间 corroborate/contradict 关系")
    p.add_argument("payload")

    p = sub.add_parser("license", help="登记传承人授权（地区与期限）")
    p.add_argument("payload")

    p = sub.add_parser("attach", help="把授权挂接到素材")
    p.add_argument("payload")

    p = sub.add_parser("publish", help="发布作品并冻结证据与许可")
    p.add_argument("payload")

    p = sub.add_parser("open-dispute", help="开启争议")
    p.add_argument("payload")

    p = sub.add_parser("close-dispute", help="关闭争议（创作者本人不可关闭）")
    p.add_argument("payload")

    p = sub.add_parser("correct", help="传承人更正说法并联动下游作品")
    p.add_argument("payload")

    p = sub.add_parser("revoke", help="撤回授权并找全受影响作品")
    p.add_argument("license_id")

    p = sub.add_parser("sweep", help="扫描到期授权并延续复核")
    p.add_argument("--at", default="", help="判定时点（ISO 时间），默认当前")

    p = sub.add_parser("resolve-task", help="由独立人员关闭复核任务")
    p.add_argument("payload")

    p = sub.add_parser("tasks", help="列出复核任务")
    p.add_argument("--status", default="open", choices=["open", "closed", "all"])

    p = sub.add_parser("pending", help="列出当前必须处理的作品")

    p = sub.add_parser("publications", help="列出版本（可按作品过滤）")
    p.add_argument("work_id", nargs="?", default="")

    p = sub.add_parser("availability", help="查看作品地区×期限可用性")
    p.add_argument("work_id")
    p.add_argument("--at", default="")

    p = sub.add_parser("trace", help="溯源：任一片段来自哪里、加工与可用范围")
    p.add_argument("material_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = Service(_store(args))
    try:
        if args.command == "health":
            _print(service.health())
        elif args.command == "validate":
            _print(service.register(_payload(args.payload)))
        elif args.command == "import":
            _print(service.import_material(_payload(args.payload)))
        elif args.command == "derive":
            _print(service.add_derivation(_payload(args.payload)))
        elif args.command == "claim":
            _print(service.add_claim(_payload(args.payload)))
        elif args.command == "relate":
            _print(service.relate_claims(_payload(args.payload)))
        elif args.command == "license":
            _print(service.add_license(_payload(args.payload)))
        elif args.command == "attach":
            _print(service.attach_license(_payload(args.payload)))
        elif args.command == "publish":
            _print(service.publish_work(_payload(args.payload)))
        elif args.command == "open-dispute":
            _print(service.open_dispute(_payload(args.payload)))
        elif args.command == "close-dispute":
            _print(service.close_dispute(_payload(args.payload)))
        elif args.command == "correct":
            _print(service.correct_claim(_payload(args.payload)))
        elif args.command == "revoke":
            _print(service.revoke_license(args.license_id))
        elif args.command == "sweep":
            _print(service.sweep_expirations(args.at))
        elif args.command == "resolve-task":
            _print(service.resolve_review_task(_payload(args.payload)))
        elif args.command == "tasks":
            status = "" if args.status == "all" else args.status
            _print(service.list_review_tasks(status))
        elif args.command == "pending":
            _print(service.pending_works())
        elif args.command == "publications":
            _print(service.list_publications(args.work_id))
        elif args.command == "availability":
            _print(service.availability(args.work_id, args.at))
        elif args.command == "trace":
            _print(service.trace(args.material_id))
        else:  # pragma: no cover - argparse 已保证
            parser.error(f"未知命令：{args.command}")
    except (ServiceError, ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
