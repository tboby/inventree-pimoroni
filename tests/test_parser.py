from pathlib import Path

from inventree_pimoroni.pimoroni import parse_product_html


def test_parse_product_html_extracts_core_fields():
    fixture = (
        Path(__file__).parent / "fixtures" / "pimoroni_product.html"
    ).read_text(encoding="utf-8")

    data = parse_product_html(
        "https://shop.pimoroni.com/products/pico-display-pack", fixture
    )

    assert data.sku == "PIM123"
    assert data.name == "Pico Display Pack"
    assert data.brand == "Pimoroni"
    assert data.currency == "GBP"
    assert str(data.price) == "12.34"
