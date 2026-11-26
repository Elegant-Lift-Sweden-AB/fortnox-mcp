"""Fortnox MCP server: day-to-day work in Fortnox from an AI assistant.

Articles, prices, stock, stock corrections, manual deliveries, purchase orders, goods receipt, customers, suppliers,
offers, orders, invoices, credit notes and payments. One tool per action, so a client or gateway can grant each action
separately (read vs create vs book vs send). Every write tool has `dry_run`: it returns the exact request(s) it would
send, without sending. `api_request` reaches any Fortnox endpoint the named tools don't cover.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import sys
from datetime import date
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import BlobResourceContents, EmbeddedResource, TextContent, ToolAnnotations

from . import roles
from .client import Fortnox, FortnoxError, dry_run, enc, gs1_valid
from .payment_evidence import invoice_evidence
from .sanitize import dropped_chars, fortnox_safe_title


def _env_json(name: str) -> dict:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except ValueError as e:
        raise SystemExit(f"{name} is not valid JSON: {e}")
    if not isinstance(val, dict):
        raise SystemExit(f"{name} must be a JSON object")
    return val


# ---------------------------------------------------------------- configuration (environment variables, see README)
PRICE_LIST = os.environ.get("FORTNOX_PRICE_LIST", "").strip() or "A"       # the sales price list the tools use
STOCK_POINT_CODE = os.environ.get("FORTNOX_STOCK_POINT", "").strip() or None  # unset = the only active stock point
# Fields every new article gets. Fortnox fills accounts and VAT from the company settings unless you set them here,
# e.g. {"VAT": 25, "SalesAccount": 3001, "PurchaseAccount": 4000}.
ARTICLE_DEFAULTS = {"Type": "STOCK", "StockGoods": True, **_env_json("FORTNOX_ARTICLE_DEFAULTS")}
ARTICLE_NUMBER_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-./_+")

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))


def _transport_security() -> TransportSecuritySettings:
    """HTTP mode: only accept requests whose Host header is on the list (blocks DNS-rebinding from web pages).

    Default: localhost / 127.0.0.1 on PORT. Behind a proxy or on another name, list the names in MCP_ALLOWED_HOSTS
    (comma-separated, e.g. "fortnox.example.com,fortnox.example.com:443"); "*" switches the check off.
    """
    raw = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
    if raw == "*":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = [h.strip() for h in raw.split(",") if h.strip()] or [
        f"localhost:{PORT}", f"127.0.0.1:{PORT}", f"[::1]:{PORT}", "localhost", "127.0.0.1"]
    origins = [f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


fx = Fortnox()
ROLE = roles.load_role_from_env()  # None = full access; else the company's role file (FORTNOX_ROLE_FILE)
mcp = FastMCP(
    "fortnox",
    instructions=(
        "Fortnox is the company's register for articles (ArticleNumber = SKU), stock, purchasing and sales documents. "
        "Every write tool has dry_run=true to preview the exact request. Stock only changes through warehouse documents "
        "(goods receipt, stock correction, manual in/outbound, invoices with delivered quantity), never by editing an "
        "article. Nothing in Fortnox is truly deleted: offers/orders cancel, articles deactivate, sent invoices are "
        "credited, never cancelled."
        + (" " + ROLE.instructions if ROLE and ROLE.instructions else "")
    ),
    host=HOST,
    port=PORT,
    transport_security=_transport_security(),
    streamable_http_path="/mcp",
    stateless_http=True,  # no server-side sessions: survives restarts behind a proxy
)




def tool(**kwargs):
    """Register a tool with plain (unstructured) output on every Python version.

    FastMCP otherwise builds an output schema for some return annotations (`Any` on Python 3.10), which adds a
    second, structured copy of the result to each answer. One output channel keeps the role scrubbing simple.
    """
    return mcp.tool(structured_output=False, **kwargs)


# ====================================================================== helpers

def _brief(e: FortnoxError) -> str:
    """Status and code only: raw Fortnox text stays out of answers (it can contain values a role hides)."""
    return f"Fortnox HTTP {e.status}" + (f" code {e.code}" if e.code else "")


async def _call(coro):
    try:
        return await coro
    except FortnoxError as e:
        raise ToolError(str(e)) from e


async def _write(dry: bool, fn):
    """Run a write sequence; in dry-run mode return the recorded requests instead."""
    try:
        with dry_run(dry) as rec:
            result = await fn()
        if dry:
            return {"dry_run": True, "would_send": rec, "note": "nothing was changed"}
        return result
    except FortnoxError as e:
        raise ToolError(str(e)) from e


_stock_point_cache: dict | None = None


async def _stock_point() -> dict:
    """The stock point this server works on: FORTNOX_STOCK_POINT, or the only active one. Read once, then cached."""
    global _stock_point_cache
    if _stock_point_cache is None:
        res = await fx.get("/api/warehouse/stockpoints-v1")
        if isinstance(res, list):
            points = res
        elif isinstance(res, dict) and res.get("code"):
            points = [res]
        else:
            points = list((res or {}).get("stockPoints") or [])
        if STOCK_POINT_CODE:
            match = [sp for sp in points if sp.get("code") == STOCK_POINT_CODE]
            if not match:
                raise ToolError(f"Stock point {STOCK_POINT_CODE!r} (FORTNOX_STOCK_POINT) was not found in Fortnox; "
                                f"stock points: {[sp.get('code') for sp in points]}")
            _stock_point_cache = match[0]
        else:
            active = [sp for sp in points if sp.get("active", True)]
            if len(active) != 1:
                raise ToolError(f"Fortnox has {len(active)} active stock points {[sp.get('code') for sp in active]}; "
                                "set FORTNOX_STOCK_POINT to the code this server should use")
            _stock_point_cache = active[0]
    return _stock_point_cache


async def _delivery_address() -> dict:
    """Where purchase orders are delivered: the stock point's own address, else the company address."""
    sp = await _stock_point()
    if sp.get("usingCompanyAddress") is False and sp.get("deliveryAddress"):
        addr = {"deliveryName": sp.get("deliveryName") or sp.get("name"), "deliveryAddress": sp.get("deliveryAddress"),
                "deliveryAddress2": sp.get("deliveryAddress2"), "deliveryZipCode": sp.get("deliveryZipCode"),
                "deliveryCity": sp.get("deliveryCity"), "deliveryCountryCode": sp.get("deliveryCountryCode")}
    else:
        ci = (await fx.get("/3/companyinformation")).get("CompanyInformation", {})
        addr = {"deliveryName": ci.get("CompanyName"), "deliveryAddress": ci.get("Address"),
                "deliveryZipCode": ci.get("ZipCode"), "deliveryCity": ci.get("City"),
                "deliveryCountryCode": ci.get("CountryCode")}
    return {k: v for k, v in addr.items() if v}


def _today() -> str:
    return date.today().isoformat()


