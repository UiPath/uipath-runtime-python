import pytest

from uipath.runtime.jobapi.contract import (
    IIpcLogSink,
    JobLogDto,
    LogLevel,
)


def test_endpoint_name_matches_dotnet_router_key():
    # uipath-ipc routes by the contract class __name__, which must equal the
    # .NET interface name the Python executors register (IIpcLogSink, the log
    # channel that .NET's IJobInvocationApi extends).
    assert IIpcLogSink.__name__ == "IIpcLogSink"


def test_contract_only_declares_send_log():
    # Scope is logs only; the result stays on output.json.
    assert hasattr(IIpcLogSink, "SendLog")
    assert not hasattr(IIpcLogSink, "SetResult")


def test_log_level_values_match_microsoft_extensions():
    assert [
        LogLevel.TRACE,
        LogLevel.DEBUG,
        LogLevel.INFORMATION,
        LogLevel.WARNING,
        LogLevel.ERROR,
        LogLevel.CRITICAL,
        LogLevel.NONE,
    ] == [0, 1, 2, 3, 4, 5, 6]


def test_job_log_dto_wire_shape():
    # The field names are the wire keys, so this pins them against the .NET DTO.
    to_wire = pytest.importorskip("uipath_ipc").to_wire
    assert to_wire(JobLogDto(Message="hi", LogLevel=3)) == {
        "Message": "hi",
        "LogLevel": 3,
    }
