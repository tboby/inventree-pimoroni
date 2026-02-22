# InvenTree Pimoroni Supplier Plugin

Import Pimoroni products into InvenTree using the built-in Supplier import flow.

## What this plugin does (v1)

- Search Pimoroni by name in the InvenTree import dialog
- Accept direct Pimoroni product URLs as search input
- Show standard InvenTree import preview and category selection
- Reuse existing records when possible
  - Reuse by existing Pimoroni supplier SKU first
  - Then reuse by manufacturer part match (if available)
  - Then reuse by exact part-name match
- Create and link:
  - `Part`
  - `ManufacturerPart` (when manufacturer / MPN can be extracted)
  - `SupplierPart` (supplier = configured Pimoroni company)

## Installation

Install in editable mode while developing:

```bash
pip install -e .
```

Then activate in InvenTree plugin settings.

## Configuration

- `Supplier`: must point to your Pimoroni supplier company in InvenTree
- `Default Currency`: fallback currency when page data has no currency (default: GBP)
- `Search Result Limit`: max number of search candidates (default: 5)

## Notes

- This implementation intentionally avoids private APIs and credentials.
- It parses public product/search pages and JSON-LD metadata.
- If Pimoroni changes page structure, parser updates may be required.
