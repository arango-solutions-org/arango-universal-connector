"""Unit tests for the Atlassian Document Format -> plain text rendering."""

from __future__ import annotations

from typing import Any

from arango_connector.sources.jira.adf import adf_to_text


def text(value: str, **attrs: Any) -> dict[str, Any]:
    return {"type": "text", "text": value, **attrs}


def para(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(content)}


def doc(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(content)}


def item(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "listItem", "content": list(content)}


def test_empty_values() -> None:
    assert adf_to_text(None) == ""
    assert adf_to_text(doc()) == ""
    assert adf_to_text({"type": "doc"}) == ""


def test_plain_string_passes_through() -> None:
    assert adf_to_text("h1. wiki markup") == "h1. wiki markup"


def test_paragraphs_are_separated_by_a_blank_line() -> None:
    assert adf_to_text(doc(para(text("one")), para(text("two")))) == "one\n\ntwo"


def test_marks_are_dropped_and_inline_nodes_rendered() -> None:
    rendered = adf_to_text(
        doc(
            para(
                text("Hi ", marks=[{"type": "strong"}]),
                {"type": "mention", "attrs": {"id": "5b10", "text": "@Ada"}},
                text(" "),
                {"type": "emoji", "attrs": {"shortName": ":tada:", "text": "🎉"}},
                {"type": "hardBreak"},
                {"type": "inlineCard", "attrs": {"url": "https://example.com/x"}},
                text(" "),
                {"type": "status", "attrs": {"text": "BLOCKED", "color": "red"}},
            )
        )
    )
    assert rendered == "Hi @Ada 🎉\nhttps://example.com/x BLOCKED"


def test_mention_without_text_falls_back_to_id() -> None:
    assert adf_to_text(doc(para({"type": "mention", "attrs": {"id": "5b10"}}))) == "@5b10"


def test_lists_nest_and_number() -> None:
    rendered = adf_to_text(
        doc(
            {
                "type": "orderedList",
                "attrs": {"order": 3},
                "content": [
                    item(para(text("three"))),
                    item(
                        para(text("four")),
                        {"type": "bulletList", "content": [item(para(text("nested")))]},
                    ),
                ],
            },
            para(text("after")),
        )
    )
    assert rendered == "3. three\n4. four\n   - nested\n\nafter"


def test_heading_quote_code_rule_and_table() -> None:
    cell = {"type": "tableCell", "content": [para(text("B"))]}
    header = {"type": "tableHeader", "content": [para(text("A"))]}
    rendered = adf_to_text(
        doc(
            {"type": "heading", "attrs": {"level": 2}, "content": [text("Steps")]},
            {"type": "blockquote", "content": [para(text("quoted"))]},
            {"type": "codeBlock", "attrs": {"language": "py"}, "content": [text("x = 1\ny = 2")]},
            {"type": "rule"},
            {"type": "table", "content": [{"type": "tableRow", "content": [header, cell]}]},
        )
    )
    assert rendered == "Steps\n\n> quoted\n\nx = 1\ny = 2\n\n---\n\nA | B"


def test_unknown_nodes_keep_their_text() -> None:
    future = {"type": "somethingNew", "content": [para(text("still here"))]}
    assert adf_to_text(doc(future)) == "still here"


def test_malformed_content_is_ignored() -> None:
    assert adf_to_text(doc(para(text("ok")), "junk", {"content": "nope"})) == "ok"  # type: ignore[arg-type]
