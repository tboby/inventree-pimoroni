from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Any
from urllib.parse import urlparse

import requests

PIMORONI_BASE_URL = "https://shop.pimoroni.com"
PRODUCT_PATH_RE = re.compile(r"/products/[a-z0-9\-]+", re.IGNORECASE)
HREF_PRODUCT_RE = re.compile(r'href="(/products/[^"]+)"')
JSON_LD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
META_RE = re.compile(
    r'<meta[^>]+property="([^"]+)"[^>]+content="([^"]*)"[^>]*>',
    re.IGNORECASE,
)
STRIP_HTML_RE = re.compile(r"<[^>]+>")


@dataclass(slots=True)
class PimoroniPartData:
    part_id: str
    sku: str
    name: str
    description: str
    link: str
    image_url: str | None
    brand: str | None
    mpn: str | None
    price: Decimal | None
    currency: str | None

    def as_import_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "part_id": self.part_id,
            "sku": self.sku,
            "name": self.name,
            "description": self.description,
            "link": self.link,
            "image_url": self.image_url,
            "brand": self.brand,
            "mpn": self.mpn,
        }
        if self.price is not None and self.currency:
            payload["price"] = {1: [float(self.price), self.currency]}
        else:
            payload["price"] = {}
        return payload


class PimoroniClient:
    def __init__(self, timeout_s: int = 20):
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; inventree-pimoroni-plugin/0.1; +https://inventree.org/)"
                )
            }
        )

    def normalize_product_url(self, value: str) -> str | None:
        value = value.strip()
        if not value:
            return None
        if not value.startswith("http"):
            value = f"{PIMORONI_BASE_URL.rstrip('/')}/{value.lstrip('/')}"

        parsed = urlparse(value)
        if parsed.netloc not in {"shop.pimoroni.com", "pimoroni.com", "www.pimoroni.com"}:
            return None

        match = PRODUCT_PATH_RE.search(parsed.path)
        if not match:
            return None

        return f"{PIMORONI_BASE_URL}{match.group(0).lower()}"

    def fetch_product(self, product_url: str) -> PimoroniPartData:
        response = self.session.get(product_url, timeout=self.timeout_s)
        response.raise_for_status()
        return parse_product_html(product_url, response.text)

    def search(self, term: str, limit: int = 5) -> list[PimoroniPartData]:
        term = term.strip()
        if not term:
            return []

        url = self.normalize_product_url(term)
        if url:
            return [self.fetch_product(url)]

        response = self.session.get(
            f"{PIMORONI_BASE_URL}/search",
            params={"q": term, "type": "product"},
            timeout=self.timeout_s,
        )
        response.raise_for_status()

        links = []
        seen = set()
        for path in HREF_PRODUCT_RE.findall(response.text):
            if path in seen:
                continue
            seen.add(path)
            links.append(f"{PIMORONI_BASE_URL}{path.split('?')[0]}")
            if len(links) >= limit:
                break

        results: list[PimoroniPartData] = []
        for link in links:
            try:
                results.append(self.fetch_product(link))
            except Exception:
                continue
        return results


def parse_product_html(url: str, html: str) -> PimoroniPartData:
    product_data = _parse_json_ld_product(html)
    meta = _parse_meta_tags(html)

    name = _first_non_empty(
        _get_nested(product_data, "name"),
        meta.get("og:title"),
    )
    description = _clean_text(
        _first_non_empty(
            _get_nested(product_data, "description"),
            meta.get("og:description"),
            "",
        )
    )
    sku = _first_non_empty(
        _get_nested(product_data, "sku"),
        _get_nested(product_data, "productID"),
        _get_nested(product_data, "mpn"),
    )
    if not sku:
        raise ValueError(f"Could not determine SKU for product URL: {url}")

    brand = _coerce_brand(_get_nested(product_data, "brand"))
    mpn = _first_non_empty(_get_nested(product_data, "mpn"), sku)

    image_url = _coerce_image(_get_nested(product_data, "image"))
    if not image_url:
        image_url = meta.get("og:image")

    price, currency = _extract_price(_get_nested(product_data, "offers"), meta)

    return PimoroniPartData(
        part_id=sku,
        sku=sku,
        name=name or sku,
        description=description,
        link=url,
        image_url=image_url,
        brand=brand,
        mpn=mpn,
        price=price,
        currency=currency,
    )


def _parse_json_ld_product(html: str) -> dict[str, Any]:
    for raw_json in JSON_LD_RE.findall(html):
        raw_json = unescape(raw_json).strip()
        if not raw_json:
            continue
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            continue

        product = _find_product_payload(data)
        if product:
            return product
    return {}


def _find_product_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        for item in value:
            product = _find_product_payload(item)
            if product:
                return product
        return None

    if isinstance(value, dict):
        typ = value.get("@type")
        if typ == "Product" or (isinstance(typ, list) and "Product" in typ):
            return value

        graph = value.get("@graph")
        if graph:
            return _find_product_payload(graph)
    return None


def _parse_meta_tags(html: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for key, value in META_RE.findall(html):
        tags[key] = value
    return tags


def _extract_price(offers: Any, meta: dict[str, str]) -> tuple[Decimal | None, str | None]:
    offer_payload = offers
    if isinstance(offers, list) and offers:
        offer_payload = offers[0]

    raw_price = _get_nested(offer_payload, "price")
    raw_currency = _get_nested(offer_payload, "priceCurrency")

    if not raw_price:
        raw_price = _first_non_empty(meta.get("product:price:amount"), None)
    if not raw_currency:
        raw_currency = _first_non_empty(meta.get("product:price:currency"), None)

    if raw_price is None:
        return None, raw_currency

    try:
        return Decimal(str(raw_price)), (raw_currency or "GBP").upper()
    except (InvalidOperation, ValueError):
        return None, raw_currency


def _coerce_brand(raw_brand: Any) -> str | None:
    if isinstance(raw_brand, str):
        return raw_brand
    if isinstance(raw_brand, dict):
        return raw_brand.get("name")
    return None


def _coerce_image(raw_image: Any) -> str | None:
    if isinstance(raw_image, str):
        return raw_image
    if isinstance(raw_image, list):
        for image in raw_image:
            if isinstance(image, str):
                return image
    return None


def _get_nested(data: Any, key: str) -> Any:
    if isinstance(data, dict):
        return data.get(key)
    return None


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _clean_text(value: str) -> str:
    value = STRIP_HTML_RE.sub(" ", value)
    value = unescape(value)
    return " ".join(value.split())
