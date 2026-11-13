"""构建本地示例源库：python -I scripts/build_demo_db.py [--orphan] [路径]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.demo import build_demo_db  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="构建切片服务示例源库")
    parser.add_argument(
        "path",
        nargs="?",
        default="/data/source.db",
        help="输出的 SQLite 文件路径（默认 /data/source.db）",
    )
    parser.add_argument(
        "--orphan",
        action="store_true",
        help="植入一条缺失引用的退货明细（用于演示整次失败、无残件）",
    )
    args = parser.parse_args()
    path = build_demo_db(args.path, with_orphan=args.orphan)
    print(f"示例源库已生成: {path}" + ("（含断引用）" if args.orphan else ""))


if __name__ == "__main__":
    main()
