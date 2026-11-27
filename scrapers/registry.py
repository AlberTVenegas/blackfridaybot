from __future__ import annotations

from config.settings import Settings
from scrapers.base import BaseScraper
from scrapers.stores import (
    AdidasScraper,
    AlishaPerfumesScraper,
    BoldScraper,
    DecathlonScraper,
    ElitePerfumesScraper,
    FalabellaScraper,
    LiderScraper,
    LippiScraper,
    MercadoLibreScraper,
    NikeScraper,
    ParisScraper,
    RipleyScraper,
    SamsungScraper,
    SilkPerfumesScraper,
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
    "bold": BoldScraper,
    "lippi": LippiScraper,
    "silkperfumes": SilkPerfumesScraper,
    "eliteperfumes": ElitePerfumesScraper,
}


def create_scraper(store_slug: str, settings: Settings) -> BaseScraper:
    slug = store_slug.strip().lower()
    cls = SCRAPER_REGISTRY.get(slug)
    if cls is None:
        available = ", ".join(sorted(SCRAPER_REGISTRY))
        raise KeyError(f"Unknown retailer '{store_slug}'. Available: {available}")
    return cls(settings)
