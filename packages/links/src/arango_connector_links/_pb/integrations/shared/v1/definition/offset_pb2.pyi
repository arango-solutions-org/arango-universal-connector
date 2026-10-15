from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class OffsetRequest(_message.Message):
    __slots__ = ("items_per_page", "page")
    ITEMS_PER_PAGE_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    items_per_page: int
    page: int
    def __init__(self, items_per_page: _Optional[int] = ..., page: _Optional[int] = ...) -> None: ...

class OffsetResponse(_message.Message):
    __slots__ = ("items", "page")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    items: int
    page: int
    def __init__(self, items: _Optional[int] = ..., page: _Optional[int] = ...) -> None: ...
