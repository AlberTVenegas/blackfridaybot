from __future__ import annotations

from scrapers.stores.alishaperfumes import AlishaPerfumesScraper


class BoldScraper(AlishaPerfumesScraper):
    """Production scraper for Bold Chile (https://www.bold.cl) — Nike, Jordan, Adidas, New Balance."""

    retailer_name = "Bold"
    retailer_slug = "bold"
    base_url = "https://www.bold.cl"


class LippiScraper(AlishaPerfumesScraper):
    """Production scraper for Lippi Outdoor Chile (https://www.lippioutdoor.com)."""

    retailer_name = "Lippi"
    retailer_slug = "lippi"
    base_url = "https://www.lippioutdoor.com"


class SilkPerfumesScraper(AlishaPerfumesScraper):
    """Production scraper for Silk Perfumes Chile (https://silkperfumes.cl)."""

    retailer_name = "Silk Perfumes"
    retailer_slug = "silkperfumes"
    base_url = "https://silkperfumes.cl"
