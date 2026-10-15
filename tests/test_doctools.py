"""Docs tooling: generated tables, drift checks and wiki rendering."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal

import pytest
from pydantic import Field, SecretStr
from pydantic_settings import NoDecode

from arango_connector import doctools
from arango_connector.config import ConnectorSettings
from arango_connector.registry import SOURCE_INFO, SOURCES

REPO = doctools.REPO_ROOT


class SampleSettings(ConnectorSettings):
    sample_token: SecretStr = Field(description="A secret.")
    sample_mode: Literal["a", "b"] = Field(default="a", description="Pick | one.")
    sample_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["x", "y"], description="Some ids."
    )
    sample_window: timedelta = Field(default=timedelta(hours=24), description="How long.")
    sample_count: int = Field(default=3, ge=0, le=9, description="How many.")
    sample_flag: bool = Field(default=False, description="On or off.")
    sample_file: Path | None = Field(default=None, description="Optional file.")


def test_config_table_renders_every_setting() -> None:
    table = doctools.config_table(SampleSettings).splitlines()
    assert table[0] == "| Variable | Default | Values | Description |"
    rows = {line.split(" | ")[0].strip("| `"): line for line in table[2:]}
    assert rows["SAMPLE_TOKEN"] == "| `SAMPLE_TOKEN` | **required** | secret | A secret. |"
    assert "`a` \\| `b`" in rows["SAMPLE_MODE"] and "Pick \\| one." in rows["SAMPLE_MODE"]
    assert "`x,y`" in rows["SAMPLE_IDS"] and "comma-separated" in rows["SAMPLE_IDS"]
    assert "`24h`" in rows["SAMPLE_WINDOW"] and "duration" in rows["SAMPLE_WINDOW"]
    assert "integer ≥ 0, ≤ 9" in rows["SAMPLE_COUNT"]
    assert "`false`" in rows["SAMPLE_FLAG"]
    assert "| — | path |" in rows["SAMPLE_FILE"]


def test_settings_without_description_are_rejected() -> None:
    class Undocumented(ConnectorSettings):
        nameless: str = "x"

    with pytest.raises(ValueError, match=r"Undocumented\.nameless has no description"):
        doctools.config_table(Undocumented)


def test_every_real_setting_is_documented() -> None:
    for info in SOURCE_INFO.values():
        doctools.config_table(doctools.load_settings_class(info.settings))
    doctools.config_table(doctools.CoreSettings)


def test_every_source_has_metadata_and_a_page() -> None:
    assert set(SOURCE_INFO) == set(SOURCES)
    assert doctools.registry_gaps() == []


def test_regions_are_replaced_and_markers_kept() -> None:
    text = (
        "intro\n<!-- BEGIN GENERATED: catalogue -->\nstale\n"
        "<!-- END GENERATED: catalogue -->\noutro\n"
    )
    out = doctools.regenerate(text, Path("docs/README.md"))
    assert out.startswith("intro\n<!-- BEGIN GENERATED: catalogue -->\n| Source |")
    assert "stale" not in out
    assert "[Gmail docs](sources/gmail.md)" in out
    assert out.endswith("<!-- END GENERATED: catalogue -->\noutro\n")


def test_catalogue_links_are_relative_to_the_file() -> None:
    root_out = doctools.render_region("catalogue", Path("README.md"))
    assert "(docs/sources/gmail.md)" in root_out
    page_out = doctools.render_region("catalogue", Path("docs/sources/slack.md"))
    assert "(gmail.md)" in page_out


def test_unknown_region_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown generated region"):
        doctools.render_region("config:nope", Path("README.md"))


def test_repo_docs_are_up_to_date() -> None:
    """The committed docs match the code. Fix with `make docs-gen`."""
    assert doctools.check() == []


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    for rel in ("README.md", "docs", ".env.example", "slack_manifest.yaml"):
        src = REPO / rel
        if src.is_dir():
            shutil.copytree(src, tmp_path / rel)
        else:
            shutil.copy(src, tmp_path / rel)
    return tmp_path


def test_check_catches_stale_regions(repo_copy: Path) -> None:
    page = repo_copy / "docs/sources/gmail.md"
    page.write_text(page.read_text().replace("Seconds between polls.", "edited by hand"))
    assert any("generated region is stale" in p for p in doctools.check(repo_copy))
    doctools.generate(repo_copy)
    assert not any("stale" in p for p in doctools.check(repo_copy))


def test_check_catches_env_example_gaps(repo_copy: Path) -> None:
    env = repo_copy / ".env.example"
    env.write_text(env.read_text().replace("GMAIL_LABEL_MATCH=", "REMOVED="))
    assert ".env.example: GMAIL_LABEL_MATCH is not listed" in doctools.check(repo_copy)


def test_check_catches_broken_links_and_anchors(repo_copy: Path) -> None:
    page = repo_copy / "docs/README.md"
    page.write_text(page.read_text() + "\n[x](sources/nope.md) [y](sources/gmail.md#nope)\n")
    problems = doctools.check(repo_copy)
    assert "docs/README.md: missing file sources/nope.md" in problems
    assert "docs/README.md: missing anchor sources/gmail.md#nope" in problems


def test_links_inside_code_blocks_are_ignored(repo_copy: Path) -> None:
    page = repo_copy / "docs/README.md"
    page.write_text(page.read_text() + "\n```\n[x](missing.md)\n```\n")
    assert doctools.broken_links(repo_copy) == []


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        ("How Gmail sync works", "how-gmail-sync-works"),
        (
            "Option A: service account with domain-wide delegation",
            "option-a-service-account-with-domain-wide-delegation",
        ),
        ("Adding a new source or sink", "adding-a-new-source-or-sink"),
        ("Upgrading from schema version 3", "upgrading-from-schema-version-3"),
    ],
)
def test_slug(heading: str, anchor: str) -> None:
    assert doctools.slug(heading) == anchor


# -- wiki -------------------------------------------------------------------------------


def test_wiki_build(tmp_path: Path) -> None:
    written = doctools.build_wiki(tmp_path, repo="org/repo", sha="0123456789abcdef", root=REPO)
    names = sorted(p.name for p in written)
    assert names == sorted(
        [
            "Home.md",
            "Documentation.md",
            "Slack-source.md",
            "Gmail-source.md",
            "Jira-source.md",
            "Source-page-template.md",
            "_Sidebar.md",
            "_Footer.md",
        ]
    )
    home = (tmp_path / "Home.md").read_text()
    banner = "> Generated from [`README.md`](https://github.com/org/repo/blob/main/README.md)"
    assert home.startswith(f"{banner} at `0123456`.")
    assert "](Gmail-source#configuration)" in home
    assert "](docs/sources" not in home
    assert "[Gmail](Gmail-source)" in (tmp_path / "_Sidebar.md").read_text()


def test_wiki_link_rewriting() -> None:
    page = doctools.WikiPage(Path("docs/sources/slack.md"), "Slack-source")
    by_source = {Path("README.md"): "Home", Path("docs/sources/gmail.md"): "Gmail-source"}
    text = (
        "[a](gmail.md#setup) [b](../../README.md#retention) [c](../../slack_manifest.yaml) "
        "[d](../../src/arango_connector) [e](#local) [f](https://x.y/z)\n"
        "```\n[g](gmail.md)\n```\n"
    )
    out = doctools.rewrite_links(text, page, by_source, "https://github.com/o/r/blob/main")
    assert "[a](Gmail-source#setup)" in out
    assert "[b](Home#retention)" in out
    assert "[c](https://github.com/o/r/blob/main/slack_manifest.yaml)" in out
    assert "[d](https://github.com/o/r/tree/main/src/arango_connector)" in out
    assert "[e](#local)" in out and "[f](https://x.y/z)" in out
    assert "[g](gmail.md)" in out  # code blocks untouched
