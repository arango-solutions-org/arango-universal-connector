from google.api import annotations_pb2 as _annotations_pb2
from arango_connector_links._pb.integrations.link.v1.definition import external_pb2 as _external_pb2
from arango_connector_links._pb.integrations.link.v1.definition import info_pb2 as _info_pb2
from arango_connector_links._pb.integrations.link.v1.definition import job_pb2 as _job_pb2
from arango_connector_links._pb.integrations.shared.v1.definition import empty_pb2 as _empty_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PickUpJobResponse(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class UpdateJobStatusRequest(_message.Message):
    __slots__ = ("id", "status")
    ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    id: str
    status: _job_pb2.JobStatus
    def __init__(self, id: _Optional[str] = ..., status: _Optional[_Union[_job_pb2.JobStatus, _Mapping]] = ...) -> None: ...

class UpdateJobStatusResponse(_message.Message):
    __slots__ = ("job",)
    JOB_FIELD_NUMBER: _ClassVar[int]
    job: _job_pb2.Job
    def __init__(self, job: _Optional[_Union[_job_pb2.Job, _Mapping]] = ...) -> None: ...

class UploadFileRequest(_message.Message):
    __slots__ = ("job_id", "name", "data")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    name: str
    data: bytes
    def __init__(self, job_id: _Optional[str] = ..., name: _Optional[str] = ..., data: _Optional[bytes] = ...) -> None: ...

class UploadFileResponse(_message.Message):
    __slots__ = ("bytes", "checksum")
    BYTES_FIELD_NUMBER: _ClassVar[int]
    CHECKSUM_FIELD_NUMBER: _ClassVar[int]
    bytes: int
    checksum: str
    def __init__(self, bytes: _Optional[int] = ..., checksum: _Optional[str] = ...) -> None: ...

class BatchUploadFileRequest(_message.Message):
    __slots__ = ("job_id", "name", "chunk")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CHUNK_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    name: str
    chunk: bytes
    def __init__(self, job_id: _Optional[str] = ..., name: _Optional[str] = ..., chunk: _Optional[bytes] = ...) -> None: ...

class BatchUploadFilesResponse(_message.Message):
    __slots__ = ("files",)
    FILES_FIELD_NUMBER: _ClassVar[int]
    files: _containers.RepeatedCompositeFieldContainer[UploadFileResponse]
    def __init__(self, files: _Optional[_Iterable[_Union[UploadFileResponse, _Mapping]]] = ...) -> None: ...
