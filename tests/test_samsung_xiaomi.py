from __future__ import annotations

from analyzer.models import PaymentMethodType
from config.settings import Settings
from scrapers.stores.samsung import SamsungScraper
from scrapers.stores.xiaomi import XiaomiScraper

SAMPLE_SAMSUNG_HTML = """
<html>
<head>
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "ItemList",
    "itemListElement": [
      {
        "@type": "ListItem",
        "item": {
          "@type": "Product",
          "@id": "https://www.samsung.com/cl/smartphones/galaxy-s/galaxy-s26-fe-128gb-sm-s741/",
          "name": "Galaxy S26 FE",
          "offers": {
            "@type": "Offer",
            "priceCurrency": "CLP",
            "price": "839990",
            "availability": "https://schema.org/InStock"
          }
        }
      },
      {
        "@type": "ListItem",
        "item": {
          "@type": "Product",
          "@id": "https://www.samsung.com/cl/tablets/galaxy-tab-s10-sm-x820/",
          "name": "Galaxy Tab S10",
          "offers": {
            "@type": "Offer",
            "priceCurrency": "CLP",
            "price": "599990.00",
            "highPrice": "799990.00",
            "availability": "https://schema.org/OutOfStock"
          }
        }
      }
    ]
  }
  </script>
</head>
<body></body>
</html>
"""

SAMPLE_XIAOMI_HTML = """
<html>
<body>
<script>
window.__PRELOADED_STATE__ = {
  "pagedata": {
    "product_list": {
      "dataProvider": {
        "total_count": 2,
        "data": [
          {
            "product": {"id": "35315", "name": "REDMI 17"},
            "commodity": [
              {
                "id": "3223734902",
                "name": "REDMI 17 Negro 256GB",
                "is_sale": 1,
                "has_store": 1,
                "market_price_max": "299990.00",
                "price_min": "249990.00",
                "item_link": "https://www.mi.com/cl/product/redmi-17/"
              },
              {
                "id": "3223734903",
                "name": "REDMI 17 Agotado",
                "is_sale": 1,
                "has_store": 0,
                "market_price_max": "299990.00",
                "price_min": "199990.00",
                "item_link": "https://www.mi.com/cl/product/redmi-17/"
              }
            ]
          }
        ]
      }
    }
  }
};
</script>
</body>
</html>
"""


def test_samsung_category_parsing(test_settings: Settings) -> None:
    scraper = SamsungScraper(test_settings)
    products = scraper.parse_category_html(SAMPLE_SAMSUNG_HTML, category_name="Smartphones")

    assert len(products) == 2

    s26 = products[0]
    assert s26.retailer_name == "Samsung"
    assert s26.sku == "galaxy-s26-fe-128gb-sm-s741"
    assert s26.price_clp == 839990
    assert s26.in_stock is True
    assert s26.payment_method == PaymentMethodType.ALL_PAYMENT_METHODS

    tab = products[1]
    assert tab.price_clp == 599990
    assert tab.normal_price_clp == 799990
    assert tab.in_stock is False


def test_xiaomi_category_parsing(test_settings: Settings) -> None:
    scraper = XiaomiScraper(test_settings)
    products = scraper.parse_category_html(SAMPLE_XIAOMI_HTML, category_name="Smartphones")

    assert len(products) == 2

    redmi = products[0]
    assert redmi.retailer_name == "Xiaomi"
    assert redmi.sku == "3223734902"
    assert redmi.price_clp == 249990
    assert redmi.normal_price_clp == 299990
    assert redmi.in_stock is True
    assert redmi.url == "https://www.mi.com/cl/product/redmi-17/"

    agotado = products[1]
    assert agotado.sku == "3223734903"
    assert agotado.price_clp == 199990
    assert agotado.in_stock is False