def _plus_month(d: str | None = None) -> str:
    base = date.fromisoformat(d) if d else date.today()
    m = base.month % 12 + 1
    y = base.year + (1 if base.month == 12 else 0)
    day = min(base.day, [31, 29 if y % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day).isoformat()


# Article fields that have their own, checked parameter: extra_fields may not set them (that would skip the check).
GUARDED_ARTICLE_FIELDS = {"ArticleNumber": "article_number", "Description": "description", "EAN": "ean / clear_ean",
                          "WebshopArticle": "webshop_article / set_webshop_article",
                          "QuantityInStock": "a goods receipt, stock correction or manual delivery",
                          "DisposableQuantity": "a goods receipt, stock correction or manual delivery"}


def _check_extra_article_fields(extra: dict | None) -> None:
    for k in extra or {}:
        if k in GUARDED_ARTICLE_FIELDS:
            raise ToolError(f"Article field {k!r} can't be set through extra_fields; use {GUARDED_ARTICLE_FIELDS[k]} "
                            f"(it is checked there)")


def _check_article_number(n: str) -> None:
    bad = sorted({c for c in n if c not in ARTICLE_NUMBER_CHARS})
    if not n or len(n) > 50 or bad:
        raise ToolError(f"Article number {n!r} not allowed: max 50 chars, only A-Z a-z 0-9 - . / _ + (bad: {bad})")


def _clean_description(desc: str) -> tuple[str, list[str]]:
    """Fortnox silently deletes characters it doesn't accept; clean first and say what changed."""
    warnings = []
    lost = dropped_chars(desc)
    clean = fortnox_safe_title(desc)
    if clean != desc.strip():
        warnings.append(f"Description adjusted for Fortnox: {desc!r} -> {clean!r}" + (f" (removed {lost})" if lost else ""))
    if not clean:
        raise ToolError("Description is empty after removing characters Fortnox does not accept")
    return clean, warnings


async def _check_ean(ean: str, article_number: str | None = None) -> list[str]:
    if not gs1_valid(ean):
        raise ToolError(f"EAN {ean!r} fails the GS1 check digit (or is not 8/12/13/14 digits).")
    res = await fx.get("/3/articles", ean=ean)  # active and inactive: a retired article may still hold the EAN
    others = [a["ArticleNumber"] for a in res.get("Articles", []) if a.get("EAN") == ean and a.get("ArticleNumber") != article_number]
    if others:
        raise ToolError(f"EAN {ean} is already on article(s) {others}. Barcodes must be unique.")
    return []


async def _customer(number: str) -> dict:
    return (await fx.get(f"/3/customers/{enc(number)}"))["Customer"]


def _language(country: str | None) -> str:
    return "SV" if (country or "SE").upper() == "SE" else "EN"


async def _currency_rate(code: str) -> tuple[float, int]:
    code = code.upper()
    if code == "SEK":
        return 1.0, 1
    c = (await fx.get(f"/3/currencies/{enc(code)}"))["Currency"]
    return float(c["BuyRate"]), int(c.get("Unit") or 1)


def _rows_sales(rows: list[dict], delivered: bool) -> list[dict]:
    out = []
    for r in rows:
        if "article_number" not in r and "description" not in r:
            raise ToolError("Each row needs article_number (or a description for a text row)")
        row: dict[str, Any] = {}
        if r.get("article_number"):
            row["ArticleNumber"] = r["article_number"]
        if r.get("description"):
            row["Description"] = fortnox_safe_title(r["description"])
        qty = r.get("quantity")
        if qty is not None:
            row["DeliveredQuantity" if delivered else "Quantity"] = str(qty)
        if r.get("price") is not None:
            row["Price"] = r["price"]
        if r.get("discount"):
            row["Discount"] = r["discount"]
            row["DiscountType"] = r.get("discount_type", "PERCENT")
        if r.get("unit"):
            row["Unit"] = r["unit"]
        row.update(r.get("extra") or {})
        out.append(row)
    return out


# ====================================================================== articles — read

@tool()
async def get_article(article_number: str) -> dict:
    """Everything about one article (SKU): register data, stock (in stock / available / reserved), sales price,
    order point + max stock, and supplier entries (supplier, supplier part number, purchase price)."""
    async def go():
        a = (await fx.get(f"/3/articles/{enc(article_number)}"))["Article"]
        out: dict[str, Any] = {"article": a}
        try:
            out["order_point"] = await fx.get(f"/api/warehouse/itemstockpointdata-v1/{enc(article_number)}")
        except FortnoxError as e:
            out["order_point_error"] = _brief(e)
        try:
            out["suppliers"] = await fx.request("GET", f"/api/warehouse/itemsuppliers-v1/{enc(article_number)}", retry_404=4)
        except FortnoxError as e:
            out["suppliers"] = None
            out["suppliers_note"] = f"no supplier entries returned ({e.status})"
        return out
    return await _call(go())


@tool()
async def search_articles(
    text: str | None = None,
    article_number: str | None = None,
    description: str | None = None,
    ean: str | None = None,
    supplier_number: str | None = None,
    manufacturer: str | None = None,
    webshop: bool | None = None,
    status: Literal["active", "inactive"] = "active",
    sort_by: Literal["articlenumber", "quantityinstock", "reservedquantity", "stockvalue"] | None = None,
    page: int = 1,
    limit: int = 100,
) -> dict:
    """Search the article register. `text` searches description. Rows include stock, prices, EAN, webshop flag.
    Always filtered on active/inactive (the unfiltered list hides which is which)."""
    params = {"filter": status, "articlenumber": article_number, "description": description or text, "ean": ean,
              "suppliernumber": supplier_number, "manufacturer": manufacturer, "page": page, "limit": min(limit, 500),
              "sortby": sort_by}
    if webshop is not None:
        params["webshop"] = str(webshop).lower()
    res = await _call(fx.get("/3/articles", **params))
    return {"articles": res.get("Articles", []), "paging": res.get("MetaInformation")}


@tool()
async def get_price(article_number: str, price_list: str | None = None) -> dict:
    """Sales price of an article on a price list (default: the server's price list, FORTNOX_PRICE_LIST)."""
    return await _call(fx.get(f"/3/prices/{enc(price_list or PRICE_LIST)}/{enc(article_number)}/0"))


# ====================================================================== articles — write

@tool()
async def create_article(
    article_number: str,
    description: str,
    ean: str | None = None,
    manufacturer: str | None = None,
    manufacturer_article_number: str | None = None,
    supplier_number: str | None = None,
    weight_grams: float | None = None,
    sales_price: float | None = None,
    webshop_article: bool = False,
    unit: str | None = None,
    note: str | None = None,
    extra_fields: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """Create a stock article. Applies the server's article defaults (FORTNOX_ARTICLE_DEFAULTS; Type STOCK,
    StockGoods). The description is cleaned of characters Fortnox would silently delete, and an EAN is checked
    (GS1 check digit, not already used). If sales_price is given it is set on the price list BEFORE the webshop flag,
    so a webshop never shows the product at 0. Articles can't be deleted once used on any document."""
    _check_article_number(article_number)
    _check_extra_article_fields(extra_fields)
    clean, warnings = _clean_description(description)
    if webshop_article and not sales_price:
        raise ToolError("webshop_article=true needs a sales_price > 0 (otherwise the product goes live at 0 kr)")

    async def go():
        if ean:
            await _check_ean(ean, article_number)
        art = {**ARTICLE_DEFAULTS, **(extra_fields or {}), "ArticleNumber": article_number, "Description": clean,
               "WebshopArticle": False}
        for k, v in {"EAN": ean, "Manufacturer": manufacturer, "ManufacturerArticleNumber": manufacturer_article_number,
                     "SupplierNumber": supplier_number, "Weight": weight_grams, "Unit": unit, "Note": note}.items():
            if v is not None:
                art[k] = v
        created = await fx.request("POST", "/3/articles", json={"Article": art})
        if sales_price:
            await _set_price(article_number, sales_price, PRICE_LIST)
        if webshop_article:
            await fx.request("PUT", f"/3/articles/{enc(article_number)}", json={"Article": {"WebshopArticle": True}})
        return {"created": created.get("Article", created), "warnings": warnings}
    return await _write(dry_run, go)


@tool()
async def update_article(
    article_number: str,
    description: str | None = None,
    ean: str | None = None,
    clear_ean: bool = False,
    manufacturer: str | None = None,
    manufacturer_article_number: str | None = None,
    supplier_number: str | None = None,
    weight_grams: float | None = None,
    purchase_price: float | None = None,
    stock_warning: float | None = None,
    stock_goods: bool | None = None,
    active: bool | None = None,
    note: str | None = None,
    extra_fields: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """Change article fields. Description is cleaned of characters Fortnox would silently delete; EAN is checked
    (valid + unique). Never changes stock quantities (use goods receipt / stock correction).
    Price: use set_price. Webshop flag: use set_webshop_article."""
    fields: dict[str, Any] = {}
    warnings: list[str] = []
    if description is not None:
        fields["Description"], warnings = _clean_description(description)
    for k, v in {"Manufacturer": manufacturer, "ManufacturerArticleNumber": manufacturer_article_number,
                 "SupplierNumber": supplier_number, "Weight": weight_grams, "PurchasePrice": purchase_price,
                 "StockWarning": stock_warning, "StockGoods": stock_goods, "Active": active, "Note": note}.items():
        if v is not None:
            fields[k] = v
    if clear_ean:
        fields["EAN"] = None  # "" is silently ignored by Fortnox; null clears
    extra = dict(extra_fields or {})
    if any(k in extra for k in ("QuantityInStock", "DisposableQuantity")):
        raise ToolError("Stock is never changed on the article. Use start_stock_correction, a goods receipt or a manual delivery.")
    _check_extra_article_fields(extra)
    fields = {**extra, **fields}

    async def go():
        if ean:
            await _check_ean(ean, article_number)
            fields["EAN"] = ean
        if not fields:
            raise ToolError("Nothing to change")
        res = await fx.request("PUT", f"/3/articles/{enc(article_number)}", json={"Article": fields})
        return {"article": res.get("Article", res), "warnings": warnings}
    return await _write(dry_run, go)


async def _set_price(article_number: str, price: float, price_list: str) -> dict:
    path = f"/3/prices/{enc(price_list)}/{enc(article_number)}/0"
    try:
        await fx.get(path)
        exists = True
    except FortnoxError as e:
        if e.status != 404:
            raise
        exists = False
    if exists:
        return await fx.request("PUT", path, json={"Price": {"Price": price}})
    return await fx.request("POST", "/3/prices", json={"Price": {"ArticleNumber": article_number, "PriceList": price_list,
                                                                  "FromQuantity": 0, "Price": price}})


@tool()
async def set_price(article_number: str, price: float, price_list: str | None = None, dry_run: bool = False) -> dict:
    """Set the sales price (company currency, ex VAT) on a price list (default: the server's price list,
    FORTNOX_PRICE_LIST). Creates the price row if the article has none on that list."""
    if price <= 0:
        raise ToolError("Price must be > 0")
    return await _write(dry_run, lambda: _set_price(article_number, price, price_list or PRICE_LIST))


@tool()
async def set_webshop_article(article_number: str, on: bool, dry_run: bool = False) -> dict:
    """Tick/untick 'Webbshopsartikel'. Webshop integrations use this flag to publish the article, so ticking it is
    refused while the article has no price on the server's price list (it would go live at 0)."""
    async def go():
        if on:
            try:
                p = await fx.get(f"/3/prices/{enc(PRICE_LIST)}/{enc(article_number)}/0")
                price = float(p.get("Price", {}).get("Price") or 0)
            except FortnoxError:
                price = 0
            if price <= 0:
                raise ToolError(f"No price on price list {PRICE_LIST} — set_price first, otherwise the product goes live at 0")
        return await fx.request("PUT", f"/3/articles/{enc(article_number)}", json={"Article": {"WebshopArticle": on}})
    return await _write(dry_run, go)


@tool()
async def set_order_point(article_number: str, order_point: float, max_stock: float | None = None, dry_run: bool = False) -> dict:
    """Set 'Beställningspunkt' (order point) and max stock on the server's stock point. -1 = not stocked, order when
    sold. (This is not the article's StockWarning/'Lagervarning' field.)"""
    # Fortnox answers HTTP 500 {"error":"internal"} for decimals (-1.0, 0.0); whole numbers must go out as JSON ints.
    def whole(x):
        return int(x) if float(x).is_integer() else x

    async def go():
        sp = await _stock_point()
        row = {"itemId": article_number, "stockPointId": sp["id"], "orderPointQuantity": whole(order_point)}
        if max_stock is not None:
            row["maxStock"] = whole(max_stock)
        return await fx.request("POST", f"/api/warehouse/itemstockpointdata-v1/{enc(article_number)}", json=[row])
    return await _write(dry_run, go)


@tool()
async def set_article_supplier(
    article_number: str,
    supplier_number: str,
    price: float | None = None,
    currency: str | None = None,
    supplier_article_number: str | None = None,
    supplier_article_name: str | None = None,
    delivery_time_days: int | None = None,
    minimum_quantity: float | None = None,
    remove: bool = False,
    article_has_no_supplier_entries: bool = False,
    dry_run: bool = False,
) -> dict:
    """Add/update/remove one supplier entry on an article ('Leverantörsinformation': supplier part number, purchase
    price in the supplier's currency, lead time, MOQ). Reads the full list first and writes it back with only this
    supplier changed. Fortnox answers a random 404 now and then; if the list can't be read at all, the write is
    refused unless article_has_no_supplier_entries=true (otherwise other suppliers would be wiped)."""
    async def go():
        try:
            cur = await fx.request("GET", f"/api/warehouse/itemsuppliers-v1/{enc(article_number)}", retry_404=6)
            entries = list(cur.get("suppliers") or [])
        except FortnoxError as e:
            if e.status == 404 and article_has_no_supplier_entries:
                entries = []
            else:
                raise ToolError("Could not read the article's current supplier entries (Fortnox 404s randomly). Retry, or "
                                "set article_has_no_supplier_entries=true if you know it has none.")
        entries = [x for x in entries if str(x.get("supplierNumber")) != str(supplier_number)] if remove else entries
        if not remove:
            mine = next((x for x in entries if str(x.get("supplierNumber")) == str(supplier_number)), None)
            if mine is None:
                mine = {"supplierNumber": str(supplier_number), "itemId": article_number}
                entries.append(mine)
            for k, v in {"price": price, "currencyCode": currency, "supplierItemId": supplier_article_number,
                         "supplierItemName": supplier_article_name, "deliveryTime": delivery_time_days,
                         "minimumQuantityToPurchase": minimum_quantity}.items():
                if v is not None:
                    mine[k] = v
        await fx.request("POST", "/api/warehouse/itemsuppliers-v1", json=[{"itemId": article_number, "suppliers": entries}])
        return await fx.request("GET", f"/api/warehouse/itemsuppliers-v1/{enc(article_number)}", retry_404=6)
    return await _write(dry_run, go)


@tool()
async def deactivate_article(article_number: str, replaced_by: str | None = None, clear_ean: bool = True, dry_run: bool = False) -> dict:
    """Retire an article (articles that were ever used can't be deleted): webshop off, inactive, EAN cleared so it
    can move to a new article, description prefixed 'UTGÅTT' (plus 'ersatt av X' with replaced_by). Move any stock
    first (manual outbound/inbound)."""
    async def go():
        a = (await fx.get(f"/3/articles/{enc(article_number)}"))["Article"]
        prefix = f"UTGÅTT (ersatt av {replaced_by})" if replaced_by else "UTGÅTT"
        desc = f"{prefix} - {a.get('Description') or ''}"
        fields: dict[str, Any] = {"WebshopArticle": False, "Active": False, "Description": fortnox_safe_title(desc)}
        if clear_ean:
            fields["EAN"] = None
        res = await fx.request("PUT", f"/3/articles/{enc(article_number)}", json={"Article": fields})
        out = {"article": res.get("Article", res)}
        if float(a.get("QuantityInStock") or 0):
            out["warning"] = f"Article still holds {a.get('QuantityInStock')} in stock — move it with a manual outbound/inbound."
        return out
    return await _write(dry_run, go)


# ====================================================================== stock — read

@tool()
async def get_stock(article_numbers: list[str] | None = None) -> dict:
    """Current stock per article on the server's stock point: inStock and availableStock (= in stock minus
    reservations, what a webshop should show). Articles missing from the answer have 0. Omit article_numbers for
    every non-zero balance."""
    async def go():
        sp = await _stock_point()
        return await fx.get("/api/warehouse/status-v1/stockbalance", stockPointCodes=sp["code"])
    rows = await _call(go())
    if article_numbers:
        want = set(article_numbers)
        found = {r["itemId"]: r for r in rows if r.get("itemId") in want}
        return {"stock": [found.get(n, {"itemId": n, "inStock": 0, "availableStock": 0, "note": "no balance row = 0"}) for n in article_numbers]}
    return {"count": len(rows), "stock": rows}


@tool()
async def stock_warnings(status: Literal["LOW_STOCK_BALANCE", "ORDER_SHORTAGE", "ALL"] = "LOW_STOCK_BALANCE") -> dict:
    """The 'Lågt saldo' stock warning list (Lager → Lagerlista): supplier, article, in stock, on order, reserved,
    available, order shortage, order point, stock warning."""
    async def go():
        data = await fx.request("GET", "/api/warehouse/reports-v2/stockitemlistcsv", raw=True, accept_json=False, params={
            "itemId": "", "includeSuppliers": "true", "stockBalanceStatus": status, "itemIdSearch": "",
            "itemDescriptionSearch": "", "csvExportLevel": "ITEM", "userLocale": "sv_SE"})
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=";,\t")
        rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
        return {"count": len(rows), "rows": rows}
    return await _call(go())


@tool()
async def article_movements(article_number: str) -> dict:
    """Stock history for one article, rebuilt from its documents (Fortnox has no movement log): goods receipts,
    manual in/outbound deliveries, stock corrections and purchase orders. Sales are on invoices (list_invoices)."""
    async def go():
        n = article_number
        out: dict[str, Any] = {}
        for key, path, params in [
            ("goods_receipts", "/api/warehouse/incominggoods-v1", {"itemId": n}),
            ("manual_deliveries", "/api/warehouse/deliveries-v1", {"type": "all", "itemId": n}),
            ("stock_corrections", "/api/warehouse/stocktaking-v1", {"itemId": n}),
            ("purchase_orders", "/api/warehouse/purchaseorders-v1", {"itemId": n, "state": "ALL"}),
        ]:
            try:
                out[key] = await fx.get(path, **params)
            except FortnoxError as e:
                out[key] = {"error": _brief(e)}
        return out
    return await _call(go())


# ====================================================================== stock — write

@tool()
async def start_stock_correction(article_number: str, counted_quantity: float, reason: str, responsible: str = "mcp",
                                 dry_run: bool = False) -> dict:
    """Step 1 of correcting a stock balance (= a stocktaking with one counted row). Creates and starts it and returns
    the current balance next to the counted one. Nothing changes until release_stock_correction. Use when the count
    is wrong; for a real physical event (scrap, found goods) use a manual delivery."""
    async def go():
        d = _today()
        name = fortnox_safe_title(f"Korrigering {article_number} ({reason})")[:100]
        st = await fx.request("POST", "/api/warehouse/stocktaking-v1", json={"date": d, "name": name, "responsible": responsible})
        dry = bool(st.get("dry_run"))
        sid = "<new stock correction id>" if dry else st.get("id")
        row = {"itemId": article_number, "stockTakenQuantity": counted_quantity, "countedBy": responsible}
        await fx.request("POST", f"/api/warehouse/stocktaking-v1/{sid}/rows", json=[row])
        await fx.request("PUT", f"/api/warehouse/stocktaking-v1/{sid}", json={
            "id": sid, "date": d, "name": name, "responsible": responsible, "state": "started",
            "rows": [{**row, "stockPointId": None, "stockLocationId": None}]})
        rows = None if dry else await fx.get(f"/api/warehouse/stocktaking-v1/{sid}/rows")
        return {"stock_correction_id": sid, "state": "started", "rows": rows,
                "next": "release_stock_correction to apply (bookkept, cannot be undone), or cancel_stock_correction"}
    return await _write(dry_run, go)


@tool()
async def release_stock_correction(stock_correction_id: int, dry_run: bool = False) -> dict:
    """Apply a started stock correction: stock is set to the counted quantity and bookkept. Completed corrections
    cannot be voided (a wrong count needs a new correction)."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/api/warehouse/stocktaking-v1/{stock_correction_id}/release"))


@tool()
async def cancel_stock_correction(stock_correction_id: int, dry_run: bool = False) -> dict:
    """Throw away a stock correction that has not been released (no effect on stock)."""
    return await _write(dry_run, lambda: fx.request("DELETE", f"/api/warehouse/stocktaking-v1/{stock_correction_id}"))


@tool()
async def list_stock_corrections(article_number: str | None = None, state: str | None = None) -> Any:
    """Stocktakings / stock corrections, optionally for one article."""
    return await _call(fx.get("/api/warehouse/stocktaking-v1", itemId=article_number, state=state))


@tool()
async def get_stock_correction(stock_correction_id: int) -> dict:
    """One stocktaking / stock correction with its rows (counted vs current balance)."""
    async def go():
        head = await fx.get(f"/api/warehouse/stocktaking-v1/{stock_correction_id}")
        rows = await fx.get(f"/api/warehouse/stocktaking-v1/{stock_correction_id}/rows")
        return {"stock_correction": head, "rows": rows}
    return await _call(go())


@tool()
async def create_manual_inbound(rows: list[dict], note: str, currency: str = "SEK", delivery_date: str | None = None,
                                dry_run: bool = False) -> dict:
    """Goods in WITHOUT a purchase order (found stock, sample, warranty return, SKU swap). rows: [{article_number,
    quantity, unit_cost (in currency, default 0), freight_cost, other_cost}]. Created as a draft; stock changes on
    release_manual_delivery. Real purchases belong on a purchase order + goods receipt."""
    async def go():
        rate, unit = await _currency_rate(currency)
        body = {"date": delivery_date or _today(), "currency": currency.upper(), "currencyRate": rate, "currencyUnit": unit,
                "note": note, "rows": [{"itemId": r["article_number"], "quantity": r["quantity"],
                                        "directCost": r.get("unit_cost", 0), "freightCost": r.get("freight_cost", 0),
                                        "otherCost": r.get("other_cost", 0)} for r in rows]}
        res = await fx.request("POST", "/api/warehouse/deliveries-v1/inbounddeliveries", json=body)
        return {"manual_inbound": res, "next": "release_manual_delivery(type='inbound', id) to add the stock"}
    return await _write(dry_run, go)


@tool()
async def create_manual_outbound(rows: list[dict], note: str, delivery_date: str | None = None, dry_run: bool = False) -> dict:
    """Goods out WITHOUT a sale (scrap, damage, loss, SKU swap). rows: [{article_number, quantity}]. Draft until
    release_manual_delivery."""
    body = {"date": delivery_date or _today(), "note": note,
            "rows": [{"itemId": r["article_number"], "quantity": r["quantity"]} for r in rows]}
    return await _write(dry_run, lambda: fx.request("POST", "/api/warehouse/deliveries-v1/outbounddeliveries", json=body))


@tool()
async def release_manual_delivery(type: Literal["inbound", "outbound"], delivery_id: int, dry_run: bool = False) -> dict:
    """Apply a manual inbound/outbound delivery: stock and value change and are bookkept."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/api/warehouse/deliveries-v1/{type}deliveries/{delivery_id}/release"))


