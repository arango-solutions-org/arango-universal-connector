"""A link with no external source, for checking a deployment end to end.

Returns synthetic message documents, so an operator can confirm the platform wiring
(registration, job queue, StorageV2 results) before configuring a real source.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arango_connector.envelope import Envelope, Scope, make_key
from arango_connector.payloads import MessagePayload
from arango_connector_links.link import Example, JobContext, Link


class Echo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["echo"] = "echo"
    text: str = Field(default="hello from the connector", max_length=10_000)
    count: int = Field(default=1, ge=0, le=10_000, description="Documents to return.")


class SelftestLink(Link[Echo]):
    name = "selftest"
    description = (
        "Diagnostic link with no external source: returns `count` message documents "
        "containing `text`, to check that jobs and results work on this platform."
    )
    tags = ("diagnostic", "test")
    input_type = Echo
    examples = (
        Example("echo", {"operation": "echo", "text": "ping", "count": 2}, "Two documents."),
    )

    def run(self, job_input: Echo, ctx: JobContext) -> Iterator[Envelope[Any]]:
        now = datetime.now(UTC)
        for n in range(job_input.count):
            ctx.check()
            yield Envelope[MessagePayload](
                key=make_key("selftest", ctx.job_id, str(n)),
                source="selftest",
                source_id=str(n),
                scope=Scope(container_id=ctx.job_id),
                created_at=now,
                payload=MessagePayload(author_type="system", text=job_input.text),
                raw={},
            )
