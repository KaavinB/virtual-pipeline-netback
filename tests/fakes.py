"""Stand-ins for requests.Session so tests never touch the network."""

import requests


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Returns queued responses (or raises queued exceptions) in order; records params."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def connection_error():
    return requests.ConnectionError("boom")