@tool()
async def void_manual_delivery(type: Literal["inbound", "outbound"], delivery_id: int, dry_run: bool = False) -> dict:
    """Void a manual inbound/outbound delivery."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/api/warehouse/deliveries-v1/{type}deliveries/{delivery_id}/void"))


@tool()
async def list_manual_deliveries(type: Literal["all", "inbound", "outbound"] = "all", state: str | None = None,
                                 article_number: str | None = None) -> Any:
    """Manual inbound/outbound deliveries (and transfers)."""
    return await _call(fx.get("/api/warehouse/deliveries-v1", type=type, state=state, itemId=article_number))


@tool()
async def get_manual_delivery(type: Literal["inbound", "outbound"], delivery_id: int) -> Any:
    """One manual inbound/outbound delivery with rows."""
    return await _call(fx.get(f"/api/warehouse/deliveries-v1/{type}deliveries/{delivery_id}"))


# ====================================================================== customers + suppliers

def _norm(s: str | None) -> str:
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


@tool()
async def search_customers(
    name: str | None = None,
    email: str | None = None,
    organisation_number: str | None = None,
    vat_number: str | None = None,
    customer_number: str | None = None,
    include_inactive: bool = False,
) -> dict:
    """Find customers. vat_number / organisation_number scan ALL customers (registers often hold the same company
    under several customer numbers), so you find every existing account before creating a new one."""
    async def go():
        if vat_number or organisation_number:
            key = _norm(vat_number or organisation_number)
            allc = await fx.list_all("/3/customers", "Customers", filter=None if include_inactive else "active")
            hits = [c for c in allc if key and (key in _norm(c.get("VATNumber")) or key in _norm(c.get("OrganisationNumber"))
                                                or _norm(c.get("VATNumber")).endswith(key) or _norm(c.get("OrganisationNumber")) == key)]
            return {"customers": hits, "note": "several hits = duplicate accounts of the same company"}
        res = await fx.get("/3/customers", name=name, email=email, customernumber=customer_number,
                           filter=None if include_inactive else "active", limit=100)
        return {"customers": res.get("Customers", []), "paging": res.get("MetaInformation")}
    return await _call(go())


@tool()
async def get_customer(customer_number: str) -> dict:
    """One customer. Email is the customer's main e-mail; Fortnox also has EmailInvoice / EmailOffer / EmailOrder
    for where each document type is sent."""
    return await _call(_customer(customer_number))


@tool()
async def create_customer(
    name: str,
    country_code: str,
    email: str | None = None,
    vat_number: str | None = None,
    organisation_number: str | None = None,
    address1: str | None = None,
    address2: str | None = None,
    zip_code: str | None = None,
    city: str | None = None,
    phone: str | None = None,
    currency: str | None = None,
    customer_type: Literal["COMPANY", "PRIVATE"] | None = None,
    allow_duplicate: bool = False,
    extra_fields: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """Create a customer. Checks VAT / org number against every existing customer first and returns the existing
    account(s) instead, unless allow_duplicate=true. email = the customer's main e-mail address. EU companies:
    give the VAT number (drives reverse-charge VAT)."""
    async def go():
        key = vat_number or organisation_number
        if key and not allow_duplicate:
            found = (await search_customers(vat_number=vat_number, organisation_number=organisation_number))["customers"]
            if found:
                return {"created": False, "existing_customers": found,
                        "note": "Customer already exists — use one of these, or allow_duplicate=true"}
        c: dict[str, Any] = {"Name": name, "CountryCode": country_code.upper()}
        for k, v in {"Email": email, "VATNumber": vat_number, "OrganisationNumber": organisation_number, "Address1": address1,
                     "Address2": address2, "ZipCode": zip_code, "City": city, "Phone1": phone, "Currency": currency,
                     "Type": customer_type}.items():
            if v:
                c[k] = v
        c.update(extra_fields or {})
        res = await fx.request("POST", "/3/customers", json={"Customer": c})
        return {"created": True, "customer": res.get("Customer", res)}
    return await _write(dry_run, go)


@tool()
async def update_customer(customer_number: str, fields: dict, dry_run: bool = False) -> dict:
    """Change customer fields (Fortnox field names, e.g. {"Address1": "...", "VATNumber": "...", "Email": "..."}).
    Existing documents keep the e-mail address they were created with."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/3/customers/{enc(customer_number)}", json={"Customer": fields}))


