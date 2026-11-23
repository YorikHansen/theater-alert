'''Playwright browser management and scraping for the Theater Kiel webshop.'''

from __future__ import annotations

import asyncio
import io
import logging
from datetime import date
from typing import Any, Self

from PIL import Image, UnidentifiedImageError
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    ViewportSize,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from theater_alert.config import BASE_URL, EVENT_WINDOW_DAYS
from theater_alert.models import Event, Seatmap
from theater_alert.theater.parser import events_in_window, parse_events, parse_seatmap

LOGGER = logging.getLogger(__name__)

VIEWPORT: ViewportSize = {'width': 1920, 'height': 1080}
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
)


class TheaterBrowser:
    '''Manages browser sessions for the Theater Kiel webshop.'''

    BASE_URL = BASE_URL

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        '''Start Chromium and initialize a browser context.'''
        self._playwright = await async_playwright().start()

        if self.headless:
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                channel='chromium',
            )
        else:
            self._browser = await self._playwright.chromium.launch(headless=False)

        self._context = await self._browser.new_context(
            locale='de-DE',
            timezone_id='Europe/Berlin',
            viewport=VIEWPORT,
            screen=VIEWPORT,
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            user_agent=USER_AGENT,
            extra_http_headers={'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8'},
        )
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

    async def new_page(self) -> Page:
        '''Create a new page within the current session.'''
        if self._context is None:
            raise RuntimeError('Browser has not been started.')

        return await self._context.new_page()

    async def list_all_events(self, today: date | None = None) -> list[Event]:
        '''Open the base URL and parse every event present in the loaded HTML.'''
        page = await self.new_page()
        try:
            await self._open_base(page)
            return parse_events(await page.content(), today=today, window_days=None)
        finally:
            await page.close()

    async def list_events(
        self,
        today: date | None = None,
        window_days: int | None = EVENT_WINDOW_DAYS,
    ) -> list[Event]:
        '''List events inside the notification window; None keeps all parsed events.'''
        events = await self.list_all_events(today=today)
        return events_in_window(events, today=today, window_days=window_days)

    async def fetch_events(
        self,
        today: date | None = None,
        window_days: int | None = EVENT_WINDOW_DAYS,
    ) -> list[Event]:
        '''Alias for :meth:`list_events`.'''
        return await self.list_events(today=today, window_days=window_days)

    async def fetch_seatmap(self, event: Event) -> Seatmap:
        '''Fetch the seat groups using the event's link from the event list.'''
        page = await self.new_page()
        try:
            await self._open_base(page)
            await page.goto(event.url or BASE_URL)
            try:
                await page.wait_for_selector('li[data-original-value]', timeout=30_000)
            except PlaywrightTimeoutError:
                await page.wait_for_selector('#seatmap[ready="true"]', timeout=30_000)

            image = await self._capture_leaflet_canvas(page)
            if image is None:
                image = await self._capture_leaflet_image(page)
            if image is None:
                image = await self._screenshot_map(page)

            seatmap = parse_seatmap(await page.content())
            return Seatmap(groups=seatmap.groups, screenshot=image)
        finally:
            await page.close()

    async def _open_base(self, page: Page) -> None:
        '''Load the base URL first so the webshop session is established in this tab.'''
        await page.goto(BASE_URL)
        await page.wait_for_selector('#eventListUl', timeout=30_000)
        await self._show_bookable_events_only(page)

    @staticmethod
    async def _zoom(page: Page, direction: str) -> bool:
        control = page.locator(f'.leaflet-control-zoom-{direction}')
        try:
            if not await control.count():
                return False
            classes = await control.first.get_attribute('class') or ''
            if 'leaflet-disabled' in classes:
                return False
            await control.first.click()
            await page.wait_for_timeout(700)
            return True
        except PlaywrightError:
            return False

    @staticmethod
    def _map_fits(layout: Any) -> bool:
        width = layout['width']
        height = layout['height']
        for tile in layout['tiles']:
            if 'emptyTile.png' in tile['src']:
                continue
            if (
                tile['x'] < -1
                or tile['y'] < -1
                or tile['x'] + tile['width'] > width + 1
                or tile['y'] + tile['height'] > height + 1
            ):
                return False
        return True

    async def _capture_leaflet_canvas(self, page: Page) -> bytes | None:
        try:
            await page.wait_for_selector('.leaflet-container', timeout=10_000)
        except PlaywrightTimeoutError:
            return None

        layout = await self._read_map_tiles(page)
        for _ in range(4):
            if layout is None or not await self._zoom(page, 'in'):
                break
            zoomed = await self._read_map_tiles(page)
            if zoomed is None or not self._map_fits(zoomed):
                await self._zoom(page, 'out')
                break
            layout = zoomed

        try:
            await page.add_style_tag(
                content='.leaflet-control-container { display: none !important; }'
            )
            await page.wait_for_timeout(100)
        except PlaywrightError:
            pass

        for selector in ('.leaflet-container', '#seatmap'):
            try:
                return await page.locator(selector).screenshot(
                    type='png',
                    timeout=15_000,
                    animations='disabled',
                )
            except PlaywrightError:
                continue
        return None

    @staticmethod
    async def _read_map_tiles(page: Page) -> Any:
        layout = await page.evaluate(
            """() => {
                const container = document.querySelector('.leaflet-container');
                if (!container) return null;
                const box = container.getBoundingClientRect();
                const tiles = Array.from(document.querySelectorAll('img.leaflet-tile'))
                    .map((img) => {
                        const rect = img.getBoundingClientRect();
                        return {
                            src: img.currentSrc || img.src || '',
                            x: rect.left - box.left,
                            y: rect.top - box.top,
                            width: rect.width,
                            height: rect.height,
                        };
                    })
                    .filter((tile) => tile.src && tile.width > 0 && tile.height > 0);
                if (!tiles.length) return null;
                return {width: box.width, height: box.height, tiles};
            }"""
        )
        return layout if isinstance(layout, dict) else None

    async def _capture_leaflet_image(self, page: Page) -> bytes | None:
        try:
            await page.wait_for_selector('img.leaflet-tile-loaded', timeout=10_000)
        except PlaywrightTimeoutError:
            return None

        layout = await self._read_map_tiles(page)
        if not layout or not layout['tiles']:
            return None
        sources = sorted(
            {
                tile['src']
                for tile in layout['tiles']
                if 'emptyTile.png' not in tile['src']
            }
        )

        async def fetch(source: str) -> tuple[str, bytes | None]:
            try:
                response = await page.request.get(source, timeout=20_000)
                if response.ok:
                    return source, await response.body()
            except PlaywrightError:
                pass
            return source, None

        fetched = dict(await asyncio.gather(*(fetch(source) for source in sources)))
        return self._stitch_layout(layout, fetched)

    @staticmethod
    def _stitch_layout(layout: Any, bodies: dict[str, bytes | None]) -> bytes | None:
        width = max(1, int(layout['width']))
        height = max(1, int(layout['height']))
        canvas = Image.new('RGB', (width, height), '#ffffff')
        pasted = 0
        for tile in layout['tiles']:
            raw = bodies.get(tile['src'])
            if not raw:
                continue
            try:
                image = Image.open(io.BytesIO(raw))
                image.load()
            except (UnidentifiedImageError, OSError, ValueError):
                continue
            tile_width = max(1, int(tile['width']))
            tile_height = max(1, int(tile['height']))
            if image.size != (tile_width, tile_height):
                image = image.resize((tile_width, tile_height))
            rgba = image.convert('RGBA')
            canvas.paste(rgba.convert('RGB'), (int(tile['x']), int(tile['y'])), rgba.getchannel('A'))
            pasted += 1
        if not pasted:
            return None
        output = io.BytesIO()
        canvas.save(output, format='PNG', optimize=True)
        return output.getvalue()

    @staticmethod
    async def _screenshot_map(page: Page) -> bytes | None:
        try:
            await page.add_style_tag(
                content='.leaflet-control-container { display: none !important; }'
            )
        except PlaywrightError:
            pass
        for selector in ('.leaflet-map-pane', '.leaflet-container', '#seatmap'):
            try:
                return await page.locator(selector).screenshot(
                    type='png',
                    timeout=10_000,
                    animations='disabled',
                )
            except PlaywrightError:
                continue
        return None

    async def _show_bookable_events_only(self, page: Page) -> None:
        '''Enable the webshop's filter so only bookable events are listed.'''
        checkbox = page.locator(
            'label:has-text("Nur buchbare Veranstaltungen") input[type="checkbox"]'
        )
        try:
            if await checkbox.count() and not await checkbox.first.is_checked():
                await checkbox.first.check()
                await page.wait_for_timeout(500)
        except PlaywrightError as error:
            LOGGER.debug('Could not enable the bookable-events filter: %s', error)

    async def close(self) -> None:
        '''Close browser resources.'''
        if self._context is not None:
            await self._context.close()

        if self._browser is not None:
            await self._browser.close()

        if self._playwright is not None:
            await self._playwright.stop()

        self._context = None
        self._browser = None
        self._playwright = None
