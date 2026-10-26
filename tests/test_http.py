import pytest

from ingestion.http import FetchError, get_json
from tests.fakes import FakeResponse, FakeSession, connection_error


def test_retries_transient_failures_with_exponential_backoff():
    session = FakeSession(
        FakeResponse(503), connection_error(), FakeResponse(429), FakeResponse(200, {"ok": 1})
    )
    waits = []
    assert get_json(session, "u", {}, sleep=waits.append) == {"ok": 1}
    assert waits == [1.0, 2.0, 4.0]


def test_does_not_retry_client_errors():
    session = FakeSession(FakeResponse(403, {"error": "bad key"}))
    with pytest.raises(FetchError, match="403"):
        get_json(session, "u", {}, sleep=lambda s: None)
    assert len(session.calls) == 1


def test_gives_up_after_max_attempts_without_leaking_params():
    session = FakeSession(*[FakeResponse(500)] * 3)
    with pytest.raises(FetchError, match="gave up after 3") as err:
        get_json(session, "u", {"api_key": "SECRET"}, attempts=3, sleep=lambda s: None)
    assert "SECRET" not in str(err.value)