@tool()
async def search_suppliers(name: str | None = None, organisation_number: str | None = None, email: str | None = None,
                           supplier_number: str | None = None) -> dict:
    """Find suppliers by name, organisation number, e-mail or supplier number."""
    res = await _call(fx.get("/3/suppliers", name=name, organisationnumber=organisation_number, email=email,
                             suppliernumber=supplier_number, limit=100))
    return {"suppliers": res.get("Suppliers", []), "paging": res.get("MetaInformation")}


@tool()
async def get_supplier(supplier_number: str) -> dict:
    """One supplier."""
    return await _call(fx.get(f"/3/suppliers/{enc(supplier_number)}"))


@tool()
async def create_supplier(name: str, country_code: str, email: str | None = None, currency: str | None = None,
                          vat_number: str | None = None, organisation_number: str | None = None, address1: str | None = None,
                          zip_code: str | None = None, city: str | None = None, extra_fields: dict | None = None,
                          allow_duplicate: bool = False, dry_run: bool = False) -> dict:
    """Create a supplier (checks name / org number for an existing one first)."""
    async def go():
        if not allow_duplicate:
            found = (await fx.get("/3/suppliers", name=name)).get("Suppliers", [])
            if organisation_number:
                found += (await fx.get("/3/suppliers", organisationnumber=organisation_number)).get("Suppliers", [])
            if found:
                return {"created": False, "existing_suppliers": found, "note": "use allow_duplicate=true to create anyway"}
        s: dict[str, Any] = {"Name": name, "CountryCode": country_code.upper()}
        for k, v in {"Email": email, "Currency": currency, "VATNumber": vat_number, "OrganisationNumber": organisation_number,
                     "Address1": address1, "ZipCode": zip_code, "City": city}.items():
            if v:
                s[k] = v
        s.update(extra_fields or {})
        res = await fx.request("POST", "/3/suppliers", json={"Supplier": s})
        return {"created": True, "supplier": res.get("Supplier", res)}
    return await _write(dry_run, go)


