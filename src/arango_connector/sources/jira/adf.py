"""Atlassian Document Format (ADF) to plain text. Pure, no I/O.

Jira's v3 API returns descriptions and comment bodies as ADF, a JSON tree of nodes. The
plain text goes into the payload so it can be read and searched; the ADF itself stays in
``raw``. See https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Nodes whose children each start on a new line.
_BLOCKS = frozenset(
    {
        "doc",
        "paragraph",
        "heading",
        "blockquote",
        "codeBlock",
        "panel",
        "bulletList",
        "orderedList",
        "listItem",
        "table",
        "tableRow",
        "expand",
        "nestedExpand",
        "decisionList",
        "decisionItem",
        "taskList",
        "taskItem",
        "mediaSingle",
        "mediaGroup",
    }
)


def adf_to_text(node: Any) -> str:
    """Render an ADF document (or any node) as plain text.

    Returns ``""`` for ``None``. A plain string (the v2 API's wiki markup) is returned
    unchanged. Unknown node types fall back to their children, so new ADF features
    degrade to their text rather than disappearing.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    lines: list[str] = []
    _render(node, lines, prefix="")
    text = "\n".join(lines)
    # Collapse the blank runs that nesting produces, and trim.
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def _inline(node: dict[str, Any]) -> str:
    """Text of an inline node and its inline children."""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return str(attrs.get("text") or "@" + str(attrs.get("id", "")))
    if kind == "emoji":
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if kind in ("inlineCard", "blockCard", "embedCard"):
        return str(attrs.get("url") or "")
    if kind == "date":
        timestamp = str(attrs.get("timestamp") or "")
        try:  # ADF dates are epoch milliseconds at midnight UTC
            return datetime.fromtimestamp(int(timestamp) / 1000, UTC).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return timestamp
    if kind == "status":
        return str(attrs.get("text") or "")
    return "".join(_inline(child) for child in _children(node))


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    content = node.get("content")
    return [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []


def _render(node: Any, lines: list[str], prefix: str) -> None:
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    if kind not in _BLOCKS and kind != "rule":
        lines.append(prefix + _inline(node))
        return
    if kind == "rule":
        lines.extend([prefix + "---", ""])
        return
    if kind in ("bulletList", "orderedList"):
        start = int((node.get("attrs") or {}).get("order") or 1)
        for i, item in enumerate(_children(node)):
            marker = f"{start + i}. " if kind == "orderedList" else "- "
            item_lines: list[str] = []
            _render(item, item_lines, prefix="")
            item_lines = [line for line in item_lines if line.strip()]
            for j, line in enumerate(item_lines):
                lines.append(prefix + (marker if j == 0 else " " * len(marker)) + line)
        lines.append("")
        return
    if kind == "tableRow":
        cells = []
        for cell in _children(node):
            cell_lines: list[str] = []
            _render({"type": "doc", "content": _children(cell)}, cell_lines, prefix="")
            cells.append(" ".join(line.strip() for line in cell_lines if line.strip()))
        lines.append(prefix + " | ".join(cells))
        return
    if kind == "blockquote":
        quoted: list[str] = []
        for child in _children(node):
            _render(child, quoted, prefix="")
        lines.extend(prefix + "> " + line for line in quoted if line.strip())
        lines.append("")
        return
    if kind in ("paragraph", "heading", "codeBlock", "taskItem", "decisionItem"):
        text = "".join(_inline(child) for child in _children(node))
        lines.extend(prefix + line for line in text.split("\n"))
        if kind in ("paragraph", "heading", "codeBlock"):
            lines.append("")  # blank line between blocks
        return
    for child in _children(node):
        _render(child, lines, prefix)
    if kind == "table":
        lines.append("")
