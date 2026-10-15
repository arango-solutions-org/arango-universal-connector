import datetime

from google.protobuf import duration_pb2 as _duration_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class JobState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    JOB_STATE_PENDING: _ClassVar[JobState]
    JOB_STATE_SCHEDULED: _ClassVar[JobState]
    JOB_STATE_RUNNING: _ClassVar[JobState]
    JOB_STATE_COMPLETED: _ClassVar[JobState]
    JOB_STATE_FAILED: _ClassVar[JobState]
    JOB_STATE_CANCELLED: _ClassVar[JobState]
JOB_STATE_PENDING: JobState
JOB_STATE_SCHEDULED: JobState
JOB_STATE_RUNNING: JobState
JOB_STATE_COMPLETED: JobState
JOB_STATE_FAILED: JobState
JOB_STATE_CANCELLED: JobState

class JobStatus(_message.Message):
    __slots__ = ("state", "description", "updated")
    STATE_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    UPDATED_FIELD_NUMBER: _ClassVar[int]
    state: JobState
    description: str
    updated: _timestamp_pb2.Timestamp
    def __init__(self, state: _Optional[_Union[JobState, str]] = ..., description: _Optional[str] = ..., updated: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class Job(_message.Message):
    __slots__ = ("id", "link_id", "handler_id", "statuses", "input", "timeout", "created", "result")
    ID_FIELD_NUMBER: _ClassVar[int]
    LINK_ID_FIELD_NUMBER: _ClassVar[int]
    HANDLER_ID_FIELD_NUMBER: _ClassVar[int]
    STATUSES_FIELD_NUMBER: _ClassVar[int]
    INPUT_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    CREATED_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    id: str
    link_id: str
    handler_id: str
    statuses: _containers.RepeatedCompositeFieldContainer[JobStatus]
    input: bytes
    timeout: _duration_pb2.Duration
    created: _timestamp_pb2.Timestamp
    result: str
    def __init__(self, id: _Optional[str] = ..., link_id: _Optional[str] = ..., handler_id: _Optional[str] = ..., statuses: _Optional[_Iterable[_Union[JobStatus, _Mapping]]] = ..., input: _Optional[bytes] = ..., timeout: _Optional[_Union[datetime.timedelta, _duration_pb2.Duration, _Mapping]] = ..., created: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., result: _Optional[str] = ...) -> None: ...
