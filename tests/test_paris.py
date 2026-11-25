from __future__ import annotations

from analyzer.models import PaymentMethodType
from config.settings import Settings
from scrapers.stores.paris import ParisScraper, _with_paris_page_param


SAMPLE_PARIS_CATEGORY_HTML = """
<html>
<body>
  <div data-cnstrc-item-id="924050999"
       data-cnstrc-item-name="Smartphone Galaxy S25 256GB Navy Liberado"
       data-cnstrc-item-price="599990">
    <a href="/smartphone-galaxy-s25-256gb-navy-liberado-924050999.html">Ver</a>
    <span>45%</span>
    <span>$589.990</span>
    <span>$599.990</span>
    <span>$1.069.990</span>
  </div>
  <div data-cnstrc-item-id="MKIHJRNHGM"
       data-cnstrc-item-name="Smartphone Xiaomi Poco C85 128GB"
       data-cnstrc-item-price="129990">
    <a href="/smartphone-xiaomi-poco-c85-MKIHJRNHGM.html">Ver</a>
    <span>$129.990</span>
    <span>$139.990</span>
  </div>
  <div data-cnstrc-item-id="OUT001"
       data-cnstrc-item-name="Tablet Agotada"
       data-cnstrc-item-price="99990">
    <a href="/tablet-agotada-OUT001.html">Ver</a>
    <span>Agotado</span>
    <span>$99.990</span>
  </div>
</body>
</html>
"""

SAMPLE_PARIS_PDP_HTML = """
<html>
<head>
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Smart TV Samsung 55 pulgadas 4K",
    "sku": "TV55SAM001",
    "url": "https://www.paris.cl/smart-tv-samsung-55-TV55SAM001.html",
    "offers": {
      "@type": "Offer",
      "price": "349990",
      "highPrice": "599990",
      "availability": "https://schema.org/InStock"
    }
  }
  </script>
</head>
<body></body>
</html>
"""


def test_paris_category_parsing_with_cencosud_price(test_settings: Settings) -> None:
    scraper = ParisScraper(test_settings)
    products = scraper.parse_category_html(SAMPLE_PARIS_CATEGORY_HTML, category_name="Smartphones")

    assert len(products) == 3

    s25 = products[0]
    assert s25.retailer_name == "Paris"
    assert s25.sku == "924050999"
    assert s25.price_clp == 589990
    assert s25.all_payment_price_clp == 599990
    assert s25.card_price_clp == 589990
    assert s25.normal_price_clp == 1069990
    assert s25.payment_method == PaymentMethodType.STORE_CARD
    assert s25.payment_method_label == "Exclusivo Tarjeta Cencosud"
    assert s25.in_stock is True
    assert s25.url == "https://www.paris.cl/smartphone-galaxy-s25-256gb-navy-liberado-924050999.html"

    poco = products[1]
    assert poco.sku == "MKIHJRNHGM"
    assert poco.price_clp == 129990
    assert poco.card_price_clp is None
    assert poco.normal_price_clp == 139990
    assert poco.payment_method == PaymentMethodType.ALL_PAYMENT_METHODS
    assert poco.payment_method_label == "Todo medio de pago"

    agotado = products[2]
    assert agotado.sku == "OUT001"
    assert agotado.in_stock is False


def test_paris_category_ignores_store_card_when_disabled(test_settings: Settings) -> None:
    settings_no_card = test_settings.model_copy(update={"include_store_card_prices": False})
    scraper = ParisScraper(settings_no_card)
    products = scraper.parse_category_html(SAMPLE_PARIS_CATEGORY_HTML, category_name="Smartphones")

    s25 = products[0]
    assert s25.price_clp == 599990
    assert s25.payment_method == PaymentMethodType.ALL_PAYMENT_METHODS


def test_paris_pdp_json_ld_parsing(test_settings: Settings) -> None:
    scraper = ParisScraper(test_settings)
    product = scraper.parse_product_html(
        SAMPLE_PARIS_PDP_HTML,
        product_url="https://www.paris.cl/smart-tv-samsung-55-TV55SAM001.html",
        category_name="Televisores",
    )

    assert product is not None
    assert product.sku == "TV55SAM001"
    assert product.price_clp == 349990
    assert product.normal_price_clp == 599990
    assert product.in_stock is True


def test_paris_page_param() -> None:
    base = "https://www.paris.cl/tecnologia/celulares/smartphone/"
    assert _with_paris_page_param(base, 1) == base
    assert _with_paris_page_param(base, 2) == f"{base}?page=2"
