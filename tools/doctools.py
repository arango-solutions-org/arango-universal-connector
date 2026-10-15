"""Docs tooling: generate reference tables from code, check for drift, build the wiki.

``docs/`` (plus ``README.md``) is the source of truth, reviewed in PRs. This module:

* ``gen``: rewrites the generated regions in those files from the settings classes and
  the source registry, so configuration tables can never drift from the code.
* ``check``: fails if a generated region is stale, a setting is missing from
  ``.env.example``, or a relative link or anchor is broken. Run in CI.
* ``wiki <out_dir>``: renders the docs as GitHub wiki pages (links rewritten, sidebar,
  "generated" banner). CI pushes the result to the wiki on every merge to ``main``.

Usage: ``python tools/doctools.py {gen,check,wiki} [...]``
"""

from __future__ import annotations

import argparse
import importlib
import json
import posixpath
import re
import sys
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from arango_connector.catalog import SOURCE_INFO
from arango_connector.config import ConnectorSettings
from arango_connector_standalone.config import CoreSettings
from arango_connector_standalone.registry import SOURCES

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_SCHEMAS = Path("charts/arango-connector-link/schemas")
SOURCES_DIR = Path("docs/sources")

# Files whose generated regions are maintained, relative to the repo root.
GENERATED_FILES = (
    Path("README.md"),
    Path("docs/README.md"),
    Path("docs/links.md"),
    *(SOURCES_DIR / info.doc for info in SOURCE_INFO.values()),
)

_REGION_RE = re.compile(
    r"(<!-- BEGIN GENERATED: (?P<id>[\w:-]+) -->\n)(?P<body>.*?)(<!-- END GENERATED: (?P=id) -->)",
    re.DOTALL,
)
_FENCE_RE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_HEADING_RE = re.compile(r"^#{1,6} (.+)$", re.MULTILINE)


# -- configuration tables ---------------------------------------------------------------


def load_settings_class(path: str) -> type[ConnectorSettings]:
    module, _, name = path.partition(":")
    cls = getattr(importlib.import_module(module), name)
    if not (isinstance(cls, type) and issubclass(cls, ConnectorSettings)):
        raise TypeError(f"{path} is not a settings class")
    return cls


