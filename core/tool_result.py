from typing import Generic, Literal, TypeAlias, TypeVar, TypedDict


class FileReadData(TypedDict):
    kind: Literal["file"]
    path: str
    start_line: int
    end_line: int | None
    total_lines: int
    content: str


class DirectoryListData(TypedDict):
    kind: Literal["directory"]
    path: str
    entry_count: int
    content: str


ReadData: TypeAlias = FileReadData | DirectoryListData


class SearchContextLine(TypedDict):
    line: int
    text: str


class SearchMatch(TypedDict):
    path: str
    line: int
    text: str
    context: list[SearchContextLine]


class FileSearchData(TypedDict):
    query: str
    regex: bool
    case_sensitive: bool
    match_count: int
    searched_files: int
    skipped_files: int
    truncated: bool
    matches: list[SearchMatch]


class FileWriteData(TypedDict):
    path: str
    bytes_written: int


class PatchData(TypedDict):
    path: str
    previous_sha256: str
    sha256: str


class XmlValidationData(TypedDict):
    path: str
    schema: str
    deferred: bool
    custom_elements: list[str]


class RfcSearchData(TypedDict):
    query: str
    result_count: int
    content: str


class DslModuleValidationData(TypedDict):
    path: str
    schema_count: int
    schemas: list[str]


class DslValidationData(TypedDict):
    path: str
    output: str
    validation: "ToolResult[XmlValidationData]"


class ClassSearchData(TypedDict):
    query: str
    content: str


class DotNetBuildData(TypedDict):
    source: str
    output: str
    source_count: int


PayloadT = TypeVar("PayloadT", covariant=True)


class ToolSuccess(TypedDict, Generic[PayloadT]):
    ok: Literal[True]
    code: str
    message: str
    data: PayloadT


class ToolFailure(TypedDict):
    ok: Literal[False]
    code: str
    message: str
    data: None


ToolResult: TypeAlias = ToolSuccess[PayloadT] | ToolFailure


def tool_error(code: str, message: str) -> ToolFailure:
    return {"ok": False, "code": code, "message": message, "data": None}


def tool_success(
    code: str,
    message: str,
    data: PayloadT,
) -> ToolSuccess[PayloadT]:
    return {"ok": True, "code": code, "message": message, "data": data}
