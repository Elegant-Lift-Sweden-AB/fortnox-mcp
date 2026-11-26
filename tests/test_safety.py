"""Previews never send anything, guarded fields can't be bypassed, writes are not retried blindly, HTTP mode checks Host."""
import asyncio

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from conftest import call, load_server


# ------------------------------------------------------------------ dry_run sends nothing and previews every write

async def test_stock_correction_preview_sends_nothing_and_shows_all_steps():
    s = load_server("admin")
    out = await call(s, "start_stock_correction", article_number="ABC-1001", counted_quantity=9, reason="count",
                     dry_run=True)
    assert s.fx.sent == [] and out["dry_run"] is True
    steps = [(r["method"], r["path"]) for r in out["would_send"]]
    assert steps[0] == ("POST", "/api/warehouse/stocktaking-v1")
    assert all("<new stock correction id>" in p for _, p in steps[1:]) and len(steps) == 3


async def test_api_request_dry_run_does_not_send_a_get():
    s = load_server("admin")
    out = await call(s, "api_request", method="GET", path="/3/invoices/1234/email", dry_run=True)
    assert out["dry_run"] is True and out["would_send"][0]["path"] == "/3/invoices/1234/email"
    assert s.fx.sent == []


async def test_payment_preview_includes_the_bookkeeping_step():
    s = load_server("admin")
    out = await call(s, "register_invoice_payment", document_number="1234", amount=100.0, bookkeep=True, dry_run=True)
    assert [r["method"] for r in out["would_send"]] == ["POST", "PUT"]
    assert out["would_send"][1]["path"] == "/3/invoicepayments/<new payment number>/bookkeep"


# ------------------------------------------------------------------ checked fields can't be smuggled in

@pytest.mark.parametrize("extra", [{"WebshopArticle": True}, {"EAN": "123"}, {"Description": "x"},
                                   {"ArticleNumber": "OTHER"}, {"QuantityInStock": 5}])
async def test_guarded_article_fields_refused_in_extra_fields(extra):
    s = load_server("admin")
    with pytest.raises(ToolError):
        await call(s, "create_article", article_number="NEW-1", description="Roller", extra_fields=extra, dry_run=True)
    with pytest.raises(ToolError):
        await call(s, "update_article", article_number="ABC-1001", extra_fields=extra, dry_run=True)
    assert s.fx.sent == []


async def test_extra_fields_still_work_for_other_fields():
    s = load_server("admin")
    out = await call(s, "create_article", article_number="NEW-1", description="Roller",
                     extra_fields={"Weight": 120, "Note": "n"}, dry_run=True)
    art = out["would_send"][0]["json"]["Article"]
    assert art["Weight"] == 120 and art["WebshopArticle"] is False and art["Description"] == "Roller"


# ------------------------------------------------------------------ sales documents follow the caller and the customer

async def test_invoice_uses_customer_price_list_and_caller_header_fields():
    s = load_server("admin")
    rows = [{"article_number": "ABC-1001", "quantity": 1, "price": 450.0}]
    out = await call(s, "create_invoice", customer_number="100", rows=rows, dry_run=True)
    inv = out["would_send"][0]["json"]["Invoice"]
    assert inv["PriceList"] == "B"  # the customer's own list (fixture), not the server default A
    out = await call(s, "create_invoice", customer_number="100", rows=rows, dry_run=True,
                     extra_fields={"InvoiceDate": "2026-01-15", "PriceList": "C"})
    inv = out["would_send"][0]["json"]["Invoice"]
    assert inv["InvoiceDate"] == "2026-01-15" and inv["PriceList"] == "C"


async def test_deactivate_keeps_the_old_description():
    s = load_server("admin")
    out = await call(s, "deactivate_article", article_number="ABC-1001", replaced_by="ABC-2002", dry_run=True)
    desc = out["would_send"][0]["json"]["Article"]["Description"]
    assert desc.startswith("UTGÅTT (ersatt av ABC-2002) - ") and "Door roller" in desc


async def test_sales_example_role_cannot_rank_by_stock_value_or_filter_by_supplier():
    s = load_server("sales")
    with pytest.raises(ToolError):
        await call(s, "search_articles", sort_by="stockvalue")
    with pytest.raises(ToolError):
        await call(s, "search_articles", supplier_number="2001")
    assert (await call(s, "search_articles", text="roller"))["articles"]


# ------------------------------------------------------------------ network errors: no blind retry of writes

def _client(monkeypatch, exc_type):
    from fortnox_mcp import client
    calls = []

    def handler(request):
        calls.append(request.method)
        raise exc_type("boom", request=request)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(client.asyncio, "sleep", no_sleep)
    fx = client.Fortnox()
    fx._token, fx._token_exp = "t", 9e18
    fx._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return fx, calls, client.FortnoxError


@pytest.mark.parametrize("exc_type,method,attempts", [
    (httpx.ReadTimeout, "POST", 1),       # may have been created: never resent
    (httpx.ConnectError, "POST", 3),      # never reached Fortnox: safe to retry
    (httpx.ReadTimeout, "GET", 3),        # reads are safe to retry
])
async def test_retry_policy(monkeypatch, exc_type, method, attempts):
    fx, calls, FortnoxError = _client(monkeypatch, exc_type)
    with pytest.raises(FortnoxError):
        await fx.request(method, "/3/invoices", json={"Invoice": {}} if method == "POST" else None)
    assert calls == [method] * attempts


# ------------------------------------------------------------------ HTTP mode

def test_http_mode_only_accepts_listed_hosts(monkeypatch):
    s = load_server("admin")
    ts = s.mcp.settings.transport_security
    assert ts.enable_dns_rebinding_protection and "localhost:8000" in ts.allowed_hosts
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "fortnox.example.com")
    ts = load_server("admin").mcp.settings.transport_security
    assert ts.allowed_hosts == ["fortnox.example.com"] and "https://fortnox.example.com" in ts.allowed_origins
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "*")
    assert not load_server("admin").mcp.settings.transport_security.enable_dns_rebinding_protection


def test_help_does_not_start_the_server(monkeypatch, capsys):
    s = load_server("admin")
    monkeypatch.setattr("sys.argv", ["fortnox-mcp", "--help"])
    s.main()
    assert "FORTNOX_CLIENT_ID" in capsys.readouterr().out
