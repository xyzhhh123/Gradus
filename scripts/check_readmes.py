#!/usr/bin/env python3
"""Keep the four READMEs from drifting apart.

Checks, across README.md / README.en.md / README.ja.md / README.ko.md:

1. every file exists and carries the same language switcher line;
2. the heading skeleton matches (same sequence of levels, so sections are
   added or removed in all four at once);
3. block-level structure matches (fenced code blocks, tables, images);
4. every ``](#anchor)`` link resolves to a heading in the same file;
5. no anchor contains U+FE0F, the variation selector that silently breaks
   links when an emoji keeps a presentation suffix;
6. every relative ``*.md`` link points at a file that exists.

The slug approximation below is deliberately not a reimplementation of
GitHub's slugger: it is a drift detector. Verify a genuinely new anchor by
clicking it.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["README.md", "README.en.md", "README.ja.md", "README.ko.md"]

HEADING = re.compile(r"^(#{2,3})\s+(.*)$", re.MULTILINE)
ANCHOR_LINK = re.compile(r"\]\(#([^)]+)\)")
MD_LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)#]+\.md)(?:#[^)]*)?\)")
FENCE = re.compile(r"^```", re.MULTILINE)
TABLE_ROW = re.compile(r"^\|.*\|\s*$", re.MULTILINE)


def slug(heading: str) -> str:
    """Approximate GitHub's heading slug: drop symbols/emoji, lower, spaces to hyphens."""
    out = []
    for ch in heading.strip():
        if ch in ("\ufe0f", "\ufe0e"):  # variation selectors
            continue
        if ch == " ":
            out.append(" ")
        elif ch in "-_":
            out.append(ch)
        elif unicodedata.category(ch)[0] in ("L", "N"):
            out.append(ch.lower())
        # everything else (emoji, punctuation, brackets) is dropped
    return "".join(out).replace(" ", "-")


def main() -> int:
    problems: list[str] = []
    texts: dict[str, str] = {}
    switchers: dict[str, str] = {}
    skeletons: dict[str, list[int]] = {}
    shapes: dict[str, tuple[int, int, int]] = {}

    for name in FILES:
        path = ROOT / name
        if not path.exists():
            problems.append(f"{name}: missing")
            continue
        text = path.read_text(encoding="utf-8")
        texts[name] = text

        for line in text.splitlines():
            if all(other in line for other in ("README.md", "README.en.md", "README.ja.md", "README.ko.md")):
                switchers[name] = line.strip()
                break
        else:
            problems.append(f"{name}: no language switcher line")

        skeletons[name] = [len(m.group(1)) for m in HEADING.finditer(text)]
        shapes[name] = (
            len(FENCE.findall(text)),
            len(TABLE_ROW.findall(text)),
            text.count("!["),
        )

    if len(set(switchers.values())) > 1:
        problems.append("language switcher differs between files:")
        for name, line in switchers.items():
            problems.append(f"  {name}: {line}")

    baseline = FILES[0] if FILES[0] in skeletons else None
    if baseline:
        for name, skel in skeletons.items():
            if name == baseline:
                continue
            if skel != skeletons[baseline]:
                problems.append(
                    f"{name}: heading skeleton {skel} != {baseline} {skeletons[baseline]}"
                )
        for name, shape in shapes.items():
            if name == baseline:
                continue
            if shape != shapes[baseline]:
                problems.append(
                    f"{name}: structure (fences, table rows, images) {shape} != "
                    f"{baseline} {shapes[baseline]}"
                )

    for name, text in texts.items():
        anchors = {slug(m.group(2)) for m in HEADING.finditer(text)}
        for raw in ANCHOR_LINK.findall(text):
            if "\ufe0f" in raw:
                problems.append(f"{name}: anchor contains U+FE0F: {raw!r}")
                continue
            if raw not in anchors and raw.lstrip("-") not in {a.lstrip("-") for a in anchors}:
                problems.append(f"{name}: dead anchor #{raw}")
        for target in set(MD_LINK.findall(text)):
            if not (ROOT / target).exists():
                problems.append(f"{name}: link to missing file {target}")

    if problems:
        for line in problems:
            print(f"::error::{line}", file=sys.stderr)
        return 1

    print(f"ok: {len(texts)} READMEs in sync "
          f"({skeletons[baseline]} heading skeleton, {shapes[baseline]} structure)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
