import datetime

from google.api import annotations_pb2 as _annotations_pb2
from google.protobuf import duration_pb2 as _duration_pb2
from arango_connector_links._pb.integrations.link.v1.definition import info_pb2 as _info_pb2
from arango_connector_links._pb.integrations.link.v1.definition import job_pb2 as _job_pb2
from arango_connector_links._pb.integrations.shared.v1.definition import empty_pb2 as _empty_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CreateJobRequest(_message.Message):
    __slots__ = ("input", "timeout")
    INPUT_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    input: bytes
    timeout: _duration_pb2.Duration
    def __init__(self, input: _Optional[bytes] = ..., timeout: _Optional[_Union[datetime.timedelta, _duration_pb2.Duration, _Mapping]] = ...) -> None: ...

class CreateJobResponse(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class GetJobRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class ListJobsRequest(_message.Message):
    __slots__ = ("state",)
    STATE_FIELD_NUMBER: _ClassVar[int]
    state: _job_pb2.JobState
    def __init__(self, state: _Optional[_Union[_job_pb2.JobState, str]] = ...) -> None: ...

class ListJobsResponse(_message.Message):
    __slots__ = ("jobs",)
    JOBS_FIELD_NUMBER: _ClassVar[int]
    jobs: _containers.RepeatedCompositeFieldContainer[_job_pb2.Job]
    def __init__(self, jobs: _Optional[_Iterable[_Union[_job_pb2.Job, _Mapping]]] = ...) -> None: ...

class CancelJobRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class CancelJobResponse(_message.Message):
    __slots__ = ("job",)
    JOB_FIELD_NUMBER: _ClassVar[int]
    job: _job_pb2.Job
    def __init__(self, job: _Optional[_Union[_job_pb2.Job, _Mapping]] = ...) -> None: ...

class GetInfoResponse(_message.Message):
    __slots__ = ("info",)
    INFO_FIELD_NUMBER: _ClassVar[int]
    info: _info_pb2.LinkInfo
    def __init__(self, info: _Optional[_Union[_info_pb2.LinkInfo, _Mapping]] = ...) -> None: ...
