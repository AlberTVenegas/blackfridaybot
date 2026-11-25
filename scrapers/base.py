from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Self

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from config.settings import Settings
from scrapers.models import ScrapedProduct
from scrapers.utils import ProxyRotator, get_random_user_agent, random_delay

logger = logging.getLogger(__name__)


class RetryableFetchError(Exception):
    """Raised on transient HTTP/network failures (429, 5xx, timeouts)."""


class BlockedFetchError(Exception):
    """Raised when a retailer WAF blocks HTTP access (e.g. 403/Cloudflare)."""


class BaseScraper(ABC):
    """Abstract base scraper with hybrid curl_cffi + Playwright anti-blocking engine."""

    retailer_name: str
    retailer_slug: str
    base_url: str

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.proxy_rotator = ProxyRotator(settings.parsed_proxy_urls)
        self._http_session: AsyncSession | None = None
        self._playwright: Any = None
        self._browser: Any = None

    async def __aenter__(self) -> Self:
        self._http_session = AsyncSession(
            impersonate="chrome",
            timeout=self.settings.http_timeout_seconds,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    def _build_headers(self, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": self.base_url,
        }
        if extra_headers:
            headers.update(extra_headers)
        return headers

    async def fetch_http(
        self,
        url: str,
        *,
        extra_headers: dict[str, str] | None = None,
        max_attempts: int = 3,
    ) -> str:
        """Fetch URL using curl_cffi with Chrome TLS fingerprint, retries, and jitter."""
        if self._http_session is None:
            self._http_session = AsyncSession(
                impersonate="chrome",
                timeout=self.settings.http_timeout_seconds,
                curl_options={CurlOpt.IPRESOLVE: 1},
            )

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential_jitter(initial=1.5, max=15.0, jitter=1.5),
            retry=retry_if_exception_type(RetryableFetchError),
            reraise=True,
        ):
            with attempt:
                await random_delay(
                    self.settings.request_min_delay_sec,
                    self.settings.request_max_delay_sec,
                )
                proxy = self.proxy_rotator.next_proxy()
                proxies = {"http": proxy, "https": proxy} if proxy else None
                try:
                    response = await self._http_session.get(
                        url,
                        headers=self._build_headers(extra_headers),
                        proxies=proxies,
                        allow_redirects=True,
                    )
                except Exception as exc:
                    raise RetryableFetchError(f"Network error fetching {url}: {exc}") from exc

                status = response.status_code
                if status in (429, 500, 502, 503, 504):
                    raise RetryableFetchError(f"Transient HTTP {status} for {url}")
                if status in (401, 403):
                    raise BlockedFetchError(f"Blocked HTTP {status} for {url}")
                if status >= 400:
                    raise RuntimeError(f"HTTP {status} for {url}")

                return response.text

        raise RuntimeError(f"Exhausted retries for {url}")

    async def fetch_browser(
        self,
        url: str,
        *,
        wait_selector: str | None = None,
        timeout_ms: int = 30000,
    ) -> str:
        """Fallback fetch via headless Playwright Chromium, blocking heavy assets."""
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()

        if self._browser is None:
            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            proxy = self.proxy_rotator.next_proxy()
            if proxy:
                launch_kwargs["proxy"] = {"server": proxy}
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)

        context = await self._browser.new_context(
            user_agent=get_random_user_agent(),
            locale="es-CL",
            timezone_id="America/Santiago",
            viewport={"width": 1366, "height": 768},
        )
        page = await context.new_page()

        # Block images, fonts, and media to drastically lower RAM & bandwidth on Railway
        async def _block_heavy_resources(route: Any) -> None:
            if route.request.resource_type in {"image", "media", "font"}:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", _block_heavy_resources)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=10000)
                except PlaywrightTimeoutError:
                    logger.debug("Selector %s not found within timeout on %s", wait_selector, url)
            return await page.content()
        finally:
            await context.close()

    async def fetch_hybrid(
        self,
        url: str,
        *,
        wait_selector: str | None = None,
    ) -> str:
        """Attempt fast curl_cffi fetch first; fall back to Playwright if blocked."""
        try:
            return await self.fetch_http(url)
        except (BlockedFetchError, RetryableFetchError) as exc:
            if not self.settings.playwright_fallback_enabled:
                raise
            logger.warning(
                "[%s] HTTP fetch failed (%s). Falling back to Playwright for %s",
                self.retailer_name,
                exc,
                url,
            )
            return await self.fetch_browser(url, wait_selector=wait_selector)

    @abstractmethod
    async def scrape_category(
        self,
        category_url: str,
        category_name: str,
        max_pages: int = 2,
    ) -> list[ScrapedProduct]:
        """Discover and extract products from a category listing."""

    @abstractmethod
    async def scrape_product(
        self,
        product_url: str,
        category_name: str | None = None,
    ) -> ScrapedProduct | None:
        """Extract a single product from its product detail page (watchlist mode)."""
