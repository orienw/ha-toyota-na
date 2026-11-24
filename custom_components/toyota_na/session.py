"""What every request to Toyota shares: its brand, its timeouts and the aiohttp session."""

from contextlib import asynccontextmanager

import aiohttp

TRANSPORT_BRAND = "T"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=30)


@asynccontextmanager
async def request_session(session=None):
    """Yield a shared aiohttp session or create a short-lived fallback session."""
    if session is not None and not getattr(session, "closed", False):
        yield session, True
        return
    async with aiohttp.ClientSession(
        timeout=HTTP_TIMEOUT, cookie_jar=aiohttp.DummyCookieJar()
    ) as owned:
        yield owned, False
