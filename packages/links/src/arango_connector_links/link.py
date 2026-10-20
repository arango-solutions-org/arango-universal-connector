"""What a link is: its declared tool definition and the operations it runs.

A link registers its definition with the platform (``UpdateInfo``): a description, tags,
an input JSON Schema, an output schema and examples. AI tools read these to submit jobs.
Each job's input is validated against ``input_model`` and handed to ``run``, which yields
the envelopes that become the job's JSONL result.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import TypeAdapter

from arango_connector.envelope import Envelope

RESULT_FILE = "result.0000000.jsonl"


class JobError(Exception):
    """A job failed for a reason worth telling the caller (bad scope, not found, ...).

    The message becomes the job's ``Failed`` description, so it must not contain secrets.
    """


class JobCancelled(Exception):
    """The job was cancelled, ran past its timeout, or the link is shutting down."""


@dataclass
class JobContext:
    """Passed to ``Link.run``; lets long operations stop promptly."""

    job_id: str
    deadline: float | None = None  # a clock() value
    cancelled: bool = False
    clock: Callable[[], float] = time.monotonic
    _check: Any = field(default=None, repr=False)
    _stopping: Callable[[], bool] | None = field(default=None, repr=False)

    def check(self) -> None:
        """Raise ``JobCancelled`` if the link is stopping, or the job was cancelled or is
        past its timeout."""
        if self._stopping is not None and self._stopping():
            # Jobs can't be put back in the queue (UpdateJobStatus allows Running,
            # Completed or Failed), so a restart fails the job and the caller resubmits.
            raise JobCancelled("link restarting; resubmit the job")
        if self.deadline is not None and self.clock() > self.deadline:
            raise JobCancelled("timed out")
        if self._check is not None and self._check():
            self.cancelled = True
            raise JobCancelled("cancelled")


@dataclass(frozen=True)
class Example:
    name: str
    input: dict[str, Any]
    output: str


class Link[I](ABC):
    """A source exposed as an ArangoDB Platform Link."""

    name: ClassVar[str]
    description: ClassVar[str]
    tags: ClassVar[tuple[str, ...]]
    # A pydantic model, or a discriminated union of models, for the job input.
    input_type: ClassVar[Any]
    examples: ClassVar[tuple[Example, ...]] = ()

    def parse_input(self, raw: bytes) -> I:
        """Validate a job's input; raises ``JobError`` with a readable message."""
        try:
            data = json.loads(raw or b"{}")
        except ValueError as exc:
            raise JobError(f"input is not valid JSON: {exc}") from None
        try:
            parsed: I = TypeAdapter(self.input_type).validate_python(data)
        except ValueError as exc:
            raise JobError(f"invalid input: {exc}") from None
        return parsed

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        schema: dict[str, Any] = TypeAdapter(cls.input_type).json_schema()
        return schema

    @classmethod
    def output_schema(cls) -> dict[str, Any]:
        """One ``Envelope`` per JSONL line."""
        return {
            "type": "string",
            "contentMediaType": "application/jsonl",
            "description": "One Envelope (a JSON object) per line.",
            "contentSchema": Envelope.model_json_schema(by_alias=True),
        }

    @abstractmethod
    def run(self, job_input: I, ctx: JobContext) -> Iterator[Envelope[Any]]:
        """Run one job. Call ``ctx.check()`` between items so cancellation is prompt."""
