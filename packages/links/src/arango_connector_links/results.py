"""Buffer a job's envelopes as JSONL, then stream them to StorageV2 in chunks.

Results are written to a spooled temporary file first (memory, then disk past a few MB)
and uploaded only once the operation has finished, so a failing operation never leaves
a half-uploaded result behind.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from typing import Any

from arango_connector.envelope import Envelope
from arango_connector_links._pb.integrations.link.v1.definition import internal_pb2

CHUNK_BYTES = 1024 * 1024
_SPOOL_BYTES = 8 * 1024 * 1024


class JsonlResult:
    def __init__(self) -> None:
        self._file = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)  # noqa: SIM115
        self.records = 0

    def write(self, envelope: Envelope[Any]) -> None:
        line = json.dumps(envelope.to_document(), ensure_ascii=False).encode() + b"\n"
        self._file.write(line)
        self.records += 1

    def upload_requests(
        self, job_id: str, name: str
    ) -> Iterator[internal_pb2.BatchUploadFileRequest]:
        """``BatchUploadFiles`` messages: ``job_id`` and ``name`` on the first chunk only."""
        self._file.seek(0)
        first = True
        while True:
            chunk = self._file.read(CHUNK_BYTES)
            if not chunk and not first:
                return
            if first:
                yield internal_pb2.BatchUploadFileRequest(job_id=job_id, name=name, chunk=chunk)
                first = False
            else:
                yield internal_pb2.BatchUploadFileRequest(chunk=chunk)
            if not chunk:
                return

    def close(self) -> None:
        self._file.close()