@tool()
async def update_supplier(supplier_number: str, fields: dict, dry_run: bool = False) -> dict:
    """Change supplier fields (Fortnox field names)."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/3/suppliers/{enc(supplier_number)}", json={"Supplier": fields}))


# ====================================================================== sales documents — read

def _doc_list_params(filter, customer_number, from_date, to_date, page, limit, **kw):
    # newest first
    return {"filter": filter, "customernumber": customer_number, "fromdate": from_date, "todate": to_date,
            "page": page, "limit": min(limit, 500), "sortby": "documentnumber", "sortorder": "descending", **kw}


@tool()
async def list_offers(filter: Literal["cancelled", "expired", "ordercreated", "ordernotcreated"] | None = None,
                      customer_number: str | None = None, from_date: str | None = None, to_date: str | None = None,
                      page: int = 1, limit: int = 100) -> dict:
    """List offers (quotes)."""
    res = await _call(fx.get("/3/offers", **_doc_list_params(filter, customer_number, from_date, to_date, page, limit)))
    return {"offers": res.get("Offers", []), "paging": res.get("MetaInformation")}


@tool()
async def get_offer(document_number: str) -> dict:
    """One offer with rows."""
    return await _call(fx.get(f"/3/offers/{enc(document_number)}"))


@tool()
async def list_orders(filter: Literal["cancelled", "expired", "invoicecreated", "invoicenotcreated"] | None = None,
                      customer_number: str | None = None, from_date: str | None = None, to_date: str | None = None,
                      not_completed: bool | None = None, page: int = 1, limit: int = 100) -> dict:
    """List orders."""
    extra = {"notcompleted": str(not_completed).lower()} if not_completed is not None else {}
    res = await _call(fx.get("/3/orders", **_doc_list_params(filter, customer_number, from_date, to_date, page, limit, **extra)))
    return {"orders": res.get("Orders", []), "paging": res.get("MetaInformation")}


@tool()
async def get_order(document_number: str) -> dict:
    """One order with rows and delivery/warehouse state."""
    return await _call(fx.get(f"/3/orders/{enc(document_number)}"))


@tool()
async def list_invoices(
    filter: Literal["cancelled", "fullypaid", "unpaid", "unpaidoverdue", "unbooked"] | None = None,
    customer_number: str | None = None,
    your_order_number: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    page: int = 1,
    limit: int = 100,
) -> dict:
    """List invoices. your_order_number = the customer's or webshop's order number (YourOrderNumber) — how the
    invoice for a webshop order is usually found."""
    res = await _call(fx.get("/3/invoices", **_doc_list_params(filter, customer_number, from_date, to_date, page, limit,
                                                                yourordernumber=your_order_number)))
    return {"invoices": res.get("Invoices", []), "paging": res.get("MetaInformation")}


@tool()
async def get_invoice(document_number: str) -> dict:
    """One invoice with rows, payment and booking state."""
    return await _call(fx.get(f"/3/invoices/{enc(document_number)}"))


@tool()
async def get_document_pdf(document_type: Literal["offer", "order", "invoice"], document_number: str) -> list:
    """The PDF of an offer, order or invoice (as Fortnox prints it)."""
    data = await _call(fx.pdf(f"/3/{document_type}s/{enc(document_number)}/preview"))
    name = f"{document_type}-{document_number}.pdf"
    text = f"{name} ({len(data)} bytes)"
    return [
        TextContent(type="text", text=text),
        EmbeddedResource(type="resource", resource=BlobResourceContents(
            uri=f"fortnox://{document_type}/{document_number}.pdf", mimeType="application/pdf",
            blob=base64.b64encode(data).decode())),
    ]


# ====================================================================== sales documents — write

async def _sales_header(customer_number: str, currency: str | None, language: str | None, our_reference: str | None,
                        your_reference: str | None, remarks: str | None, extra: dict | None) -> dict:
    cust = await _customer(customer_number)
    h: dict[str, Any] = {"CustomerNumber": customer_number,
                         "Language": language or _language(cust.get("CountryCode")),
                         "Currency": (currency or cust.get("Currency") or "SEK").upper(),
                         "PriceList": cust.get("PriceList") or PRICE_LIST,  # the customer's own list, else the server's
                         "VATIncluded": False}
    for k, v in {"OurReference": our_reference, "YourReference": your_reference, "Remarks": remarks}.items():
        if v:
            h[k] = v
    h.update(extra or {})
    return h


@tool()
async def create_offer(
    customer_number: str,
    rows: list[dict],
    currency: str | None = None,
    expire_date: str | None = None,
    language: Literal["SV", "EN"] | None = None,
    our_reference: str | None = None,
    your_reference: str | None = None,
    freight: float | None = None,
    remarks: str | None = None,
    extra_fields: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """Create an offer (quote). rows: [{article_number, quantity, price (per unit, ex VAT, in the offer currency),
    description?, discount?}] — use the final prices you were given, never a formula. Defaults: valid 1 month,
    Swedish for Swedish customers else English, the customer's price list (else the server's), customer's currency.
    Freight goes in `freight`; other Fortnox header fields (e.g. OfferDate) in extra_fields."""
    async def go():
        h = await _sales_header(customer_number, currency, language, our_reference, your_reference, remarks, extra_fields)
        h.setdefault("OfferDate", _today())
        h.setdefault("ExpireDate", expire_date or _plus_month(h["OfferDate"]))
        if expire_date:
            h["ExpireDate"] = expire_date
        h["OfferRows"] = _rows_sales(rows, delivered=False)
        if freight is not None:
            h["Freight"] = freight
        return await fx.request("POST", "/3/offers", json={"Offer": h})
    return await _write(dry_run, go)


@tool()
async def update_offer(document_number: str, fields: dict | None = None, rows: list[dict] | None = None, dry_run: bool = False) -> dict:
    """Change an offer: fields = Fortnox header fields (e.g. {"ExpireDate": "2026-11-30"}); rows replaces ALL rows."""
    body = dict(fields or {})
    if rows is not None:
        body["OfferRows"] = _rows_sales(rows, delivered=False)
    return await _write(dry_run, lambda: fx.request("PUT", f"/3/offers/{enc(document_number)}", json={"Offer": body}))


@tool()
async def cancel_offer(document_number: str, dry_run: bool = False) -> dict:
    """Cancel an offer (it stays in the list, marked cancelled, total zeroed)."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/3/offers/{enc(document_number)}/cancel"))


