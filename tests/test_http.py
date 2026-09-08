import pytest
import requests

from home_sales.http import FetchError, Fetcher, RobotsDisallowed

ROBOTS_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
ROBOTS_PARTIAL = "User-agent: *\nDisallow: /private\nAllow: /\n"


class StubResponse:
    def __init__(self, text="", status_code=200, json_data=None, headers=None):
        self.text = text
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {"Content-Type": "application/json"}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def make_fetcher(handler, **kwargs):
    fetcher = Fetcher("test-bot/1.0", delay_seconds=0, max_retries=1, **kwargs)
    fetcher.session.get = handler  # type: ignore[method-assign]
    return fetcher


def test_disallowed_path_raises_rather_than_scraping():
    def handler(url, **kwargs):
        if url.endswith("/robots.txt"):
            return StubResponse(ROBOTS_DISALLOW_ALL)
        raise AssertionError("must not request a disallowed page")

    fetcher = make_fetcher(handler)
    with pytest.raises(RobotsDisallowed, match="disallows"):
        fetcher.get("https://listings.example.com/sold")


def test_allowed_path_proceeds():
    def handler(url, **kwargs):
        if url.endswith("/robots.txt"):
            return StubResponse(ROBOTS_PARTIAL)
        return StubResponse("ok")

    assert make_fetcher(handler).get("https://county.example.org/data").text == "ok"


def test_robots_is_fetched_once_per_host():
    calls = {"robots": 0}

    def handler(url, **kwargs):
        if url.endswith("/robots.txt"):
            calls["robots"] += 1
            return StubResponse(ROBOTS_PARTIAL)
        return StubResponse("ok")

    fetcher = make_fetcher(handler)
    fetcher.get("https://county.example.org/a")
    fetcher.get("https://county.example.org/b")
    assert calls["robots"] == 1


def test_missing_robots_is_treated_as_allowed():
    def handler(url, **kwargs):
        if url.endswith("/robots.txt"):
            return StubResponse("Not Found", status_code=404)
        return StubResponse("ok")

    assert make_fetcher(handler).get("https://county.example.org/data").text == "ok"


def test_unreachable_robots_does_not_block_the_run():
    def handler(url, **kwargs):
        if url.endswith("/robots.txt"):
            raise requests.ConnectionError("dns failure")
        return StubResponse("ok")

    assert make_fetcher(handler).get("https://county.example.org/data").text == "ok"


def test_respect_robots_can_be_disabled_for_your_own_endpoints():
    def handler(url, **kwargs):
        assert not url.endswith("/robots.txt")
        return StubResponse("ok")

    fetcher = make_fetcher(handler, respect_robots=False)
    assert fetcher.get("https://internal.example.org/data").text == "ok"


def test_retries_then_succeeds_on_transient_error():
    attempts = {"n": 0}

    def handler(url, **kwargs):
        if url.endswith("/robots.txt"):
            return StubResponse(ROBOTS_PARTIAL)
        attempts["n"] += 1
        if attempts["n"] == 1:
            return StubResponse("", status_code=503)
        return StubResponse("recovered")

    assert make_fetcher(handler).get("https://county.example.org/x").text == "recovered"
    assert attempts["n"] == 2


def test_gives_up_after_max_retries():
    def handler(url, **kwargs):
        if url.endswith("/robots.txt"):
            return StubResponse(ROBOTS_PARTIAL)
        return StubResponse("", status_code=500)

    with pytest.raises(FetchError, match="failed after"):
        make_fetcher(handler).get("https://county.example.org/x")


def test_get_json_reports_html_error_pages_clearly():
    def handler(url, **kwargs):
        if url.endswith("/robots.txt"):
            return StubResponse(ROBOTS_PARTIAL)
        return StubResponse("<html>Service unavailable</html>", headers={"Content-Type": "text/html"})

    with pytest.raises(FetchError, match="Expected JSON"):
        make_fetcher(handler).get_json("https://county.example.org/query")


def test_user_agent_is_sent():
    seen = {}

    def handler(url, **kwargs):
        seen["ua"] = fetcher.session.headers["User-Agent"]
        return StubResponse(ROBOTS_PARTIAL if url.endswith("/robots.txt") else "ok")

    fetcher = make_fetcher(handler)
    fetcher.get("https://county.example.org/data")
    assert seen["ua"] == "test-bot/1.0"
