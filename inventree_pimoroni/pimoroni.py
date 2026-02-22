from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlparse

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

        normalized = f"{PIMORONI_BASE_URL}{match.group(0).lower()}"

        variant = parse_qs(parsed.query).get("variant", [None])[0]
        if variant and variant.isdigit():
            return f"{normalized}?variant={variant}"

        return normalized

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
                response = self.session.get(link, timeout=self.timeout_s)
                response.raise_for_status()
                results.append(parse_product_html(link, response.text, preferred_term=term))
            except Exception:
                continue
        return results


def parse_product_html(
    url: str, html: str, preferred_term: str | None = None
) -> PimoroniPartData:
    product_data = _parse_json_ld_product(html)
    meta = _parse_meta_tags(html)
    variants = _parse_embedded_variants(html)
    selected_variant = _select_variant(url, variants, preferred_term)

    base_name = _first_non_empty(
        _get_nested(product_data, "name"),
        meta.get("og:title"),
    )
    name = _first_non_empty(
        _get_nested(selected_variant, "name"),
        base_name,
    )
    description = _clean_text(
        _first_non_empty(
            _get_nested(product_data, "description"),
            meta.get("og:description"),
            "",
        )
    )
    sku = _first_non_empty(
        _get_nested(selected_variant, "sku"),
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

    variant_id = _extract_variant_id(url)
    price, currency = _extract_price(
        _get_nested(product_data, "offers"),
        meta,
        variant_id=variant_id,
        variant_sku=sku,
    )

    variant_price = _coerce_variant_price(_get_nested(selected_variant, "price"))
    if variant_price is not None:
        price = variant_price

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


def _extract_price(
    offers: Any,
    meta: dict[str, str],
    variant_id: str | None = None,
    variant_sku: str | None = None,
) -> tuple[Decimal | None, str | None]:
    offer_payload = offers
    if isinstance(offers, list) and offers:
        matched_offer = None

        if variant_id:
            for offer in offers:
                offer_url = _get_nested(offer, "url")
                if not offer_url:
                    continue
                offer_variant_id = _extract_variant_id(str(offer_url))
                if offer_variant_id and offer_variant_id == variant_id:
                    matched_offer = offer
                    break

        if matched_offer is None and variant_sku:
            for offer in offers:
                offer_sku = str(_get_nested(offer, "sku") or "").strip().lower()
                if offer_sku and offer_sku == variant_sku.strip().lower():
                    matched_offer = offer
                    break

        offer_payload = matched_offer or offers[0]

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


def _extract_variant_id(url: str) -> str | None:
    try:
        variant = parse_qs(urlparse(url).query).get("variant", [None])[0]
    except Exception:
        return None

    if variant and str(variant).isdigit():
        return str(variant)

    return None


def _parse_embedded_variants(html: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()

    for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.IGNORECASE | re.DOTALL):
        if '"variants":[' not in script:
            continue

        for match in re.finditer(r"\{", script):
            try:
                payload, _ = decoder.raw_decode(script, match.start())
            except Exception:
                continue

            if not isinstance(payload, dict):
                continue

            variants = _get_nested(payload, "variants")
            if not isinstance(variants, list):
                continue

            parsed: list[dict[str, Any]] = []
            for variant in variants:
                if not isinstance(variant, dict):
                    continue

                sku = str(_get_nested(variant, "sku") or "").strip()
                if not sku:
                    continue

                parsed.append(
                    {
                        "id": str(_get_nested(variant, "id") or "").strip(),
                        "sku": sku,
                        "name": _first_non_empty(
                            _get_nested(variant, "name"),
                            _get_nested(variant, "public_title"),
                        ),
                        "price": _get_nested(variant, "price"),
                    }
                )

            if parsed:
                return parsed

    return []


def _select_variant(
    url: str, variants: list[dict[str, Any]], preferred_term: str | None
) -> dict[str, Any]:
    if not variants:
        return {}

    variant_id = _extract_variant_id(url)
    if variant_id:
        for variant in variants:
            if _get_nested(variant, "id") == variant_id:
                return variant

    if preferred_term:
        term = preferred_term.strip().lower()
        term_token = _token(preferred_term)

        for variant in variants:
            sku = str(_get_nested(variant, "sku") or "").strip().lower()
            if sku and sku == term:
                return variant

        for variant in variants:
            name = str(_get_nested(variant, "name") or "").strip().lower()
            if name and term in name:
                return variant

        for variant in variants:
            sku = str(_get_nested(variant, "sku") or "")
            name = str(_get_nested(variant, "name") or "")
            if term_token and (term_token in _token(sku) or term_token in _token(name)):
                return variant

    return variants[0]


def _token(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _coerce_variant_price(raw_price: Any) -> Decimal | None:
    if raw_price in (None, ""):
        return None

    try:
        if isinstance(raw_price, int):
            return Decimal(raw_price) / Decimal("100")

        text = str(raw_price).strip()
        if text.isdigit():
            return Decimal(text) / Decimal("100")

        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None
