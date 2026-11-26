import re

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from fortnox_mcp import roles
from conftest import call, call_mcp, example_role, load_server, tool_names, walk_keys

SALES, SOURCING = example_role("sales"), example_role("sourcing")

COST_KEYS = {"PurchasePrice", "StockValue", "DirectCost", "FreightCost", "OtherCost", "SupplierNumber", "SupplierName",
             "Cost", "ContributionPercent", "ContributionValue", "suppliers", "price", "unitPrice", "orderValue",
             "orderValueInSEK", "unmatchedValue", "supplierNumber", "supplierName", "goods_receipts", "purchase_orders"}
SELLING_KEYS = {"SalesPrice"}


# ------------------------------------------------------------------ tool surface (through FastMCP, not tool.fn)

def test_admin_unchanged():
    s = load_server("admin")
    assert len(tool_names(s)) == 78 and "api_request" in tool_names(s)
    assert not hasattr(s.mcp._tool_manager._tools["get_article"].fn, "__guarded_role__")


def test_bad_role_files_refuse_to_start(monkeypatch, tmp_path):
    import json
    good = {"name": "x", "tools": ["get_article"]}
    for bad in ({"tools": []}, {**good, "colour": "red"}, {**good, "params": {"set_price": {}}},
                {**good, "params": {"get_article": {"forbid": ["nope"]}}}, {"name": "x", "tools": ["no_such_tool"]},
                {**good, "params": {"get_article": {"writable": {"article_number": "Weight"}}}},
                {**good, "hide_csv_columns": "("}):
        f = tmp_path / "role.json"
        f.write_text(json.dumps(bad))
        monkeypatch.setenv("FORTNOX_ROLE_FILE", str(f))
        with pytest.raises(SystemExit):
            load_server_from_env()
    monkeypatch.setenv("FORTNOX_ROLE_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit):
        roles.load_role_from_env()


def load_server_from_env():
    import importlib, sys
    sys.modules.pop("fortnox_mcp.server", None)
    return importlib.import_module("fortnox_mcp.server")


def test_without_role_file_nothing_is_hidden():
    s = load_server("admin")
    assert s.ROLE is None and len(tool_names(s)) == 78


@pytest.mark.parametrize("role,expected,count", [("sourcing", SOURCING.tools, 31), ("sales", SALES.tools, 41)])
async def test_tools_list_is_exactly_the_allowlist_and_every_tool_is_guarded(role, expected, count):
    s = load_server(role)
    listed = {t.name for t in await s.mcp.list_tools()}
    assert listed == expected and len(listed) == count
    assert "api_request" not in listed
    for n, t in s.mcp._tool_manager._tools.items():
        assert getattr(t.fn, "__guarded_role__", None) == role, n
    with pytest.raises(ToolError, match="Unknown tool"):
        await s.mcp.call_tool("api_request", {"method": "GET", "path": "/3/articles"})


def test_roles_do_not_share_writes():
    reads = ("get_", "search_", "list_", "company", "stock_warnings", "article_movements")
    assert {t for t in SOURCING.tools if not t.startswith(reads)}.isdisjoint(
        {t for t in SALES.tools if not t.startswith(reads)})


# ------------------------------------------------------------------ mixed responses, real schema keys

async def test_get_article_sales_hides_every_cost_key_keeps_price_and_stock():
    s = load_server("sales")
    out = await call(s, "get_article", article_number="ABC-1001")
    assert walk_keys(out).isdisjoint(COST_KEYS), walk_keys(out) & COST_KEYS
    a = out["article"]
    assert a["SalesPrice"] == 450.0 and a["QuantityInStock"] == 10 and a["DisposableQuantity"] == 8
    assert "suppliers" not in out and "order_point" in out


async def test_get_article_sourcing_hides_selling_price_keeps_costs_supplier_stock():
    s = load_server("sourcing")
    out = await call(s, "get_article", article_number="ABC-1001")
    assert walk_keys(out).isdisjoint(SELLING_KEYS)
    a = out["article"]
    assert a["PurchasePrice"] == 210.5 and a["DirectCost"] == 210.5 and a["SupplierNumber"] == "2001"
    assert out["suppliers"]["suppliers"][0]["price"] == 210.5
    assert a["QuantityInStock"] == 10


async def test_search_articles_rows_scrubbed_per_role():
    for role, hidden in (("sales", COST_KEYS), ("sourcing", SELLING_KEYS)):
        s = load_server(role)
        out = await call(s, "search_articles", text="roller")
        assert walk_keys(out).isdisjoint(hidden), role
        assert out["articles"][0]["ArticleNumber"] == "ABC-1001"


async def test_mcp_wire_output_is_scrubbed_and_no_unscrubbed_structured_channel_exists():
    """What a client receives: text content built from the scrubbed result. These tools return plain dict/Any, so
    FastMCP emits no outputSchema and no structuredContent; the test pins that so a typed return added later
    (which would open a second output channel) fails here and gets reviewed."""
    for role in ("sourcing", "sales"):
        s = load_server(role)
        for n, t in s.mcp._tool_manager._tools.items():
            assert t.fn_metadata.output_schema is None, f"{n} now has an output schema: structuredContent needs scrubbing"
    s = load_server("sales")
    content, structured = await call_mcp(s, "get_article", article_number="ABC-1001")
    assert structured is None
    text = "".join(c.text for c in content if getattr(c, "type", "") == "text")
    for k in ("PurchasePrice", "DirectCost", "StockValue", "SupplierName", "suppliers", "210.5", "2105"):
        assert k not in text, k
    assert "SalesPrice" in text and "450.0" in text
    content, structured = await call_mcp(s, "get_invoice", document_number="1234")
    text = "".join(c.text for c in content if getattr(c, "type", "") == "text")
    assert "ContributionValue" not in text and "421.0" not in text and "479.0" not in text and '"Price": 450.0' in text
    s2 = load_server("sourcing")
    content, structured = await call_mcp(s2, "get_article", article_number="ABC-1001")
    text = "".join(c.text for c in content if getattr(c, "type", "") == "text")
    assert structured is None and "SalesPrice" not in text and "450.0" not in text and "210.5" in text


async def test_invoice_paths_hide_cost_and_margin_for_sales():
    s = load_server("sales")
    inv = await call(s, "get_invoice", document_number="1234")
    row = inv["Invoice"]["InvoiceRows"][0]
    assert row["Price"] == 450.0 and row["Total"] == 900.0
    assert walk_keys(inv).isdisjoint({"Cost", "ContributionPercent", "ContributionValue"})
    assert walk_keys(await call(s, "list_invoices")).isdisjoint({"ContributionPercent", "ContributionValue"})


async def test_stock_paths_do_not_leak_costs_or_suppliers_to_sales():
    s = load_server("sales")
    stock = await call(s, "get_stock", article_numbers=["ABC-1001"])
    assert stock["stock"][0]["availableStock"] == 8 and walk_keys(stock).isdisjoint(COST_KEYS)
    csv = await call(s, "stock_warnings")
    assert set(csv["rows"][0]) == {"Artikelnr", "Benämning", "I lager", "Väntas", "Reserverat", "Disponibelt",
                                   "Orderbrist", "Best. punkt", "Best. varning"}
    corr = await call(s, "get_stock_correction", stock_correction_id=5)
    assert corr["rows"][0]["stockTakenQuantity"] == 9 and walk_keys(corr).isdisjoint(COST_KEYS)
    assert set(await call(s, "article_movements", article_number="ABC-1001")) == {"stock_corrections"}


async def test_stock_warnings_sourcing_keeps_supplier_columns():
    s = load_server("sourcing")
    assert {"Leverantörsnr", "Leverantörsnamn", "I lager"} <= set((await call(s, "stock_warnings"))["rows"][0])


async def test_purchase_side_sourcing_only():
    s = load_server("sourcing")
    po = await call(s, "get_purchase_order", purchase_order_id=77)
    assert "price" in walk_keys(po)
    assert {"get_purchase_order", "list_goods_receipts", "get_supplier", "set_article_supplier",
            "list_supplier_invoices"}.isdisjoint(tool_names(load_server("sales")))


# ------------------------------------------------------------------ PDF: binary, cannot be scrubbed -> no staff role

async def test_pdf_invoice_only_for_sales_never_for_sourcing():
    """Customer invoice PDFs for the sales role. Offer/order previews are refused before any
    Fortnox call, the advertised schema only offers "invoice", sourcing has no PDF tool, admin is unchanged."""
    assert "get_document_pdf" in SALES.tools and "get_document_pdf" not in SOURCING.tools
    s = load_server("sourcing")
    assert "get_document_pdf" not in {t.name for t in await s.mcp.list_tools()}
    with pytest.raises(ToolError, match="Unknown tool"):
        await s.mcp.call_tool("get_document_pdf", {"document_type": "invoice", "document_number": "1234"})
    assert s.fx.sent == []

    s = load_server("sales")
    tool = s.mcp._tool_manager._tools["get_document_pdf"]
    assert tool.parameters["properties"]["document_type"]["enum"] == ["invoice"]
    for dt in ("offer", "order"):  # valid for admin, refused for sales by the role rule
        with pytest.raises(ToolError, match="not available for the sales role"):
            await s.mcp.call_tool("get_document_pdf", {"document_type": dt, "document_number": "170"})
    for dt in ("", None, "Invoice", ["invoice"]):  # never valid: pydantic refuses before the role rule
        with pytest.raises(ToolError):
            await s.mcp.call_tool("get_document_pdf", {"document_type": dt, "document_number": "170"})
    assert s.fx.pdf_paths == [] and s.fx.sent == []
    out = await s.mcp.call_tool("get_document_pdf", {"document_type": "invoice", "document_number": "1234"})
    blocks = out[0] if isinstance(out, tuple) else out
    kinds = {getattr(b, "type", None) for b in blocks}
    assert kinds == {"text", "resource"}
    res = next(b for b in blocks if b.type == "resource").resource
    assert res.mimeType == "application/pdf" and str(res.uri) == "fortnox://invoice/1234.pdf" and res.blob
    assert s.fx.pdf_paths == ["/3/invoices/1234/preview"] and s.fx.sent == []  # read-only, nothing written

    s = load_server("admin")
    assert "get_document_pdf" in tool_names(s)
    enum = s.mcp._tool_manager._tools["get_document_pdf"].parameters["properties"]["document_type"]["enum"]
    assert set(enum) == {"offer", "order", "invoice"}  # admin path unchanged: sales documents only


async def test_document_sending_unavailable_but_bookkeeping_and_payments_stay():
    """E-mailing a document from Fortnox attaches the print-template PDF (unverifiable from here), so the three
    send_* sales tools are admin-only. Non-document bookkeeping/payment tools remain for the sales role."""
    assert {"send_offer_email", "send_order_email", "send_invoice_email"}.isdisjoint(SALES.tools | SOURCING.tools)
    s = load_server("sales")
    names = {t.name for t in await s.mcp.list_tools()}
    assert names.isdisjoint({"send_offer_email", "send_order_email", "send_invoice_email"})
    assert {"bookkeep_invoice", "cancel_invoice", "credit_invoice", "register_invoice_payment", "mark_invoice_sent",
            "set_invoice_warehouse_ready", "offer_to_order", "order_to_invoice"} <= names
    for n, args in (("send_invoice_email", {"document_number": "1234"}), ("send_offer_email", {"document_number": "170"})):
        with pytest.raises(ToolError, match="Unknown tool"):
            await s.mcp.call_tool(n, args)
    ok = await call(s, "register_invoice_payment", document_number="1234", amount=1125.0, dry_run=True)
    assert ok["dry_run"] is True and s.fx.sent == []
    # sourcing keeps send_purchase_order: a cost-side document, no selling prices on it
    assert "send_purchase_order" in tool_names(load_server("sourcing"))


async def test_no_public_download_urls_in_results():
    """Fortnox answers carry '@url' API links (authenticated API, not downloadable without the service token).
    No tool returns a print/download URL; this pins that nothing like a PDF/preview link is passed through."""
    s = load_server("sales")
    import json as _json
    for n, kw in (("get_invoice", {"document_number": "1234"}), ("get_article", {"article_number": "ABC-1001"})):
        text = _json.dumps(await call(s, n, **kw))
        assert "preview" not in text and "print" not in text.lower() and ".pdf" not in text


# ------------------------------------------------------------------ parameter boundary (dry-run only, nothing sent)

async def test_sourcing_cannot_set_any_selling_price_including_zero():
    s = load_server("sourcing")
    for v in (100.0, 0, 0.0, -1):
        with pytest.raises(ToolError, match="sales_price"):
            await call(s, "create_article", article_number="X-1", description="Test", sales_price=v, dry_run=True)
    with pytest.raises(ToolError, match="webshop_article"):
        await call(s, "create_article", article_number="X-1", description="Test", webshop_article=True, dry_run=True)
    assert (await call(s, "create_article", article_number="X-1", description="Test", dry_run=True))["dry_run"] is True
    for bad in ({"SalesPrice": 1}, {"WebshopArticle": True}, {"QuantityInStock": 5}, {"Weight": {"nested": 1}}):
        with pytest.raises(ToolError):
            await call(s, "update_article", article_number="X-1", extra_fields=bad, dry_run=True)
    ok = await call(s, "update_article", article_number="X-1", purchase_price=12.0,
                    extra_fields={"Weight": 100, "DirectCost": 11}, dry_run=True)
    assert ok["dry_run"] is True and s.fx.sent == []


async def test_sourcing_rows_and_fields_are_allowlisted():
    s = load_server("sourcing")
    with pytest.raises(ToolError, match="not writable"):
        await call(s, "create_purchase_order", supplier_number="2001",
                   rows=[{"article_number": "X", "quantity": 1, "SalesPrice": 9}], dry_run=True)
    with pytest.raises(ToolError, match="not writable"):
        await call(s, "update_purchase_order", purchase_order_id=77, fields={"rows": []}, dry_run=True)
    with pytest.raises(ToolError, match="not writable"):
        await call(s, "create_goods_receipt", purchase_order_id=77,
                   received=[{"article_number": "ABC-1001", "quantity": 1, "price": 1}], dry_run=True)
    assert s.fx.sent == []


async def test_sales_rows_cannot_carry_cost_or_nested_surprises():
    s = load_server("sales")
    base = dict(customer_number="100", dry_run=True)
    for rows in ([{"article_number": "ABC-1001", "quantity": 1, "price": 450, "extra": {"Cost": 1}}],
                 [{"article_number": "ABC-1001", "quantity": 1, "extra": {"ContributionValue": 1}}],
                 [{"article_number": "ABC-1001", "quantity": 1, "PurchasePrice": 1}],
                 [{"article_number": "ABC-1001", "quantity": 1, "extra": {"Description": {"x": 1}}}]):
        with pytest.raises(ToolError):
            await call(s, "create_invoice", rows=rows, **base)
    for fields in ({"Rows": []}, {"InvoiceRows": []}, {"Bogus": 1}, {"EmailInformation": {"EmailAddressTo": "other@example.com"}},
                   {"PrintTemplate": "st"}):
        with pytest.raises(ToolError):
            await call(s, "update_invoice", document_number="1234", fields=fields, dry_run=True)
    with pytest.raises(ToolError, match="not writable"):
        await call(s, "update_customer", customer_number="100", fields={"Name": "A", "Bogus": 1}, dry_run=True)
    ok = await call(s, "create_invoice", rows=[{"article_number": "ABC-1001", "quantity": 2, "price": 450.0,
                                                 "extra": {"Discount": 5, "DiscountType": "PERCENT"}}],
                    extra_fields={"OurReference": "Anna"}, **base)
    assert ok["dry_run"] is True
    assert (await call(s, "set_price", article_number="ABC-1001", price=460.0, dry_run=True))["dry_run"] is True
    assert s.fx.sent == []
    assert {"update_article", "create_article", "set_article_supplier"}.isdisjoint(tool_names(s))


# ------------------------------------------------------------------ errors never echo upstream text

async def test_fortnox_error_becomes_generic_with_reference(caplog):
    from fortnox_mcp.client import FortnoxError
    s = load_server("sales")
    s.fx.fail_with = FortnoxError(400, 'Felaktigt värde: {"PurchasePrice": 210.5, "DirectCost": 99}', code=2000433)
    with pytest.raises(ToolError) as e:
        await call(s, "get_article", article_number="ABC-1001")
    msg = str(e.value)
    assert "HTTP 400" in msg and "2000433" in msg and "reference" in msg
    msg = re.sub(r"reference [0-9a-f]{8}", "reference <ref>", msg)  # the random reference may contain digits
    assert "210.5" not in msg and "99" not in msg and "Felaktigt" not in msg
    assert any("210.5" in r.getMessage() for r in caplog.records)


async def test_unexpected_exception_becomes_generic():
    s = load_server("sales")
    s.fx.fail_with = RuntimeError('body {"purchaseprice": 99, "DIRECTCOST": 7} unlabelled 123.45')
    with pytest.raises(ToolError) as e:
        await call(s, "get_article", article_number="ABC-1001")
    msg = re.sub(r"reference [0-9a-f]{8}", "reference <ref>", str(e.value))
    assert "99" not in msg and "123.45" not in msg and "unlabelled" not in msg and "reference" in msg


def test_scrub_text_is_case_insensitive_belt():
    assert "210" not in roles.scrub_text('{"purchasePRICE": 210.5}', SALES)
    assert "210" not in roles.scrub_text("DirectCost=210.5", SALES)
    assert "450" not in roles.scrub_text("salesprice: 450", SOURCING)


async def test_own_house_rule_messages_still_reach_staff():
    s = load_server("sales")
    with pytest.raises(ToolError, match="Price must be"):
        await call(s, "set_price", article_number="ABC-1001", price=0, dry_run=True)
