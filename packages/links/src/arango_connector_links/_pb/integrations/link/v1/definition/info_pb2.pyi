from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class LinkInfo(_message.Message):
    __slots__ = ("description", "tags", "input_schema", "output_schema", "examples", "result_files")
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    INPUT_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    EXAMPLES_FIELD_NUMBER: _ClassVar[int]
    RESULT_FILES_FIELD_NUMBER: _ClassVar[int]
    description: str
    tags: _containers.RepeatedScalarFieldContainer[str]
    input_schema: str
    output_schema: str
    examples: _containers.RepeatedCompositeFieldContainer[LinkExample]
    result_files: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, description: _Optional[str] = ..., tags: _Optional[_Iterable[str]] = ..., input_schema: _Optional[str] = ..., output_schema: _Optional[str] = ..., examples: _Optional[_Iterable[_Union[LinkExample, _Mapping]]] = ..., result_files: _Optional[_Iterable[str]] = ...) -> None: ...

class LinkExample(_message.Message):
    __slots__ = ("name", "input", "output")
    NAME_FIELD_NUMBER: _ClassVar[int]
    INPUT_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    name: str
    input: str
    output: str
    def __init__(self, name: _Optional[str] = ..., input: _Optional[str] = ..., output: _Optional[str] = ...) -> None: ...