@tool()
async def offer_to_order(document_number: str, dry_run: bool = False) -> dict:
    """Confirm an offer: creates an order from it."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/3/offers/{enc(document_number)}/createorder"))


@tool()
async def send_offer_email(document_number: str, dry_run: bool = False) -> dict:
    """E-mail the offer PDF to the customer from Fortnox (customer-facing)."""
    return await _write(dry_run, lambda: fx.request("GET", f"/3/offers/{enc(document_number)}/email", is_write=True))


@tool()
async def create_order(
    customer_number: str,
    rows: list[dict],
    currency: str | None = None,
    language: Literal["SV", "EN"] | None = None,
    our_reference: str | None = None,
    your_reference: str | None = None,
    your_order_number: str | None = None,
    delivery_state: Literal["registration", "reservation", "delivery"] | None = None,
    freight: float | None = None,
    remarks: str | None = None,
    extra_fields: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """Create an order. rows: [{article_number, quantity, price, description?}]. delivery_state: registration (no stock
    effect), reservation (reserves stock), delivery (takes stock out)."""
    async def go():
        h = await _sales_header(customer_number, currency, language, our_reference, your_reference, remarks, extra_fields)
        h.setdefault("OrderDate", _today())
        h["OrderRows"] = [{**r, "OrderedQuantity": r.pop("Quantity")} if "Quantity" in r else r
                          for r in _rows_sales(rows, delivered=False)]
        if your_order_number:
            h["YourOrderNumber"] = your_order_number
        if delivery_state:
            h["DeliveryState"] = delivery_state
        if freight is not None:
            h["Freight"] = freight
        return await fx.request("POST", "/3/orders", json={"Order": h})
    return await _write(dry_run, go)


@tool()
async def update_order(document_number: str, fields: dict | None = None, rows: list[dict] | None = None, dry_run: bool = False) -> dict:
    """Change an order (Fortnox header fields; rows replaces ALL rows). DeliveryState changes move stock."""
    body = dict(fields or {})
    if rows is not None:
        body["OrderRows"] = [{**r, "OrderedQuantity": r.pop("Quantity")} if "Quantity" in r else r for r in _rows_sales(rows, delivered=False)]
    return await _write(dry_run, lambda: fx.request("PUT", f"/3/orders/{enc(document_number)}", json={"Order": body}))


@tool()
async def cancel_order(document_number: str, dry_run: bool = False) -> dict:
    """Cancel an order."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/3/orders/{enc(document_number)}/cancel"))


@tool()
async def order_to_invoice(document_number: str, dry_run: bool = False) -> dict:
    """Create the invoice from an order (gets the next invoice number in the books)."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/3/orders/{enc(document_number)}/createinvoice"))


@tool()
async def send_order_email(document_number: str, dry_run: bool = False) -> dict:
    """E-mail the order confirmation PDF to the customer from Fortnox (customer-facing)."""
    return await _write(dry_run, lambda: fx.request("GET", f"/3/orders/{enc(document_number)}/email", is_write=True))


@tool()
async def create_invoice(
    customer_number: str,
    rows: list[dict],
    currency: str | None = None,
    language: Literal["SV", "EN"] | None = None,
    our_reference: str | None = None,
    your_reference: str | None = None,
    your_order_number: str | None = None,
    terms_of_payment: str | None = None,
    freight: float | None = None,
    remarks: str | None = None,
    extra_fields: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """Create an invoice (unbooked). rows: [{article_number, quantity, price, description?}]. Invoice quantities are
    delivered quantities and take the goods out of stock."""
    async def go():
        h = await _sales_header(customer_number, currency, language, our_reference, your_reference, remarks, extra_fields)
        h.setdefault("InvoiceDate", _today())
        h["InvoiceRows"] = _rows_sales(rows, delivered=True)
        for k, v in {"YourOrderNumber": your_order_number, "TermsOfPayment": terms_of_payment, "Freight": freight}.items():
            if v is not None:
                h[k] = v
        return await fx.request("POST", "/3/invoices", json={"Invoice": h})
    return await _write(dry_run, go)


@tool()
async def update_invoice(document_number: str, fields: dict | None = None, rows: list[dict] | None = None, dry_run: bool = False) -> dict:
    """Change an UNBOOKED invoice (Fortnox header fields; rows replaces ALL rows — include RowId in `extra` to keep
    rows). Re-pointing to another customer does NOT update the e-mail address (EmailInformation.EmailAddressTo) —
    set it in fields too."""
    body = dict(fields or {})
    if rows is not None:
        body["InvoiceRows"] = _rows_sales(rows, delivered=True)
    return await _write(dry_run, lambda: fx.request("PUT", f"/3/invoices/{enc(document_number)}", json={"Invoice": body}))


@tool()
async def bookkeep_invoice(document_number: str, dry_run: bool = False) -> dict:
    """Book an invoice in the ledger (Fortnox only credits booked invoices). A booked invoice can no longer be
    edited."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/3/invoices/{enc(document_number)}/bookkeep"))


@tool()
async def cancel_invoice(document_number: str, dry_run: bool = False) -> dict:
    """Cancel (makulera) an invoice that was NEVER sent and is not booked. A sent invoice is in the customer's hands,
    so it is credited instead (credit_invoice); this tool refuses it."""
    async def go():
        inv = (await fx.get(f"/3/invoices/{enc(document_number)}"))["Invoice"]
        if inv.get("Sent") or inv.get("Booked"):
            raise ToolError("This invoice has been sent or booked. Sent invoices are never cancelled — use credit_invoice.")
        return await fx.request("PUT", f"/3/invoices/{enc(document_number)}/cancel")
    return await _write(dry_run, go)


@tool()
async def credit_invoice(document_number: str, bookkeep_first: bool = False, dry_run: bool = False) -> dict:
    """Create a FULL credit note for an invoice (refund/return). Fortnox only credits BOOKED invoices; set
    bookkeep_first=true to book it first. Booking a full credit note puts the
    goods back in stock — don't add a manual inbound as well. Partial refund: credit, then update_invoice on the
    credit note to trim its rows."""
    async def go():
        inv = (await fx.get(f"/3/invoices/{enc(document_number)}"))["Invoice"]
        out: dict[str, Any] = {}
        if not inv.get("Booked"):
            if not bookkeep_first:
                raise ToolError("Invoice is not booked; Fortnox refuses to credit it (code 2000776). Re-run with bookkeep_first=true.")
            await fx.request("PUT", f"/3/invoices/{enc(document_number)}/bookkeep")
            out["booked_original"] = True
        res = await fx.request("PUT", f"/3/invoices/{enc(document_number)}/credit")
        orig = res.get("Invoice", res)
        out["credit_note_number"] = orig.get("CreditInvoiceReference") if isinstance(orig, dict) else None
        out["original"] = orig
        return out
    return await _write(dry_run, go)


