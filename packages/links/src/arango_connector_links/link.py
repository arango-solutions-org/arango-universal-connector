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
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, TypeAdapter

from arango_connector.envelope import Envelope

RESULT_FILE = "result.0000000.jsonl"


class JobError(Exception):
    """A job failed for a reason worth telling the caller (bad scope, not found, ...).

    The message becomes the job's ``Failed`` description, so it must not contain secrets.
    """


class JobCancelled(Exception):
    """The job was cancelled, or ran past its timeout, while running."""


@dataclass
class JobContext:
    """Passed to ``Link.run``; lets long operations stop promptly."""

    job_id: str
    deadline: float | None = None  # time.monotonic() value
    cancelled: bool = False
    _check: Any = field(default=None, repr=False)

    def check(self) -> None:
        """Raise ``JobCancelled`` if the job was cancelled or is past its timeout."""
        if self.deadline is not None and time.monotonic() > self.deadline:
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

    def input_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = TypeAdapter(self.input_type).json_schema()
        return schema

    def output_schema(self) -> dict[str, Any]:
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


def model_example(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)
