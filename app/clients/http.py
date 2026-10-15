"""Shared HTTP helper: timeouts, bounded retries, and errors that name the vendor.

Every vendor call in this pipeline costs money or rate limit, so retries are bounded
and only applied to transient failures (429, 5xx, network). A 4xx other than 429 is a
bug in our request and retrying it just burns the same error three more times.
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class VendorOutage(RuntimeError):
    """A vendor cannot serve ANY request: out of credit, or the key is rejected.

    The one exception type every stage agrees to treat as "stop, do not burn work".
    Troy, 2026-09-14: "make all the things needed for copy so an outage PAUSES copy and
    does not break it." Three times now a vendor running dry has been caught per item
    and filed as that item's failure -- 20 contacts when Concentrate died, 2,820
    companies when Anthropic answered 400, 3,058 contacts when Serper ran out -- each
    with its paid work already spent. Anything that can stop the whole stage raises
    this instead, and the runner pauses the vertical with its progress banked.
    """


class VendorError(RuntimeError):
    def __init__(self, vendor: str, status: int | None, body: str):
        self.vendor, self.status = vendor, status
        super().__init__(f"{vendor} {status or 'network'}: {body[:400]}")


def request(
    vendor: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    params: dict[str, Any] | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    max_attempts: int = 4,
) -> Any:
    last: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.request(method, url, headers=headers, json=json_body, params=params)
            if resp.status_code in RETRY_STATUS and attempt < max_attempts - 1:
                # Honour Retry-After when the vendor sends one; it knows better than we do.
                wait = float(resp.headers.get("Retry-After") or 0) or (
                    min(2 ** attempt, 20) + random.uniform(0, 0.75)
                )
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise VendorError(vendor, resp.status_code, resp.text)
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt < max_attempts - 1:
                time.sleep(min(2 ** attempt, 20) + random.uniform(0, 0.75))
                continue
            raise VendorError(vendor, None, str(exc)) from exc
    raise VendorError(vendor, None, str(last))
