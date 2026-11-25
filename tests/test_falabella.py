from __future__ import annotations

import json

from analyzer.models import PaymentMethodType
from config.settings import Settings
from scrapers.stores.falabella import FalabellaScraper


def _wrap_next_data(page_props: dict) -> str:
    payload = json.dumps({"props": {"pageProps": page_props}})
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{payload}</script></body></html>'


def test_falabella_category_parser_distinguishes_cmr_and_all_payment(
    test_settings: Settings,
) -> None:
    html = _wrap_next_data(
        {
            "results": [
                {
                    "skuId": "170001",
                    "displayName": "Notebook Gamer 16GB",
                    "url": "/falabella-cl/product/170001/notebook-gamer",
                    "prices": [
                        {"type": "cmrPrice", "price": ["$599.990"], "crossed": False},
                        {"type": "internetPrice", "price": ["$649.990"], "crossed": False},
                        {"type": "normalPrice", "price": ["$999.990"], "crossed": True},
                    ],
                    "availability": {
                        "homeDeliveryShipping": "2026-11-30",
                        "pickUpFromStoreShipping": "",
                    },
                },
                {
                    "skuId": "170002",
                    "displayName": "Audífonos Bluetooth Glitch",
                    "url": "/falabella-cl/product/170002/audifonos",
                    "prices": [
                        {"type": "eventPrice", "price": ["$100"], "crossed": False},
                        {"type": "normalPrice", "price": ["$100.000"], "crossed": True},
                    ],
                    "availability": {
                        "homeDeliveryShipping": "2026-11-30",
                        "pickUpFromStoreShipping": "2026-11-28",
                    },
                },
                {
                    "skuId": "170003",
                    "displayName": "Zapatillas Agotadas",
                    "url": "/falabella-cl/product/170003/zapatillas",
                    "prices": [
                        {"type": "internetPrice", "price": ["$9.990"], "crossed": False},
                        {"type": "normalPrice", "price": ["$89.990"], "crossed": True},
                    ],
                    "availability": {
                        "homeDeliveryShipping": "",
                        "pickUpFromStoreShipping": "",
                    },
                },
            ]
        }
    )

    scraper = FalabellaScraper(test_settings)
    products = scraper.parse_category_html(html, category_name="Tecnología")

    assert len(products) == 3

    # 1. CMR cheaper -> tagged STORE_CARD, preserves normalPrice & all_payment_price
    nb = products[0]
    assert nb.sku == "170001"
    assert nb.price_clp == 599_990
    assert nb.normal_price_clp == 999_990
    assert nb.all_payment_price_clp == 649_990
    assert nb.card_price_clp == 599_990
    assert nb.payment_method == PaymentMethodType.STORE_CARD
    assert nb.payment_method_label == "Exclusivo Tarjeta CMR"
    assert nb.in_stock is True
    assert nb.url == "https://www.falabella.com/falabella-cl/product/170001/notebook-gamer"

    # 2. All payment methods extreme glitch ($100.000 -> $100)
    glitch = products[1]
    assert glitch.sku == "170002"
    assert glitch.price_clp == 100
    assert glitch.normal_price_clp == 100_000
    assert glitch.payment_method == PaymentMethodType.ALL_PAYMENT_METHODS
    assert glitch.payment_method_label == "Todo medio de pago"
    assert glitch.in_stock is True

    # 3. Out of stock product
    oos = products[2]
    assert oos.sku == "170003"
    assert oos.in_stock is False


def test_falabella_respects_include_store_card_prices_false() -> None:
    settings = Settings(include_store_card_prices=False)
    html = _wrap_next_data(
        {
            "productData": {
                "id": "88888",
                "name": "Smart TV 55 4K",
                "prices": [
                    {"type": "cmrPrice", "price": ["$299.990"], "crossed": False},
                    {"type": "internetPrice", "price": ["$349.990"], "crossed": False},
                    {"type": "normalPrice", "price": ["$599.990"], "crossed": True},
                ],
                "isOutOfStock": False,
            }
        }
    )

    scraper = FalabellaScraper(settings)
    product = scraper.parse_product_html(
        html,
        product_url="https://www.falabella.com/falabella-cl/product/88888/smart-tv",
        category_name="Televisores",
    )

    assert product is not None
    assert product.price_clp == 349_990
    assert product.normal_price_clp == 599_990
    assert product.card_price_clp == 299_990
    assert product.payment_method == PaymentMethodType.ALL_PAYMENT_METHODS
    assert product.payment_method_label == "Todo medio de pago"


def test_falabella_json_ld_fallback(test_settings: Settings) -> None:
    html = """
    <html><head>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Product",
      "sku": "LD-999",
      "name": "Zapatilla Running Pro",
      "offers": {
        "@type": "Offer",
        "price": "24990",
        "priceCurrency": "CLP",
        "availability": "https://schema.org/InStock",
        "url": "https://www.falabella.com/falabella-cl/product/LD-999/zapatilla"
      }
    }
    </script>
    </head><body></body></html>
    """
    scraper = FalabellaScraper(test_settings)
    product = scraper.parse_product_html(
        html,
        product_url="https://www.falabella.com/falabella-cl/product/LD-999/zapatilla",
    )
    assert product is not None
    assert product.sku == "LD-999"
    assert product.price_clp == 24_990
    assert product.in_stock is True
    assert product.payment_method == PaymentMethodType.ALL_PAYMENT_METHODS
