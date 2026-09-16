#!/usr/bin/env python3
"""Enforce the four-module design-document update policy."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


CANONICAL_DOCS = {
    "docs/战斗.md",
    "docs/经济.md",
    "docs/防御.md",
    "docs/自进化.md",
}
LEGACY_DOCS = {
    "docs/deadline-economy.md",
    "docs/personal-hjc-main-integration.md",
    "docs/steady-mining.md",
    "docs/worker-logistics.md",
    "docs/实战采购与自进化改进.md",
    "docs/建墙与弱模型优化.md",
    "docs/来源与差异.md",
    "docs/策略优化验证.md",
    "docs/自进化与连续防线修复.md",
    "docs/设计思路.md",
    "docs/设计文档.md",
}
REQUIRED_SECTIONS = ("## 关键设定与原因", "## 变更记录")
REQUIRED_CHANGE_FIELDS = ("策略：", "设定：", "原因：", "经验教训：", "验证：")
IGNORED_PREFIXES = (".github/", "docs/")
IGNORED_FILES = {"AGENTS.md", "README.md"}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout


def changed_entries(base: str, head: str) -> list[tuple[str, str]]:
    rows = []
    for line in git("diff", "--name-status", "--find-renames", f"{base}...{head}").splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        path = parts[-1]
        rows.append((status, path))
    return rows


def is_product_change(path: str) -> bool:
    if path in IGNORED_FILES or path == "tools/check_design_governance.py":
        return False
    return not path.startswith(IGNORED_PREFIXES)


def added_doc_lines(base: str, head: str, docs: set[str]) -> str:
    if not docs:
        return ""
    diff = git("diff", "--unified=0", f"{base}...{head}", "--", *sorted(docs))
    return "\n".join(
        line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
    )


def check(base: str, head: str) -> list[str]:
    errors: list[str] = []
    entries = changed_entries(base, head)
    changed = {path for _, path in entries}
    changed_canonical = changed & CANONICAL_DOCS

    for status, path in entries:
        deleting_legacy = status == "D" and path in LEGACY_DOCS
        if (
            path.startswith("docs/")
            and path.endswith(".md")
            and path not in CANONICAL_DOCS
            and not deleting_legacy
        ):
            errors.append(
                f"不允许{ {'A': '新增', 'D': '删除'}.get(status, '修改') }非四模块文档：{path}"
            )

    for path in sorted(CANONICAL_DOCS):
        file_path = Path(path)
        if not file_path.is_file():
            errors.append(f"缺少固定模块文档：{path}")
            continue
        content = file_path.read_text(encoding="utf-8")
        for section in REQUIRED_SECTIONS:
            if section not in content:
                errors.append(f"{path} 缺少固定章节：{section}")

    product_changes = sorted(path for path in changed if is_product_change(path))
    if product_changes and not changed_canonical:
        errors.append("代码、配置或策略发生变化，但四份模块设计文档均未更新")

    if product_changes:
        additions = added_doc_lines(base, head, changed_canonical)
        for field in REQUIRED_CHANGE_FIELDS:
            if field not in additions:
                errors.append(f"模块文档本次新增内容缺少字段：{field}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="base commit SHA")
    parser.add_argument("--head", default="HEAD", help="head commit/ref")
    args = parser.parse_args()

    errors = check(args.base, args.head)
    if errors:
        print("设计文档门禁失败：")
        for error in errors:
            print(f"- {error}")
        return 1
    print("设计文档门禁通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