def _unwrap(annotation: Any) -> Any:
    """Strip Annotated[...] and Optional[...] down to the underlying type."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _unwrap(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _unwrap(args[0])
    return annotation


def _values(field: FieldInfo) -> str:
    """The values a setting accepts, for the Values column."""
    annotation = _unwrap(field.annotation)
    if typing.get_origin(annotation) is Literal:
        return " \\| ".join(f"`{v}`" for v in typing.get_args(annotation))
    if annotation is bool:
        return "`true` \\| `false`"
    bounds = []
    for meta in field.metadata:
        for attr, symbol in (("ge", "≥"), ("gt", ">"), ("le", "≤")):
            value = getattr(meta, attr, None)
            if value is not None:
                bounds.append(f"{symbol} {value}")
    if annotation in (int, float):
        name = "integer" if annotation is int else "number"
        return f"{name} {', '.join(bounds)}".strip()
    if annotation is SecretStr:
        return "secret"
    if annotation is timedelta:
        return "duration"
    if annotation is Path:
        return "path"
    if typing.get_origin(annotation) in (list, set):
        return "comma-separated"
    return "text"


def _format_duration(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        # Under two days, hours read better ("24h" rather than "1d").
        if seconds % size == 0 and not (unit == "d" and seconds < 2 * 86400):
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _default(field: FieldInfo) -> str:
    if field.is_required():
        return "**required**"
    value = (
        field.default_factory()  # type: ignore[call-arg]
        if field.default_factory is not None
        else field.default
    )
    if value is None or value is PydanticUndefined:
        return "—"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, timedelta):
        return f"`{_format_duration(value)}`"
    if isinstance(value, Path) and not value.is_absolute():
        return f"`./{value.as_posix()}`"
    if isinstance(value, list | set | tuple):
        return f"`{','.join(str(v) for v in value)}`"
    return f"`{value}`"


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def config_table(cls: type[ConnectorSettings]) -> str:
    """A Markdown table of every setting: variable, default, accepted values, description."""
    rows = ["| Variable | Default | Values | Description |", "|---|---|---|---|"]
    for name, field in cls.model_fields.items():
        if not field.description:
            raise ValueError(f"{cls.__name__}.{name} has no description")
        rows.append(
            f"| `{name.upper()}` | {_default(field)} | {_values(field)} | "
            f"{_cell(field.description)} |"
        )
    return "\n".join(rows) + "\n"


def env_vars(cls: type[ConnectorSettings]) -> list[str]:
    return [name.upper() for name in cls.model_fields]


# -- source catalogue -------------------------------------------------------------------


def catalogue_table(doc_link: Callable[[str], str]) -> str:
    """The table of sources. ``doc_link`` maps a source's page name to a link target."""
    rows = [
        "| Source | `SOURCE=` | Kinds | Standalone delivery | As a Link | What it ingests | Docs |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, info in SOURCE_INFO.items():
        kinds = ", ".join(f"`{k}`" for k in info.kinds)
        rows.append(
            f"| {info.title} | `{name}` | {kinds} | {info.delivery} | {link_operations(name)} "
            f"| {_cell(info.summary)} "
            f"| [{info.title} docs]({doc_link(info.doc)}) |"
        )
    return "\n".join(rows) + "\n"


def per_source_settings(doc_link: Callable[[str], str]) -> str:
    rows = []
    for name, info in SOURCE_INFO.items():
        prefix = f"{name.upper()}_*"
        rows.append(
            f"- `{prefix}`: required only when `SOURCE={name}`. See "
            f"[{info.title} configuration]({doc_link(info.doc)}#configuration)."
        )
    return "\n".join(rows) + "\n"


# -- links ---------------------------------------------------------------------------


def _schema_type(prop: dict[str, Any]) -> tuple[str, bool]:
    """A readable type for one JSON Schema property, and whether it accepts null."""
    options = prop.get("anyOf") or [prop]
    nullable = any(o.get("type") == "null" for o in options)
    parts = []
    for option in options:
        if option.get("type") == "null":
            continue
        if "const" in option:
            parts.append(f"`{option['const']}`")
        elif "enum" in option:
            parts.append(" \\| ".join(f"`{v}`" for v in option["enum"]))
        elif option.get("type") == "array":
            item = option.get("items", {})
            parts.append(f"list of {item.get('type', 'values')}")
        elif option.get("format") in ("date", "date-time"):
            parts.append(option["format"])
        else:
            kind = option.get("type", "value")
            bounds = [
                f"{symbol} {option[key]}"
                for key, symbol in (("minimum", "≥"), ("maximum", "≤"), ("minLength", "length ≥"))
                if key in option
            ]
            parts.append(" ".join([kind, ", ".join(bounds)]).strip())
    return " or ".join(parts) or "value", nullable


def _operations(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """``(operation name, model schema)`` pairs, for one model or a discriminated union."""
    defs = schema.get("$defs", {})
    mapping = (schema.get("discriminator") or {}).get("mapping")
    if mapping:
        return [(name, defs[ref.rsplit("/", 1)[-1]]) for name, ref in mapping.items()]
    operation = schema.get("properties", {}).get("operation", {})
    return [(str(operation.get("const") or operation.get("default") or "run"), schema)]


def link_reference(name: str) -> str:
    """Markdown for one link: its operations, their inputs, and example jobs."""
    from arango_connector_links.main import link_types

    cls = link_types()[name]
    lines = [f"{cls.description}", "", f"Tags: {', '.join(f'`{t}`' for t in cls.tags)}.", ""]
    examples = {e.input.get("operation"): e for e in cls.examples}
    for operation, model in _operations(cls.input_schema()):
        lines.append(f"**`{operation}`**: {_cell(model.get('description', ''))}")
        lines.append("")
        lines.append("| Field | Required | Default | Values | Description |")
        lines.append("|---|---|---|---|---|")
        required = set(model.get("required", []))
        for field, prop in model.get("properties", {}).items():
            if field == "operation":
                continue
            kind, _ = _schema_type(prop)
            default = prop.get("default")
            shown = "—" if default is None else f"`{json.dumps(default)}`"
            lines.append(
                f"| `{field}` | {'yes' if field in required else 'no'} | {shown} | {_cell(kind)} "
                f"| {_cell(prop.get('description', ''))} |"
            )
        example = examples.get(operation)
        if example is not None:
            lines += [
                "",
                f"Example: {_cell(example.output)}",
                "",
                "```json",
                json.dumps(example.input),
                "```",
            ]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def link_operations(name: str) -> str:
    from arango_connector_links.main import link_types

    cls = link_types().get(name)
    if cls is None:
        return "—"
    return ", ".join(f"`{op}`" for op, _ in _operations(cls.input_schema()))


# -- generated regions ------------------------------------------------------------------


def _doc_link_from(file: Path) -> Callable[[str], str]:
    """Relative link from ``file`` to a page in docs/sources/."""
    return lambda doc: posixpath.relpath((SOURCES_DIR / doc).as_posix(), file.parent.as_posix())


def render_region(region_id: str, file: Path) -> str:
    kind, _, arg = region_id.partition(":")
    if kind == "config":
        if arg == "core":
            return config_table(CoreSettings)
        if arg in SOURCE_INFO:
            return config_table(load_settings_class(SOURCE_INFO[arg].settings))
    if kind == "catalogue":
        return catalogue_table(_doc_link_from(file))
    if kind == "link":
        return link_reference(arg)
    if kind == "source-settings":
        return per_source_settings(_doc_link_from(file))
    raise ValueError(f"{file}: unknown generated region {region_id!r}")


def regenerate(text: str, file: Path) -> str:
    def replace(match: re.Match[str]) -> str:
        body = render_region(match.group("id"), file)
        return f"{match.group(1)}{body}{match.group(4)}"

    return _REGION_RE.sub(replace, text)


def chart_schemas() -> dict[Path, str]:
    """The Helm chart's per-link definitions, from the link classes themselves."""
    from arango_connector_links.main import link_types

    files = {}
    for name, cls in sorted(link_types().items()):
        info = {
            "description": cls.description,
            "tags": list(cls.tags),
            "input_schema": cls.input_schema(),
        }
        files[CHART_SCHEMAS / f"{name}.json"] = json.dumps(info, indent=2, sort_keys=True) + "\n"
    return files


def generate(root: Path = REPO_ROOT, *, write: bool = True) -> list[Path]:
    """Rewrite generated regions and files. Returns those that changed (or would change)."""
    changed = []
    for rel, content in chart_schemas().items():
        path = root / rel
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            changed.append(rel)
            if write:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
    for rel in GENERATED_FILES:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        updated = regenerate(text, rel)
        if updated != text:
            changed.append(rel)
            if write:
                path.write_text(updated, encoding="utf-8")
    return changed


# -- checks -----------------------------------------------------------------------------


def slug(heading: str) -> str:
    """GitHub's heading anchor: lower-case, punctuation dropped, spaces to hyphens."""
    heading = re.sub(r"<[^>]+>", "", heading).strip().lower()
    heading = re.sub(r"[^\w\- ]", "", heading)
    return heading.replace(" ", "-")


def _without_code(text: str) -> str:
    return _FENCE_RE.sub("", text)


def anchors(text: str) -> set[str]:
    return {slug(h) for h in _HEADING_RE.findall(_without_code(text))}


def markdown_files(root: Path) -> list[Path]:
    return [Path("README.md"), *sorted(p.relative_to(root) for p in (root / "docs").rglob("*.md"))]


def broken_links(root: Path = REPO_ROOT) -> list[str]:
    problems = []
    for rel in markdown_files(root):
        text = _without_code((root / rel).read_text(encoding="utf-8"))
        for target in _LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path, _, anchor = target.partition("#")
            dest = (root / rel.parent / path).resolve() if path else (root / rel).resolve()
            if not dest.exists():
                problems.append(f"{rel}: missing file {target}")
            elif (
                anchor
                and dest.suffix == ".md"
                and anchor not in anchors(dest.read_text(encoding="utf-8"))
            ):
                problems.append(f"{rel}: missing anchor {target}")
    return problems


def env_example_gaps(root: Path = REPO_ROOT) -> list[str]:
    text = (root / ".env.example").read_text(encoding="utf-8")
    present = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))
    classes: list[type[ConnectorSettings]] = [
        CoreSettings,
        *(load_settings_class(i.settings) for i in SOURCE_INFO.values()),
    ]
    return [
        f".env.example: {var} is not listed"
        for cls in classes
        for var in env_vars(cls)
        if var not in present
    ]


