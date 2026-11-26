from __future__ import annotations

import json

from analyzer.models import PaymentMethodType
from config.settings import Settings
from scrapers.stores.solotodo import SoloTodoScraper

SAMPLE_SOLOTODO_JSON = json.dumps(
    {
        "count": 2,
        "next": "https://publicapi.solotodo.com/categories/6/browse/?page=2",
        "results": [
            {
                "bucket": "1",
                "product_entries": [
                    {
                        "product": {
                            "id": 264814,
                            "name": "Samsung Galaxy S25 Ultra 256 GB",
                        },
                        "metadata": {
                            "prices_per_currency": [
                                {
                                    "currency_id": 1,
                                    "normal_price": "759990.0",
                                    "offer_price": "699990.0",
                                }
                            ]
                        },
                    }
                ],
            },
            {
                "bucket": "2",
                "product_entries": [
                    {
                        "product": {
                            "id": 300001,
                            "name": "Xiaomi Redmi Note 14",
                        },
                        "metadata": {
                            "prices_per_currency": [
                                {
                                    "currency_id": 1,
                                    "normal_price": "199990.0",
                                    "offer_price": "199990.0",
                                }
                            ]
                        },
                    }
                ],
            },
        ],
    }
)


def test_solotodo_browse_json_parsing(test_settings: Settings) -> None:
    scraper = SoloTodoScraper(test_settings)
    products, has_next = scraper.parse_browse_json(SAMPLE_SOLOTODO_JSON, category_name="Smartphones")

    assert has_next is True
    assert len(products) == 2

    s25 = products[0]
    assert s25.retailer_name == "SoloTodo"
    assert s25.sku == "264814"
    assert s25.price_clp == 699990
    assert s25.normal_price_clp == 759990
    assert s25.all_payment_price_clp == 759990
    assert s25.payment_method == PaymentMethodType.STORE_CARD
    assert s25.url == "https://www.solotodo.cl/products/264814"

    redmi = products[1]
    assert redmi.sku == "300001"
    assert redmi.price_clp == 199990
    assert redmi.normal_price_clp is None
    assert redmi.payment_method == PaymentMethodType.ALL_PAYMENT_METHODS
