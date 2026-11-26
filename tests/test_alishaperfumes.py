from __future__ import annotations

import json

from analyzer.models import PaymentMethodType
from config.settings import Settings
from scrapers.stores.alishaperfumes import AlishaPerfumesScraper, _to_shopify_json_url

SAMPLE_SHOPIFY_JSON = json.dumps(
    {
        "products": [
            {
                "id": 9911697080602,
                "title": "AMARIGE DE GIVENCHY 100ML DAMA",
                "handle": "amarige-de-givenchy-100ml-dama",
                "variants": [
                    {
                        "id": 111,
                        "sku": "3274878122561",
                        "price": "53990",
                        "compare_at_price": "89990",
                        "available": True,
                    }
                ],
            },
            {
                "id": 9911745085722,
                "title": "PERFUME AGOTADO 100ML",
                "handle": "perfume-agotado-100ml",
                "variants": [
                    {
                        "id": 222,
                        "sku": "",
                        "price": "36990.00",
                        "compare_at_price": "66990.00",
                        "available": False,
                    }
                ],
            },
        ]
    }
)


def test_alisha_perfumes_json_parsing(test_settings: Settings) -> None:
    scraper = AlishaPerfumesScraper(test_settings)
    products = scraper.parse_products_json(SAMPLE_SHOPIFY_JSON, category_name="Perfumes")

    assert len(products) == 2

    givenchy = products[0]
    assert givenchy.retailer_name == "Alisha Perfumes"
    assert givenchy.sku == "3274878122561"
    assert givenchy.price_clp == 53990
    assert givenchy.normal_price_clp == 89990
    assert givenchy.in_stock is True
    assert givenchy.payment_method == PaymentMethodType.ALL_PAYMENT_METHODS
    assert givenchy.url == "https://alishaperfumes.cl/products/amarige-de-givenchy-100ml-dama"

    agotado = products[1]
    assert agotado.sku == "222"
    assert agotado.price_clp == 36990
    assert agotado.normal_price_clp == 66990
    assert agotado.in_stock is False


def test_shopify_json_url_builder() -> None:
    assert (
        _to_shopify_json_url("https://alishaperfumes.cl/products.json", page=2, limit=250)
        == "https://alishaperfumes.cl/products.json?limit=250&page=2"
    )
    assert (
        _to_shopify_json_url("https://alishaperfumes.cl/collections/arabes", page=1, limit=250)
        == "https://alishaperfumes.cl/collections/arabes/products.json?limit=250&page=1"
    )