def registry_gaps() -> list[str]:
    problems = [
        f"registry: SOURCES[{n!r}] has no SOURCE_INFO" for n in SOURCES if n not in SOURCE_INFO
    ]
    problems += [
        f"registry: SOURCE_INFO[{n!r}] has no docs page docs/sources/{i.doc}"
        for n, i in SOURCE_INFO.items()
        if not (REPO_ROOT / SOURCES_DIR / i.doc).is_file()
    ]
    return problems


def check(root: Path = REPO_ROOT) -> list[str]:
    problems = [
        f"{p}: generated region is stale; run `make docs-gen`" for p in generate(root, write=False)
    ]
    return problems + registry_gaps() + env_example_gaps(root) + broken_links(root)


# -- wiki -------------------------------------------------------------------------------


@dataclass(frozen=True)
class WikiPage:
    source: Path
    name: str  # wiki page name; the file is <name>.md and links use the name


def wiki_pages(root: Path = REPO_ROOT) -> list[WikiPage]:
    pages = [WikiPage(Path("README.md"), "Home"), WikiPage(Path("docs/README.md"), "Documentation")]
    for info in SOURCE_INFO.values():
        pages.append(WikiPage(SOURCES_DIR / info.doc, f"{info.title}-source"))
    if (root / SOURCES_DIR / "_template.md").is_file():
        pages.append(WikiPage(SOURCES_DIR / "_template.md", "Source-page-template"))
    for source, name in (
        (Path("docs/links.md"), "Links"),
        (Path("docs/design/links.md"), "Links-design"),
    ):
        if (root / source).is_file():
            pages.append(WikiPage(source, name))
    return pages


