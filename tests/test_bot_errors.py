import asyncio
import logging
from types import SimpleNamespace

import pytest
from telegram.error import NetworkError

from cvflow.app.bot import on_error


def _run(exc: Exception) -> None:
    asyncio.run(on_error(None, SimpleNamespace(error=exc)))  # type: ignore[arg-type]


def test_network_blip_is_one_warning_line(caplog: pytest.LogCaptureFixture) -> None:
    # Seen live: a short DNS outage printed a 60-line traceback every 2 seconds.
    with caplog.at_level(logging.WARNING):
        _run(NetworkError("httpx.ConnectError: Temporary failure in name resolution"))
    (rec,) = caplog.records
    assert rec.levelno == logging.WARNING and rec.exc_info is None
    assert "Temporary failure in name resolution" in rec.getMessage()


def test_other_errors_keep_the_traceback(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        try:
            raise ValueError("boom")
        except ValueError as exc:
            _run(exc)
    (rec,) = caplog.records
    assert rec.levelno == logging.ERROR and rec.exc_info is not None
