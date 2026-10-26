"""HTTP GET with retries and exponential backoff, shared by every API client."""

import logging
import time
from collections.abc import Callable

import requests

log = logging.getLogger("http")

# 429 = rate limited, 5xx = server trouble: worth waiting for. Any other 4xx is our bug.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    pass


def get_json(
    session: requests.Session,
    url: str,
    params: dict,
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    timeout: float = 30,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """GET `url` and return the decoded JSON body, retrying transient failures.

    Waits base_delay * 2**n between attempts (1s, 2s, 4s, 8s by default). Error messages
    never include the URL or params, because the query string carries the API key.
    """
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            problem = type(exc).__name__
        else:
            if resp.status_code < 400:
                return resp.json()
            if resp.status_code not in RETRYABLE_STATUS:
                raise FetchError(f"HTTP {resp.status_code} (not retryable): {resp.text[:200]}")
            problem = f"HTTP {resp.status_code}"

        if attempt == attempts:
            raise FetchError(f"gave up after {attempts} attempts, last error: {problem}")
        delay = base_delay * 2 ** (attempt - 1)
        log.warning("%s; retry %d/%d in %.0fs", problem, attempt, attempts - 1, delay)
        sleep(delay)
    raise AssertionError("unreachable")
