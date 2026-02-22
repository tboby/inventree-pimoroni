from __future__ import annotations

from typing import Any

from company.models import Company, ManufacturerPart
from part.models import Part, SupplierPart, SupplierPriceBreak
from plugin import InvenTreePlugin
from plugin.base import supplier
from plugin.mixins import SupplierMixin

from .pimoroni import PimoroniClient, PimoroniPartData
from .version import __version__


class PimoroniSupplierPlugin(SupplierMixin, InvenTreePlugin):
    """Import Pimoroni products into InvenTree."""

    NAME = "PimoroniSupplierPlugin"
    SLUG = "pimoroni"
    TITLE = "Pimoroni Supplier Importer"

    VERSION = __version__

    def __init__(self):
        super().__init__()
        self.client = PimoroniClient()
        self._cache: dict[str, PimoroniPartData] = {}

        self.SETTINGS.update(
            {
                "SEARCH_RESULT_LIMIT": {
                    "name": "Search Result Limit",
                    "description": "Maximum number of Pimoroni products to return per search.",
                    "default": 5,
                    "validator": "integer",
                },
                "DEFAULT_CURRENCY": {
                    "name": "Default Currency",
                    "description": "Fallback currency if no currency is present in product metadata.",
                    "default": "GBP",
                },
            }
        )

    def get_suppliers(self) -> list[supplier.Supplier]:
        return [supplier.Supplier(slug="pimoroni", name="Pimoroni")]

    def get_search_results(
        self, supplier_slug: str, term: str
    ) -> list[supplier.SearchResult]:
        if supplier_slug != "pimoroni":
            return []

        limit = int(self.get_setting("SEARCH_RESULT_LIMIT") or 5)
        candidates = self.client.search(term=term, limit=max(1, min(limit, 20)))

        results: list[supplier.SearchResult] = []
        for candidate in candidates:
            self._cache[candidate.part_id] = candidate
            existing = SupplierPart.objects.filter(
                supplier=self.supplier_company,
                SKU__iexact=candidate.sku,
            ).first()

            price = ""
            if candidate.price is not None and candidate.currency:
                price = f"{candidate.price} {candidate.currency}"

            results.append(
                supplier.SearchResult(
                    sku=candidate.part_id,
                    name=candidate.name,
                    description=candidate.description,
                    exact=candidate.sku.lower() == term.strip().lower(),
                    price=price,
                    link=candidate.link,
                    image_url=candidate.image_url,
                    existing_part=getattr(existing, "part", None),
                )
            )
        return results

    def get_import_data(self, supplier_slug: str, part_id: str) -> dict[str, Any]:
        if supplier_slug != "pimoroni":
            raise supplier.PartNotFoundError()

        candidate = self._cache.get(part_id)
        if not candidate:
            url = self.client.normalize_product_url(part_id)
            if url:
                candidate = self.client.fetch_product(url)
            else:
                search_results = self.client.search(part_id, limit=10)
                exact = next(
                    (r for r in search_results if r.sku.lower() == part_id.lower()),
                    None,
                )
                if exact is None:
                    raise supplier.PartNotFoundError()
                candidate = exact
            self._cache[candidate.part_id] = candidate

        payload = candidate.as_import_payload()

        currency = self.get_setting("DEFAULT_CURRENCY") or "GBP"
        if not payload["price"] and candidate.price is not None:
            payload["price"] = {1: [float(candidate.price), currency]}

        return payload

    def get_pricing_data(self, data: dict[str, Any]) -> dict[int, tuple[float, str]]:
        return data.get("price", {})

    def get_parameters(self, data: dict[str, Any]) -> list[supplier.ImportParameter]:
        params: list[supplier.ImportParameter] = []
        if data.get("brand"):
            params.append(supplier.ImportParameter(name="Brand", value=data["brand"]))
        if data.get("mpn"):
            params.append(supplier.ImportParameter(name="MPN", value=data["mpn"]))
        return params

    def import_part(self, data: dict[str, Any], **kwargs) -> Part:
        existing = self._find_existing_part(data)
        if existing:
            self._fill_missing_part_fields(existing, data)
            return existing

        part = Part.objects.create(
            name=data["name"],
            description=data.get("description", ""),
            link=data.get("link", ""),
            purchaseable=True,
            **kwargs,
        )

        image_url = data.get("image_url")
        if image_url:
            try:
                file_obj, fmt = self.download_image(image_url)
                filename = f"pimoroni_part_{part.pk}.{fmt.lower()}"
                part.image.save(filename, file_obj)
            except Exception:
                pass

        return part

    def import_manufacturer_part(self, data: dict[str, Any], **kwargs) -> ManufacturerPart:
        brand = data.get("brand") or "Pimoroni"
        mpn = data.get("mpn") or data["sku"]

        manufacturer, _ = Company.objects.get_or_create(
            name__iexact=brand,
            defaults={
                "name": brand,
                "is_manufacturer": True,
                "is_supplier": False,
            },
        )

        mfg_part, _ = ManufacturerPart.objects.get_or_create(
            manufacturer=manufacturer,
            MPN=mpn,
            defaults=kwargs,
        )

        if mfg_part.part_id != kwargs.get("part").pk:
            mfg_part.part = kwargs["part"]
            mfg_part.save()

        return mfg_part

    def import_supplier_part(self, data: dict[str, Any], **kwargs) -> SupplierPart:
        sku = data["sku"]
        supplier_part, _ = SupplierPart.objects.get_or_create(
            supplier=self.supplier_company,
            SKU=sku,
            defaults={
                "link": data.get("link", ""),
                **kwargs,
            },
        )

        part = kwargs.get("part")
        manufacturer_part = kwargs.get("manufacturer_part")

        changed = False
        if part is not None and supplier_part.part_id != part.pk:
            supplier_part.part = part
            changed = True
        if (
            manufacturer_part is not None
            and supplier_part.manufacturer_part_id != manufacturer_part.pk
        ):
            supplier_part.manufacturer_part = manufacturer_part
            changed = True
        if not supplier_part.link and data.get("link"):
            supplier_part.link = data["link"]
            changed = True
        if changed:
            supplier_part.save()

        SupplierPriceBreak.objects.filter(part=supplier_part).delete()
        for quantity, payload in data.get("price", {}).items():
            SupplierPriceBreak.objects.create(
                part=supplier_part,
                quantity=int(quantity),
                price=payload[0],
                price_currency=payload[1],
            )

        return supplier_part

    def _find_existing_part(self, data: dict[str, Any]) -> Part | None:
        existing_supplier_part = SupplierPart.objects.filter(
            supplier=self.supplier_company,
            SKU__iexact=data["sku"],
        ).first()
        if existing_supplier_part:
            return existing_supplier_part.part

        if data.get("mpn"):
            existing_mpn = ManufacturerPart.objects.filter(MPN__iexact=data["mpn"]).first()
            if existing_mpn:
                return existing_mpn.part

        if data.get("name"):
            same_name = Part.objects.filter(name__iexact=data["name"]).first()
            if same_name:
                return same_name

        return None

    def _fill_missing_part_fields(self, part: Part, data: dict[str, Any]) -> None:
        changed = False
        if not part.description and data.get("description"):
            part.description = data["description"]
            changed = True
        if not part.link and data.get("link"):
            part.link = data["link"]
            changed = True

        if changed:
            part.save()
