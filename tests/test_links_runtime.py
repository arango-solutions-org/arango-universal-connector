"""The Links job loop against an in-process fake sidecar over real gRPC."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from arango_connector.envelope import Envelope, Scope
from arango_connector.payloads import MessagePayload
from arango_connector_links.link import RESULT_FILE, Example, JobContext, JobError, Link
from arango_connector_links.results import CHUNK_BYTES
from arango_connector_links.runtime import LinkRuntime
from links_fakes import S, SidecarServer, empty_pb2


class EchoInput(BaseModel):
    operation: Literal["echo"] = "echo"
    count: int = Field(default=1, ge=0)
    text: str = "hello"
    fail: str | None = None
    crash: bool = False


class EchoLink(Link[EchoInput]):
    name = "echo"
    description = "Echoes its input as messages."
    tags = ("test",)
    input_type = EchoInput
    examples = (Example("one", {"operation": "echo", "count": 1}, "one message"),)

    def __init__(self, on_item: Any = None) -> None:
        self.on_item = on_item

    def run(self, job_input: EchoInput, ctx: JobContext) -> Iterator[Envelope[Any]]:
        if job_input.fail:
            raise JobError(job_input.fail)
        if job_input.crash:
            raise RuntimeError("secret token xyz leaked in a client error")
        for n in range(job_input.count):
            if self.on_item:
                self.on_item(n)
            yield Envelope[MessagePayload](
                key=f"echo_{n}",
                source="echo",
                source_id=str(n),
                scope=Scope(container_id="c"),
                created_at=None,
                payload=MessagePayload(author_type="system", text=job_input.text),
                raw={},
            )


@pytest.fixture
def server() -> Iterator[SidecarServer]:
    s = SidecarServer()
    yield s
    s.close()


def runtime(server: SidecarServer, link: Link[Any] | None = None, **kw: Any) -> LinkRuntime:
    return LinkRuntime(link or EchoLink(), server.stub, idle_wait=0.01, **kw)


def lines(data: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in data.splitlines()]


def test_register_sends_the_tool_definition(server: SidecarServer) -> None:
    assert runtime(server).register()
    info = server.sidecar.info
    assert info is not None
    assert info.description == "Echoes its input as messages."
    assert list(info.tags) == ["test"]
    assert list(info.result_files) == [RESULT_FILE]
    schema = json.loads(info.input_schema)
    assert schema["properties"]["count"]["minimum"] == 0
    output = json.loads(info.output_schema)
    assert output["contentMediaType"] == "application/jsonl"
    assert "payload" in output["contentSchema"]["properties"]
    assert info.examples[0].name == "one" and json.loads(info.examples[0].input)["count"] == 1


def test_register_retries_until_the_sidecar_answers(server: SidecarServer) -> None:
    server.sidecar.unavailable_for = 2
    rt = runtime(server)
    rt_register_delay = []

    def fast_wait(seconds: float) -> bool:
        rt_register_delay.append(seconds)
        return False

    rt._stopped.wait = fast_wait  # type: ignore[method-assign]
    assert rt.register()
    assert rt_register_delay == [1.0, 2.0]


def test_empty_queue(server: SidecarServer) -> None:
    assert runtime(server).run_once() is False


def test_job_completes_with_jsonl_result(server: SidecarServer) -> None:
    job = server.sidecar.submit(b'{"operation": "echo", "count": 3, "text": "hi"}')

    assert runtime(server).run_once() is True

    assert job.state_names() == ["PENDING", "SCHEDULED", "RUNNING", "COMPLETED"]
    assert job.descriptions[-1] == f"3 record(s) in {RESULT_FILE}"
    records = lines(job.files[RESULT_FILE])
    assert [r["_key"] for r in records] == ["echo_0", "echo_1", "echo_2"]
    assert records[0]["payload"]["text"] == "hi"


def test_zero_records_still_uploads_an_empty_result(server: SidecarServer) -> None:
    job = server.sidecar.submit(b'{"count": 0}')
    runtime(server).run_once()
    assert job.state == S.JOB_STATE_COMPLETED
    assert job.files[RESULT_FILE] == b""


def test_large_results_are_chunked_with_the_name_on_the_first_chunk_only(
    server: SidecarServer,
) -> None:
    job = server.sidecar.submit(json.dumps({"count": 400, "text": "x" * 5000}).encode())
    runtime(server).run_once()

    chunks = server.sidecar.upload_chunks
    assert len(chunks) > 1
    assert chunks[0].job_id == job.id and chunks[0].name == RESULT_FILE
    assert all(not c.name and not c.job_id for c in chunks[1:])
    assert all(len(c.chunk) <= CHUNK_BYTES for c in chunks)
    assert len(lines(job.files[RESULT_FILE])) == 400


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"not json", "input is not valid JSON"),
        (b'{"count": -1}', "invalid input"),
        (b'{"operation": "nope"}', "invalid input"),
    ],
)
def test_invalid_input_fails_the_job(server: SidecarServer, raw: bytes, message: str) -> None:
    job = server.sidecar.submit(raw)
    runtime(server).run_once()
    assert job.state == S.JOB_STATE_FAILED
    assert job.descriptions[-1].startswith(message)
    assert RESULT_FILE not in job.files


def test_job_error_message_becomes_the_failure_description(server: SidecarServer) -> None:
    job = server.sidecar.submit(b'{"fail": "mailbox not allowed"}')
    runtime(server).run_once()
    assert job.state == S.JOB_STATE_FAILED
    assert job.descriptions[-1] == "mailbox not allowed"


def test_unexpected_errors_fail_without_leaking_their_text(server: SidecarServer) -> None:
    job = server.sidecar.submit(b'{"crash": true}')
    runtime(server).run_once()
    assert job.state == S.JOB_STATE_FAILED
    assert "RuntimeError" in job.descriptions[-1]
    assert "secret" not in job.descriptions[-1]


def test_cancellation_mid_run_stops_without_uploading(server: SidecarServer) -> None:
    job = None

    def cancel_after_two(n: int) -> None:
        if n == 2:
            server.sidecar.cancel(job.id)  # type: ignore[union-attr]

    job = server.sidecar.submit(b'{"count": 10}')
    runtime(server, EchoLink(on_item=cancel_after_two), cancel_check_interval=0).run_once()

    assert job.state == S.JOB_STATE_CANCELLED
    assert RESULT_FILE not in job.files
    assert job.rejected == []  # it didn't try to fail or complete a cancelled job


def test_already_cancelled_job_is_skipped(server: SidecarServer) -> None:
    job = server.sidecar.submit(b'{"count": 1}')
    rt = runtime(server)
    picked = server.stub.PickUpJob(empty_pb2.Empty())
    server.sidecar.cancel(picked.id)
    rt.process(picked.id)
    assert job.state_names() == ["PENDING", "SCHEDULED", "CANCELLED"]
    assert job.rejected == []  # never even tried RUNNING


def test_timeout_fails_the_job(server: SidecarServer) -> None:
    clock = [0.0]

    def tick(n: int) -> None:
        clock[0] += 10  # each item "takes" 10 seconds

    job = server.sidecar.submit(b'{"count": 10}', timeout_seconds=25)
    LinkRuntime(EchoLink(on_item=tick), server.stub, clock=lambda: clock[0]).run_once()

    assert job.state == S.JOB_STATE_FAILED
    assert job.descriptions[-1] == "timed out after 3 record(s)"  # 10s, 20s, then 30s > 25s
    assert RESULT_FILE not in job.files


def test_stopping_mid_job_fails_it_for_resubmission(server: SidecarServer) -> None:
    rt = runtime(server)
    job = server.sidecar.submit(b'{"count": 5}')
    rt.link = EchoLink(on_item=lambda n: n == 1 and rt.stop())  # SIGTERM during the job
    rt.run_once()

    assert job.state == S.JOB_STATE_FAILED
    assert job.descriptions[-1] == "link restarting; resubmit the job after 2 record(s)"
    assert RESULT_FILE not in job.files


def test_claimed_job_that_cannot_be_read_is_failed(server: SidecarServer) -> None:
    job = server.sidecar.submit(b'{"count": 1}')
    server.sidecar.unreadable.add(job.id)
    runtime(server).run_once()
    assert job.state_names() == ["PENDING", "SCHEDULED", "FAILED"]
    assert job.descriptions[-1] == "could not read the job (UNAVAILABLE); resubmit it"


def test_cancellation_after_the_last_record_is_still_noticed(server: SidecarServer) -> None:
    class CancelsAfterYielding(EchoLink):
        def run(self, job_input: EchoInput, ctx: JobContext) -> Iterator[Envelope[Any]]:
            yield from super().run(job_input, ctx)
            server.sidecar.cancel(job.id)  # e.g. while skipping messages that don't qualify

    job = server.sidecar.submit(b'{"count": 1}')
    runtime(server, CancelsAfterYielding(), cancel_check_interval=0).run_once()
    assert job.state == S.JOB_STATE_CANCELLED
    assert RESULT_FILE not in job.files


def test_long_failure_descriptions_are_truncated(server: SidecarServer) -> None:
    job = server.sidecar.submit(json.dumps({"fail": "x" * 5000}).encode())
    runtime(server).run_once()
    assert job.descriptions[-1] == "x" * 1000


def test_cancellation_is_polled_at_most_once_per_interval(server: SidecarServer) -> None:
    clock = [0.0]

    def tick(n: int) -> None:
        clock[0] += 1  # one record per second

    server.sidecar.submit(b'{"count": 20}')
    LinkRuntime(
        EchoLink(on_item=tick), server.stub, cancel_check_interval=5, clock=lambda: clock[0]
    ).run_once()
    assert server.sidecar.get_job_calls == 1 + 4  # the initial read, then at 5s, 10s, 15s, 20s


def test_run_forever_processes_jobs_until_stopped(server: SidecarServer) -> None:
    first = server.sidecar.submit(b'{"count": 1}')
    second = server.sidecar.submit(b'{"count": 2}')
    rt = runtime(server)

    worker = threading.Thread(target=rt.run_forever)
    worker.start()
    for _ in range(200):
        if first.state == second.state == S.JOB_STATE_COMPLETED:
            break
        threading.Event().wait(0.01)
    rt.stop()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert first.state == second.state == S.JOB_STATE_COMPLETED
