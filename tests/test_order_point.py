"""set_order_point must send whole numbers as JSON ints: Fortnox answers HTTP 500 for -1.0 / 0.0 ."""
import json

from conftest import call, load_server


async def test_set_order_point_sends_whole_numbers_as_ints():
    s = load_server("admin")
    await call(s, "set_order_point", article_number="ABC-2002", order_point=-1.0, max_stock=0.0)
    method, path, body = s.fx.sent[-1]
    assert method == "POST" and path == "/api/warehouse/itemstockpointdata-v1/ABC-2002"
    row = body[0]
    assert row["stockPointId"] == "sp-0001"  # resolved from the only active stock point
    assert row["orderPointQuantity"] == -1 and type(row["orderPointQuantity"]) is int
    assert row["maxStock"] == 0 and type(row["maxStock"]) is int
    assert '"orderPointQuantity": -1,' in json.dumps(row) and '"maxStock": 0}' in json.dumps(row)


async def test_set_order_point_dry_run_and_no_max_stock():
    s = load_server("admin")
    out = await call(s, "set_order_point", article_number="ABC-1001", order_point=5, dry_run=True)
    assert s.fx.sent == []
    assert "maxStock" not in json.dumps(out) and '"orderPointQuantity": 5' in json.dumps(out)


async def test_purchase_order_uses_stock_point_and_company_address():
    s = load_server("admin")
    out = await call(s, "create_purchase_order", supplier_number="2001",
                     rows=[{"article_number": "ABC-1001", "quantity": 3, "price": 9.5}], dry_run=True)
    assert s.fx.sent == []
    body = out["would_send"][0]["json"]
    assert body["stockPointCode"] == "LAGER" and body["rows"][0]["stockPointCode"] == "LAGER"
    assert body["deliveryName"] == "Example AB" and body["deliveryCity"] == "Stockholm"
    assert body["currencyCode"] == "EUR" and body["currencyRate"] == 11.25 and body["paymentTermsCode"] == "30"


async def test_several_stock_points_need_configuration(monkeypatch):
    import conftest
    monkeypatch.setattr(conftest, "STOCK_POINTS", conftest.STOCK_POINTS + [{"id": "x", "code": "B", "active": True}])
    s = load_server("admin")
    import pytest
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="FORTNOX_STOCK_POINT"):
        await call(s, "get_stock")
    monkeypatch.setattr(s, "STOCK_POINT_CODE", "B")
    s._stock_point_cache = None
    out = await call(s, "set_order_point", article_number="ABC-1001", order_point=2, dry_run=True)
    assert out["would_send"][0]["json"][0]["stockPointId"] == "x"


def test_price_list_and_article_defaults_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("FORTNOX_PRICE_LIST", "B")
    monkeypatch.setenv("FORTNOX_ARTICLE_DEFAULTS", '{"VAT": 25, "SalesAccount": 3001}')
    s = load_server("admin")
    assert s.PRICE_LIST == "B"
    assert s.ARTICLE_DEFAULTS == {"Type": "STOCK", "StockGoods": True, "VAT": 25, "SalesAccount": 3001}
