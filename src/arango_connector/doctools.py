"""Docs tooling: generate reference tables from code, check for drift, build the wiki.

``docs/`` (plus ``README.md``) is the source of truth, reviewed in PRs. This module:

* ``gen``: rewrites the generated regions in those files from the settings classes and
  the source registry, so configuration tables can never drift from the code.
* ``check``: fails if a generated region is stale, a setting is missing from
  ``.env.example``, or a relative link or anchor is broken. Run in CI.
* ``wiki <out_dir>``: renders the docs as GitHub wiki pages (links rewritten, sidebar,
  "generated" banner). CI pushes the result to the wiki on every merge to ``main``.

Usage: ``python -m arango_connector.doctools {gen,check,wiki} [...]``
"""

from __future__ import annotations

import argparse
import importlib
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

from arango_connector.config import ConnectorSettings, CoreSettings
from arango_connector.registry import SOURCE_INFO, SOURCES

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_DIR = Path("docs/sources")

# Files whose generated regions are maintained, relative to the repo root.
GENERATED_FILES = (
    Path("README.md"),
    Path("docs/README.md"),
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
        for attr, symbol in (("ge", "≥"), ("gt", ">")):
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
        "| Source | `SOURCE=` | Kinds | Delivery | What it ingests | Docs |",
        "|---|---|---|---|---|---|",
    ]
    for name, info in SOURCE_INFO.items():
        kinds = ", ".join(f"`{k}`" for k in info.kinds)
        rows.append(
            f"| {info.title} | `{name}` | {kinds} | {info.delivery} | {_cell(info.summary)} "
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
    if kind == "source-settings":
        return per_source_settings(_doc_link_from(file))
    raise ValueError(f"{file}: unknown generated region {region_id!r}")


def regenerate(text: str, file: Path) -> str:
    def replace(match: re.Match[str]) -> str:
        body = render_region(match.group("id"), file)
        return f"{match.group(1)}{body}{match.group(4)}"

    return _REGION_RE.sub(replace, text)


def generate(root: Path = REPO_ROOT, *, write: bool = True) -> list[Path]:
    """Rewrite generated regions. Returns the files that changed (or would change)."""
    changed = []
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
    parser = argparse.ArgumentParser(prog="python -m arango_connector.doctools")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gen", help="rewrite generated regions in the docs")
    sub.add_parser("check", help="fail on stale regions, .env.example gaps or broken links")
    wiki = sub.add_parser("wiki", help="render the docs as GitHub wiki pages")
    wiki.add_argument("out", type=Path)
    wiki.add_argument("--repo", default="arango-solutions/arango-universal-connector")
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