def rewrite_links(text: str, page: WikiPage, by_source: dict[Path, str], blob: str) -> str:
    """Point links at wiki pages, or at GitHub for anything that isn't a wiki page."""

    def fix(match: re.Match[str]) -> str:
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return match.group(0)
        path, _, anchor = target.partition("#")
        resolved = Path(_normalise(page.source.parent / path))
        suffix = f"#{anchor}" if anchor else ""
        if resolved in by_source:
            return f"]({by_source[resolved]}{suffix})"
        kind = "tree" if not resolved.suffix else "blob"
        return f"]({blob.replace('/blob/', f'/{kind}/')}/{resolved.as_posix()}{suffix})"

    parts = []
    last = 0
    for fence in _FENCE_RE.finditer(text):  # never touch links inside code blocks
        parts.append(_LINK_RE.sub(fix, text[last : fence.start()]))
        parts.append(fence.group(0))
        last = fence.end()
    parts.append(_LINK_RE.sub(fix, text[last:]))
    return "".join(parts)


def _normalise(path: Path) -> str:
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def build_wiki(
    out: Path, *, repo: str, ref: str = "main", sha: str = "", root: Path = REPO_ROOT
) -> list[Path]:
    """Render the docs as wiki pages into ``out``. Returns the files written."""
    out.mkdir(parents=True, exist_ok=True)
    blob = f"https://github.com/{repo}/blob/{ref}"
    pages = wiki_pages(root)
    by_source = {p.source: p.name for p in pages}
    version = f" at `{sha[:7]}`" if sha else ""
    written = []
    for page in pages:
        text = (root / page.source).read_text(encoding="utf-8")
        banner = (
            f"> Generated from [`{page.source.as_posix()}`]({blob}/{page.source.as_posix()})"
            f"{version}. Edit the file in the repo; changes made here are overwritten.\n\n"
        )
        body = rewrite_links(text, page, by_source, blob)
        target = out / f"{page.name}.md"
        target.write_text(banner + body, encoding="utf-8")
        written.append(target)

    sources = "\n".join(f"  - [{i.title}]({i.title}-source)" for i in SOURCE_INFO.values())
    sidebar = (
        "**Arango Universal Connector**\n\n"
        "- [Home](Home)\n"
        "- [Documentation](Documentation)\n"
        f"- Sources\n{sources}\n"
        "- [Links](Links)\n"
        "  - [Design](Links-design)\n"
        "- [Adding a source](Source-page-template)\n"
    )
    footer = (
        f"Generated from `docs/` in [{repo}](https://github.com/{repo}){version}. "
        "Do not edit the wiki directly.\n"
    )
    for name, content in (("_Sidebar", sidebar), ("_Footer", footer)):
        path = out / f"{name}.md"
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


# -- CLI --------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/doctools.py")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gen", help="rewrite generated regions in the docs")
    sub.add_parser("check", help="fail on stale regions, .env.example gaps or broken links")
    wiki = sub.add_parser("wiki", help="render the docs as GitHub wiki pages")
    wiki.add_argument("out", type=Path)
    wiki.add_argument("--repo", default="arango-solutions-org/arango-universal-connector")
    wiki.add_argument("--ref", default="main")
    wiki.add_argument("--sha", default="")
    args = parser.parse_args(argv)

    if args.command == "gen":
        changed = generate()
        print("\n".join(f"updated {p}" for p in changed) or "docs already up to date")
        return 0
    if args.command == "check":
        problems = check()
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"docs check: {len(problems)} problem(s)")
        return 1 if problems else 0
    written = build_wiki(args.out, repo=args.repo, ref=args.ref, sha=args.sha)
    print(f"wrote {len(written)} wiki pages to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
