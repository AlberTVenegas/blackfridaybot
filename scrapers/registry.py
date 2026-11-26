from __future__ import annotations

from config.settings import Settings
from scrapers.base import BaseScraper
from scrapers.stores import (
    AdidasScraper,
    AlishaPerfumesScraper,
    DecathlonScraper,
    FalabellaScraper,
    LiderScraper,
    MercadoLibreScraper,
    NikeScraper,
    ParisScraper,
    RipleyScraper,
    SamsungScraper,
    SodimacScraper,
    SoloTodoScraper,
    XiaomiScraper,
)

SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "falabella": FalabellaScraper,
    "paris": ParisScraper,
    "ripley": RipleyScraper,
    "samsung": SamsungScraper,
    "xiaomi": XiaomiScraper,
    "nike": NikeScraper,
    "adidas": AdidasScraper,
    "decathlon": DecathlonScraper,
    "sodimac": SodimacScraper,
    "mercadolibre": MercadoLibreScraper,
    "lider": LiderScraper,
    "alishaperfumes": AlishaPerfumesScraper,
    "solotodo": SoloTodoScraper,
}


def create_scraper(store_slug: str, settings: Settings) -> BaseScraper:
    slug = store_slug.strip().lower()
    cls = SCRAPER_REGISTRY.get(slug)
    if cls is None:
        available = ", ".join(sorted(SCRAPER_REGISTRY))
        raise KeyError(f"Unknown retailer '{store_slug}'. Available: {available}")
    return cls(settings)
