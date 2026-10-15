"""An in-process fake of the integration sidecar's LinkV1Internal gRPC service.

Enforces the real service's rules that matter to a link: atomic pickup of Pending jobs,
no status change after Cancelled, and ``job_id``/``name`` on the first upload chunk only.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from concurrent import futures
from dataclasses import dataclass, field
from typing import Any

import grpc
from google.protobuf.duration_pb2 import Duration

from arango_connector_links._pb.integrations.link.v1.definition import (
    external_pb2,
    info_pb2,
    internal_pb2,
    internal_pb2_grpc,
    job_pb2,
)
from arango_connector_links._pb.integrations.shared.v1.definition import empty_pb2

S = job_pb2


@dataclass
class FakeJob:
    id: str
    input: bytes
    timeout_seconds: float | None = None
    states: list[int] = field(default_factory=lambda: [S.JOB_STATE_PENDING])
    descriptions: list[str] = field(default_factory=lambda: [""])
    files: dict[str, bytes] = field(default_factory=dict)

    @property
    def state(self) -> int:
        return self.states[-1]

    def state_names(self) -> list[str]:
        return [S.JobState.Name(s).removeprefix("JOB_STATE_") for s in self.states]  # type: ignore[arg-type]


class FakeSidecar(internal_pb2_grpc.LinkV1InternalServicer):
    def __init__(self) -> None:
        self.jobs: dict[str, FakeJob] = {}
        self.info: info_pb2.LinkInfo | None = None
        self.unavailable_for = 0  # UpdateInfo calls to fail before succeeding
        self._lock = threading.Lock()
        self.upload_chunks: list[internal_pb2.BatchUploadFileRequest] = []

    # -- test helpers ----------------------------------------------------------------

    def submit(self, raw_input: bytes, timeout_seconds: float | None = None) -> FakeJob:
        job = FakeJob(id=str(uuid.uuid4()), input=raw_input, timeout_seconds=timeout_seconds)
        self.jobs[job.id] = job
        return job

    def cancel(self, job_id: str) -> None:
        self.jobs[job_id].states.append(S.JOB_STATE_CANCELLED)
        self.jobs[job_id].descriptions.append("cancelled by AI tool")

    def _proto(self, job: FakeJob) -> job_pb2.Job:
        statuses = [
            job_pb2.JobStatus(state=s, description=d)  # type: ignore[arg-type]
            for s, d in zip(reversed(job.states), reversed(job.descriptions), strict=True)
        ]
        built = job_pb2.Job(id=job.id, link_id="test-link", statuses=statuses, input=job.input)
        if job.timeout_seconds is not None:
            duration = Duration()
            duration.FromMilliseconds(int(job.timeout_seconds * 1000))
            built.timeout.CopyFrom(duration)
        if job.state == S.JOB_STATE_COMPLETED:
            built.result = f"/links/test-link/{job.id}/"
        return built

    # -- LinkV1Internal ----------------------------------------------------------------

    def UpdateInfo(self, request: info_pb2.LinkInfo, context: Any) -> empty_pb2.Empty:
        if self.unavailable_for > 0:
            self.unavailable_for -= 1
            context.abort(grpc.StatusCode.UNAVAILABLE, "sidecar starting")
        self.info = request
        return empty_pb2.Empty()

    def PickUpJob(self, request: empty_pb2.Empty, context: Any) -> internal_pb2.PickUpJobResponse:
        with self._lock:
            for job in self.jobs.values():
                if job.state == S.JOB_STATE_PENDING:
                    job.states.append(S.JOB_STATE_SCHEDULED)
                    job.descriptions.append("")
                    return internal_pb2.PickUpJobResponse(id=job.id)
        return internal_pb2.PickUpJobResponse()

    def GetJob(self, request: external_pb2.GetJobRequest, context: Any) -> job_pb2.Job:
        job = self.jobs.get(request.id)
        if job is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "no such job")
        return self._proto(job)  # type: ignore[arg-type]

    def UpdateJobStatus(
        self, request: internal_pb2.UpdateJobStatusRequest, context: Any
    ) -> internal_pb2.UpdateJobStatusResponse:
        job = self.jobs[request.id]
        if job.state in (S.JOB_STATE_CANCELLED, S.JOB_STATE_COMPLETED, S.JOB_STATE_FAILED):
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "invalid transition")
        job.states.append(request.status.state)
        job.descriptions.append(request.status.description)
        return internal_pb2.UpdateJobStatusResponse(job=self._proto(job))

    def BatchUploadFiles(
        self, request_iterator: Iterator[internal_pb2.BatchUploadFileRequest], context: Any
    ) -> internal_pb2.BatchUploadFilesResponse:
        current: tuple[str, str] | None = None
        sizes: dict[tuple[str, str], int] = {}
        for request in request_iterator:
            self.upload_chunks.append(request)
            if request.name:
                if not request.job_id:
                    context.abort(grpc.StatusCode.INVALID_ARGUMENT, "job_id must be set")
                current = (request.job_id, request.name)
                self.jobs[request.job_id].files[request.name] = b""
            if current is None:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "name must be set first")
            job_id, name = current  # type: ignore[misc]
            self.jobs[job_id].files[name] += request.chunk
            sizes[current] = sizes.get(current, 0) + len(request.chunk)  # type: ignore[index]
        return internal_pb2.BatchUploadFilesResponse(
            files=[internal_pb2.UploadFileResponse(bytes=n) for n in sizes.values()]
        )


class SidecarServer:
    """Runs a FakeSidecar on a free localhost port."""

    def __init__(self) -> None:
        self.sidecar = FakeSidecar()
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        internal_pb2_grpc.add_LinkV1InternalServicer_to_server(self.sidecar, self.server)
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.address = f"127.0.0.1:{port}"
        self.server.start()
        self.channel = grpc.insecure_channel(self.address)
        self.stub = internal_pb2_grpc.LinkV1InternalStub(self.channel)

    def close(self) -> None:
        self.channel.close()
        self.server.stop(grace=None)