@tool()
async def send_invoice_email(document_number: str, dry_run: bool = False) -> dict:
    """E-mail the invoice from Fortnox to the address on the invoice (customer-facing)."""
    return await _write(dry_run, lambda: fx.request("GET", f"/3/invoices/{enc(document_number)}/email", is_write=True))


@tool()
async def mark_invoice_sent(document_number: str, dry_run: bool = False) -> dict:
    """Mark an invoice as sent without e-mailing it (external print), e.g. when the PDF is sent another way."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/3/invoices/{enc(document_number)}/externalprint"))


@tool()
async def set_invoice_warehouse_ready(document_number: str, dry_run: bool = False) -> dict:
    """Mark an invoice as delivered/done in the warehouse."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/3/invoices/{enc(document_number)}/warehouseready"))


@tool()
async def register_invoice_payment(document_number: str, amount: float, payment_date: str | None = None,
                                   mode_of_payment: str | None = None, amount_currency: float | None = None,
                                   bookkeep: bool = False, dry_run: bool = False) -> dict:
    """Register an incoming customer payment on an invoice (amount in SEK; amount_currency for foreign-currency
    invoices). bookkeep=true books the payment as well."""
    async def go():
        p: dict[str, Any] = {"InvoiceNumber": int(document_number), "Amount": amount, "PaymentDate": payment_date or _today()}
        if mode_of_payment:
            p["ModeOfPayment"] = mode_of_payment
        if amount_currency is not None:
            p["AmountCurrency"] = amount_currency
        res = await fx.request("POST", "/3/invoicepayments", json={"InvoicePayment": p})
        if bookkeep:
            num = "<new payment number>" if res.get("dry_run") else (res.get("InvoicePayment") or {}).get("Number")
            if num:
                await fx.request("PUT", f"/3/invoicepayments/{num}/bookkeep")
        return res
    return await _write(dry_run, go)


# ====================================================================== purchasing

@tool()
async def list_purchase_orders(state: Literal["CURRENT", "ALL", "NOT_SENT", "SENT", "SENT_NOT_REJECTED", "DELAYED", "RECEIVED", "VOIDED"] = "CURRENT",
                               supplier_number: str | None = None, article_number: str | None = None, search: str | None = None,
                               limit: int = 100, offset: int = 0) -> Any:
    """Purchase orders (rows not included — use get_purchase_order). DELAYED = past delivery date, not fully received."""
    return await _call(fx.get("/api/warehouse/purchaseorders-v1", state=state, supplierNumber=supplier_number,
                              itemId=article_number, q=search, limit=limit, offset=offset))


@tool()
async def get_purchase_order(purchase_order_id: int, include_matches: bool = True) -> dict:
    """One purchase order with rows (ordered / received / remaining) and its matched goods receipts / supplier invoices."""
    async def go():
        po = await fx.get(f"/api/warehouse/purchaseorders-v1/{purchase_order_id}")
        out = {"purchase_order": po}
        if include_matches:
            try:
                out["matches"] = await fx.get(f"/api/warehouse/purchaseorders-v1/{purchase_order_id}/matches")
            except FortnoxError as e:
                out["matches_error"] = _brief(e)
        return out
    return await _call(go())


@tool()
async def create_purchase_order(
    supplier_number: str,
    rows: list[dict],
    currency: str | None = None,
    delivery_date: str | None = None,
    payment_terms: str | None = None,
    internal_reference: str | None = None,
    our_reference: str | None = None,
    confirmation_email: str | None = None,
    message_to_supplier: str | None = None,
    language: str = "GB",
    dry_run: bool = False,
) -> dict:
    """Create a purchase order (not sent). rows: [{article_number, quantity, price (in the PO currency), description?}];
    every article must exist (create_article first). Currency defaults to the supplier's, the rate comes from Fortnox.
    internal_reference = your own reference (e.g. the supplier's order number). language = Fortnox language code
    for the PO document (GB = English). Delivery address = the stock point's address, else
    the company address from Fortnox."""
    async def go():
        sup = (await fx.get(f"/3/suppliers/{enc(supplier_number)}"))["Supplier"]
        cur = (currency or sup.get("Currency") or "SEK").upper()
        rate, unit = await _currency_rate(cur)
        terms = payment_terms or sup.get("TermsOfPayment")
        sp = await _stock_point()
        body: dict[str, Any] = {
            "supplierNumber": str(supplier_number), "orderDate": _today(), "currencyCode": cur, "currencyRate": rate,
            "currencyUnit": unit, "languageCode": language, "purchaseType": "WAREHOUSE",
            "stockPointCode": sp["code"], **(await _delivery_address()),
            "rows": [{"itemId": r["article_number"], "itemDescription": r.get("description"), "orderedQuantity": r["quantity"],
                      "remainingOrderedQuantity": r["quantity"], "price": r.get("price"), "currencyCode": cur,
                      "stockPointCode": sp["code"]} for r in rows]}
        for k, v in {"paymentTermsCode": terms, "deliveryDate": delivery_date, "internalReference": internal_reference, "ourReference": our_reference,
                     "confirmationEmail": confirmation_email, "messageToSupplier": message_to_supplier}.items():
            if v:
                body[k] = v
        res = await fx.request("POST", "/api/warehouse/purchaseorders-v1", json=body)
        return {"purchase_order": res, "next": "send_purchase_order to e-mail it, or set_purchase_order_response if ordered another way"}
    return await _write(dry_run, go)


@tool()
async def update_purchase_order(purchase_order_id: int, fields: dict, dry_run: bool = False) -> dict:
    """Change purchase-order head fields (partial update, e.g. {"deliveryDate": "2026-10-20", "internalReference": "..."})."""
    return await _write(dry_run, lambda: fx.request("PATCH", f"/api/warehouse/purchaseorders-v1/{purchase_order_id}/partial", json=fields))


@tool()
async def send_purchase_order(purchase_order_id: int, dry_run: bool = False) -> dict:
    """E-mail the purchase order to the supplier (supplier-facing)."""
    return await _write(dry_run, lambda: fx.request("POST", f"/api/warehouse/purchaseorders-v1/{purchase_order_id}/send"))


@tool()
async def set_purchase_order_response(purchase_order_id: int,
                                      response: Literal["ACCEPTED_WITH_REQ_DLV_DATE", "ACCEPTED_WITH_CHANGED_DLV_DATE", "REJECTED", "SENT", "NOT_SENT"],
                                      dry_run: bool = False) -> dict:
    """Record the supplier's answer (e.g. when the order was placed by e-mail, phone or a web shop rather than sent
    from Fortnox)."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/api/warehouse/purchaseorders-v1/{purchase_order_id}/response",
                                                     json={"responseState": response}))


@tool()
async def complete_purchase_order(purchase_order_id: int, dry_run: bool = False) -> dict:
    """Close a purchase order manually (nothing more will arrive)."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/api/warehouse/purchaseorders-v1/{purchase_order_id}/complete"))


