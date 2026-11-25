from __future__ import annotations

from scrapers.stores.falabella import FalabellaScraper


class SodimacScraper(FalabellaScraper):
    """Production scraper for Sodimac Chile (https://www.sodimac.cl).

    Sodimac Chile runs on the Falabella Next.js platform (`__NEXT_DATA__`),
    sharing the exact same catalog schema, stock flags, and Tarjeta CMR pricing.
    """

    retailer_name = "Sodimac"
    retailer_slug = "sodimac"
    base_url = "https://www.sodimac.cl"
