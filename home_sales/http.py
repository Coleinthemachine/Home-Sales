"""Shared HTTP client: identifies itself, obeys robots.txt, and backs off.

Every network call in this project goes through `Fetcher`. That is deliberate --
it makes the crawling posture a single auditable thing rather than a habit each
source has to remember.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """A request failed after exhausting retries."""


class RobotsDisallowed(FetchError):
    """The site's robots.txt forbids this path for our user agent.

    Raised, not swallowed: a source that hits this is telling you the operator
    does not want automated access, and the fix is a licensed API or a public
    records feed -- not a different user agent.
    """


@dataclass
class _HostState:
    last_request: float = 0.0
    robots: urllib.robotparser.RobotFileParser | None = None
    robots_checked: bool = False


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        *,
        delay_seconds: float = 1.5,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        respect_robots: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.respect_robots = respect_robots
        self._hosts: dict[str, _HostState] = {}
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def _state(self, host: str) -> _HostState:
        with self._lock:
            return self._hosts.setdefault(host, _HostState())

    def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        host = parsed.netloc
        state = self._state(host)
        if not state.robots_checked:
            parser = urllib.robotparser.RobotFileParser()
            robots_url = urljoin(f"{parsed.scheme}://{host}", "/robots.txt")
            try:
                response = self.session.get(robots_url, timeout=self.timeout_seconds)
                if response.status_code == 200:
                    parser.parse(response.text.splitlines())
                else:
                    # No robots.txt published means no restriction stated.
                    parser.allow_all = True
            except requests.RequestException as exc:
                log.debug("robots.txt unreachable for %s (%s); proceeding", host, exc)
                parser.allow_all = True
            state.robots = parser
            state.robots_checked = True
        assert state.robots is not None
        return state.robots.can_fetch(self.user_agent, url)

    def _throttle(self, host: str) -> None:
        state = self._state(host)
        elapsed = time.monotonic() - state.last_request
        wait = self.delay_seconds - elapsed
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.25))
        state.last_request = time.monotonic()

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        if not self._robots_allows(url):
            raise RobotsDisallowed(
                f"robots.txt at {urlparse(url).netloc} disallows {url} for automated clients. "
                "Use a licensed data API or a public-records feed for this site instead."
            )

        host = urlparse(url).netloc
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            try:
                response = self.session.get(
                    url, timeout=self.timeout_seconds, **kwargs
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("GET %s failed (attempt %d): %s", url, attempt + 1, exc)
            else:
                if response.status_code not in RETRY_STATUS:
                    response.raise_for_status()
                    return response
                last_error = FetchError(f"HTTP {response.status_code} from {url}")
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(int(retry_after), 120))
                    continue

            if attempt < self.max_retries:
                time.sleep(min(2**attempt + random.uniform(0, 1), 60))

        raise FetchError(f"GET {url} failed after {self.max_retries + 1} attempts: {last_error}")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        response = self.get(url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            snippet = response.text[:200].replace("\n", " ")
            raise FetchError(
                f"Expected JSON from {url} but got {response.headers.get('Content-Type')}: {snippet}"
            ) from exc