@tool()
async def void_purchase_order(purchase_order_id: int, dry_run: bool = False) -> dict:
    """Void a purchase order."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/api/warehouse/purchaseorders-v1/{purchase_order_id}/void"))


@tool()
async def list_goods_receipts(released: bool | None = None, completed: bool | None = None, supplier_number: str | None = None,
                              article_number: str | None = None, delivery_note: str | None = None, search: str | None = None) -> Any:
    """Goods receipts (incoming goods). unmatchedValue = received value not yet matched to a supplier invoice."""
    p = {"released": released, "completed": completed}
    p = {k: str(v).lower() for k, v in p.items() if v is not None}
    return await _call(fx.get("/api/warehouse/incominggoods-v1", supplierNumber=supplier_number, itemId=article_number,
                              deliveryNote=delivery_note, q=search, **p))


@tool()
async def get_goods_receipt(goods_receipt_id: int) -> Any:
    """One goods receipt with rows."""
    return await _call(fx.get(f"/api/warehouse/incominggoods-v1/{goods_receipt_id}"))


@tool()
async def create_goods_receipt(purchase_order_id: int, received: list[dict], delivery_note: str | None = None,
                               receipt_date: str | None = None, rest_still_expected: bool = True, note: str | None = None,
                               dry_run: bool = False) -> dict:
    """Book a delivery against a purchase order (draft; stock goes up on release_goods_receipt).
    received: [{article_number, quantity}] — what physically arrived. rest_still_expected=false closes the remaining
    quantity (supplier short-shipped for good). delivery_note = the supplier's delivery-note/invoice number."""
    async def go():
        po = await fx.get(f"/api/warehouse/purchaseorders-v1/{purchase_order_id}")
        sp = await _stock_point()
        rows = []
        for item in received:
            matches = [r for r in po.get("rows", []) if r.get("itemId") == item["article_number"] and float(r.get("remainingOrderedQuantity") or 0) > 0]
            if not matches:
                raise ToolError(f"{item['article_number']} has nothing left to receive on purchase order {purchase_order_id}")
            r = matches[0]
            qty = float(item["quantity"])
            remaining = float(r.get("remainingOrderedQuantity") or 0)
            rows.append({"purchaseOrderId": purchase_order_id, "purchaseOrderRowId": r["id"], "itemId": r["itemId"],
                         "orderedQuantity": r.get("orderedQuantity"), "receivedQuantity": qty,
                         "remainingOrderedQuantity": max(remaining - qty, 0),
                         "backOrderQuantity": max(remaining - qty, 0) if rest_still_expected else 0,
                         "takenQuantity": 0, "invoicedQuantity": 0, "stockPointCode": sp["code"]})
        body = {"deliveryNoteId": delivery_note, "hasDeliveryNote": bool(delivery_note), "supplierNumber": po.get("supplierNumber"),
                "date": receipt_date or _today(), "note": note, "rows": rows}
        res = await fx.request("POST", "/api/warehouse/incominggoods-v1", json=body)
        return {"goods_receipt": res, "next": "release_goods_receipt to put it in stock"}
    return await _write(dry_run, go)


@tool()
async def release_goods_receipt(goods_receipt_id: int, dry_run: bool = False) -> dict:
    """Put a goods receipt in stock: stock and value are booked, the PO's received quantities update."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/api/warehouse/incominggoods-v1/{goods_receipt_id}/release"))


@tool()
async def complete_goods_receipt(goods_receipt_id: int, dry_run: bool = False) -> dict:
    """Finalise a goods receipt's bookkeeping. Do this only after the supplier invoice is matched — afterwards it
    can't be matched or voided."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/api/warehouse/incominggoods-v1/{goods_receipt_id}/completed"))


@tool()
async def void_goods_receipt(goods_receipt_id: int, dry_run: bool = False) -> dict:
    """Void a goods receipt (only while not completed and not matched to a supplier invoice)."""
    return await _write(dry_run, lambda: fx.request("PUT", f"/api/warehouse/incominggoods-v1/{goods_receipt_id}/void"))


@tool()
async def list_supplier_invoices(filter: Literal["cancelled", "fullypaid", "unpaid", "unpaidoverdue", "unbooked", "pendingpayment", "authorizepending"] | None = None,
                                 supplier_number: str | None = None, from_date: str | None = None, to_date: str | None = None,
                                 page: int = 1, limit: int = 100) -> dict:
    """Supplier invoices (bills we have to pay)."""
    res = await _call(fx.get("/3/supplierinvoices", filter=filter, suppliernumber=supplier_number, fromdate=from_date,
                             todate=to_date, page=page, limit=min(limit, 500)))
    return {"supplier_invoices": res.get("SupplierInvoices", []), "paging": res.get("MetaInformation")}


@tool()
async def get_supplier_invoice(given_number: str) -> dict:
    """One supplier invoice."""
    return await _call(fx.get(f"/3/supplierinvoices/{enc(given_number)}"))


# ====================================================================== misc

@tool()
async def get_currency_rate(currency: str) -> dict:
    """Fortnox's current exchange rate for a currency (SEK per `Unit` units)."""
    rate, unit = await _call(_currency_rate(currency))
    return {"currency": currency.upper(), "rate_sek": rate, "unit": unit}


@tool()
async def company_info() -> dict:
    """Company settings in Fortnox (name, org number, address)."""
    return await _call(fx.get("/3/companyinformation"))


@tool()
async def api_request(method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"], path: str, body: Any = None,
                      params: dict | None = None, dry_run: bool = False) -> Any:
    """Any Fortnox API call the other tools don't cover. path starts with /3/ (classic API: bodies wrapped in the
    resource key, e.g. {"Article": {...}}) or /api/warehouse/ (bare JSON). Non-GET calls change live data, and so
    do some GETs (e.g. /3/invoices/{n}/email sends the invoice). With dry_run=true nothing is sent, GETs included."""
    if dry_run:
        return await _write(True, lambda: fx.request(method, path, params=params, json=body, is_write=True))
    if method == "GET":
        return await _call(fx.request("GET", path, params=params))
    return await _write(False, lambda: fx.request(method, path, params=params, json=body))


@tool()
async def get_invoice_payment_evidence(document_number: str) -> Any:
    """Payments and the vouchers linked to ONE invoice: payment method, amounts, and the VAT / receivable / bank /
    revenue rows of each voucher. No ledger browsing beyond this invoice. Accounting entries are not bank receipts."""
    return await _call(invoice_evidence(fx, document_number))


# Tools that only read Fortnox. They are marked read-only so a client can run several of them side by side
# (Claude Code, for one, runs unmarked MCP tools strictly one at a time). Every write tool has a dry_run parameter
# and must never be listed here.
READ_ONLY_TOOLS = frozenset({
    "get_article", "search_articles", "get_price", "get_stock", "stock_warnings", "article_movements",
    "list_stock_corrections", "get_stock_correction", "list_manual_deliveries", "get_manual_delivery",
    "search_customers", "get_customer", "search_suppliers", "get_supplier",
    "list_offers", "get_offer", "list_orders", "get_order", "list_invoices", "get_invoice",
    "list_purchase_orders", "get_purchase_order", "list_goods_receipts", "get_goods_receipt",
    "list_supplier_invoices", "get_supplier_invoice", "get_currency_rate", "company_info",
    "get_invoice_payment_evidence", "get_document_pdf",
})
for _name in READ_ONLY_TOOLS:  # KeyError here = a listed tool does not exist: refuse to start
    mcp._tool_manager._tools[_name].annotations = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

# Optional role (FORTNOX_ROLE_FILE): removes tools outside the role, allowlists writable fields, scrubs results and
# errors (roles.py). Without a role file nothing is removed or hidden.
ROLE_SUMMARY = roles.apply_role(mcp, ROLE)



USAGE = """fortnox-mcp: MCP server for Fortnox (version {version})

Started by your MCP client; configured with environment variables:
  FORTNOX_CLIENT_ID, FORTNOX_CLIENT_SECRET, FORTNOX_TENANT_ID   required (Fortnox service account)
  FORTNOX_ROLE_FILE      optional role file that narrows what this server offers
  FORTNOX_PRICE_LIST     sales price list (default A)
  FORTNOX_STOCK_POINT    stock point code (default: the only active one)
  FORTNOX_ARTICLE_DEFAULTS  JSON fields every new article gets
  MCP_TRANSPORT          stdio (default) or http; HOST / PORT / MCP_ALLOWED_HOSTS for http
See the README for details."""


def main() -> None:
    from . import __version__
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(USAGE.format(version=__version__))
        return
    if any(a in ("-V", "--version") for a in sys.argv[1:]):
        print(__version__)
        return
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    # stdout belongs to the MCP protocol in stdio mode: status goes to stderr
    print(f"fortnox-mcp {__version__} role={ROLE_SUMMARY['role']} tools={ROLE_SUMMARY['tools']} transport={transport}",
          file=sys.stderr, flush=True)
    if ROLE and "api_request" in ROLE.tools:
        print("WARNING: this role includes api_request, which reaches any Fortnox endpoint and is not limited by the "
              "role's params rules", file=sys.stderr, flush=True)
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport in ("http", "streamable-http"):
        mcp.run(transport="streamable-http")
    else:
        raise SystemExit(f"MCP_TRANSPORT={transport!r}: use stdio or http")


if __name__ == "__main__":
    main()
