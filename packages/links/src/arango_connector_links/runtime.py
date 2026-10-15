"""The Links job loop: register the link, then claim, run and report jobs.

Follows the documented contract (kube-arangodb Links, ``LinkV1Internal``):

1. ``UpdateInfo`` with the link's definition, retried until the sidecar answers.
2. ``PickUpJob`` (atomic Pending -> Scheduled), waiting a little when the queue is empty.
3. ``GetJob``; ``RUNNING``; run the operation; ``BatchUploadFiles`` the JSONL result;
   ``COMPLETED``, or ``FAILED`` with a description.

Cancellation is noticed by re-reading the job between records; a cancelled job is left
alone (the platform rejects status changes after ``CANCELLED``). The job's ``timeout``
is enforced the same way and fails the job.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import grpc

from arango_connector_links._pb.integrations.link.v1.definition import (
    external_pb2,
    info_pb2,
    internal_pb2,
    internal_pb2_grpc,
    job_pb2,
)
from arango_connector_links._pb.integrations.shared.v1.definition import empty_pb2
from arango_connector_links.link import RESULT_FILE, JobCancelled, JobContext, JobError, Link
from arango_connector_links.results import JsonlResult

logger = logging.getLogger(__name__)

RUNNING = job_pb2.JOB_STATE_RUNNING
COMPLETED = job_pb2.JOB_STATE_COMPLETED
FAILED = job_pb2.JOB_STATE_FAILED
CANCELLED = job_pb2.JOB_STATE_CANCELLED

_MAX_DESCRIPTION = 1000
_REGISTER_BACKOFF_MAX = 30.0


class LinkRuntime:
    def __init__(
        self,
        link: Link[Any],
        stub: internal_pb2_grpc.LinkV1InternalStub,
        *,
        idle_wait: float = 5.0,
        cancel_check_interval: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.link = link
        self.stub = stub
        self.idle_wait = idle_wait
        self.cancel_check_interval = cancel_check_interval
        self._clock = clock
        self._stopped = threading.Event()

    # -- lifecycle -----------------------------------------------------------------

    def stop(self) -> None:
        self._stopped.set()

    @property
    def stopping(self) -> bool:
        return self._stopped.is_set()

    def link_info(self) -> info_pb2.LinkInfo:
        link = self.link
        return info_pb2.LinkInfo(
            description=link.description,
            tags=list(link.tags),
            input_schema=json.dumps(link.input_schema()),
            output_schema=json.dumps(link.output_schema()),
            examples=[
                info_pb2.LinkExample(name=e.name, input=json.dumps(e.input), output=e.output)
                for e in link.examples
            ],
            result_files=[RESULT_FILE],
        )

    def register(self) -> bool:
        """Send the link definition, retrying until it is accepted or we are stopped."""
        delay = 1.0
        info = self.link_info()
        while not self.stopping:
            try:
                self.stub.UpdateInfo(info)
            except grpc.RpcError as exc:
                logger.warning(
                    "link_register_retry link=%s code=%s wait=%ss",
                    self.link.name,
                    _code(exc),
                    delay,
                )
                self._stopped.wait(delay)
                delay = min(delay * 2, _REGISTER_BACKOFF_MAX)
                continue
            logger.info("link_registered link=%s", self.link.name)
            return True
        return False

    def run_forever(self) -> None:
        if not self.register():
            return
        logger.info("link_ready link=%s", self.link.name)
        while not self.stopping:
            try:
                worked = self.run_once()
            except grpc.RpcError as exc:
                logger.warning("link_queue_unavailable code=%s", _code(exc))
                worked = False
            if not worked:
                self._stopped.wait(self.idle_wait)
        logger.info("link_stopped link=%s", self.link.name)

    # -- jobs ----------------------------------------------------------------------

    def run_once(self) -> bool:
        """Claim and run one job. Returns ``False`` when the queue was empty."""
        picked = self.stub.PickUpJob(empty_pb2.Empty())
        if not picked.HasField("id") or not picked.id:
            return False
        self.process(picked.id)
        return True

    def process(self, job_id: str) -> None:
        job = self.stub.GetJob(external_pb2.GetJobRequest(id=job_id))
        if _state(job) == CANCELLED:
            logger.info("job_skipped job=%s reason=cancelled", job_id)
            return
        self._set_status(job_id, RUNNING, "running")
        started = self._clock()
        deadline = (
            started + job.timeout.ToTimedelta().total_seconds() if job.HasField("timeout") else None
        )
        ctx = JobContext(job_id=job_id, deadline=deadline, _check=self._cancel_checker(job_id))
        result = JsonlResult()
        try:
            job_input = self.link.parse_input(job.input)
            logger.info("job_started job=%s link=%s", job_id, self.link.name)
            for envelope in self.link.run(job_input, ctx):
                result.write(envelope)
                ctx.check()
            ctx.check()
            response = self.stub.BatchUploadFiles(result.upload_requests(job_id, RESULT_FILE))
            uploaded = sum(f.bytes for f in response.files)
        except JobCancelled as exc:
            if ctx.cancelled:
                logger.info("job_cancelled job=%s records=%s", job_id, result.records)
            else:
                self._fail(job_id, f"{exc} after {result.records} record(s)")
            return
        except JobError as exc:
            self._fail(job_id, str(exc))
            return
        except Exception as exc:
            logger.exception("job_crashed job=%s", job_id)
            # Type only: exception text from client libraries can echo request details.
            self._fail(job_id, f"internal error ({type(exc).__name__}); see the link's logs")
            return
        finally:
            result.close()
        self._set_status(
            job_id,
            COMPLETED,
            f"{result.records} record(s) in {RESULT_FILE}",
        )
        logger.info(
            "job_completed job=%s records=%s bytes=%s seconds=%.1f",
            job_id,
            result.records,
            uploaded,
            self._clock() - started,
        )

    def _cancel_checker(self, job_id: str) -> Callable[[], bool]:
        last = [self._clock()]

        def cancelled() -> bool:
            if self._clock() - last[0] < self.cancel_check_interval:
                return False
            last[0] = self._clock()
            job = self.stub.GetJob(external_pb2.GetJobRequest(id=job_id))
            return _state(job) == CANCELLED

        return cancelled

    def _fail(self, job_id: str, description: str) -> None:
        logger.warning("job_failed job=%s reason=%s", job_id, description)
        self._set_status(job_id, FAILED, description)

    def _set_status(self, job_id: str, state: int, description: str) -> None:
        try:
            self.stub.UpdateJobStatus(
                internal_pb2.UpdateJobStatusRequest(
                    id=job_id,
                    status=job_pb2.JobStatus(
                        state=state,  # type: ignore[arg-type]
                        description=description[:_MAX_DESCRIPTION],
                    ),
                )
            )
        except grpc.RpcError as exc:
            # e.g. an invalid transition because the job was cancelled meanwhile.
            logger.warning(
                "job_status_rejected job=%s state=%s code=%s",
                job_id,
                job_pb2.JobState.Name(state),
                _code(exc),
            )


def _state(job: job_pb2.Job) -> int:
    """The job's current state: statuses are kept most recent first."""
    return job.statuses[0].state if job.statuses else job_pb2.JOB_STATE_PENDING


def _code(exc: grpc.RpcError) -> str:
    code = exc.code() if hasattr(exc, "code") else None
    return code.name if code is not None else "UNKNOWN"
