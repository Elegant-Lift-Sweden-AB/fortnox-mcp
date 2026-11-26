"""Tests run the real server module with a fake Fortnox client: no network, no credentials, no writes.

Payload shapes mirror live Fortnox answers (key names copied from real GETs; values invented)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # so tests can `from conftest import ...`
EXAMPLES = os.path.join(os.path.dirname(HERE), "examples", "roles")


def example_file(name):
    return os.path.join(EXAMPLES, f"{name}.json")


def example_role(name):
    from fortnox_mcp import roles
    with open(example_file(name), encoding="utf-8") as f:
        return roles.parse_role(json.load(f), name)

ARTICLE_FULL = {  # GET /3/articles/{n}: every key the live endpoint returned
    "@url": "x", "Active": True, "ArticleNumber": "ABC-1001", "Bulky": False, "CommodityCode": "84834000",
    "ConstructionAccount": None, "CostCalculationMethod": "WEIGHTED", "DefaultStockLocation": None,
    "DefaultStockPoint": "LAGER", "Depth": None, "Description": "Door roller 40 mm", "DirectCost": 210.5,
    "DisposableQuantity": 8, "EAN": "7340000000001", "EUAccount": 3108, "EUVATAccount": 3106, "Expired": False,
    "ExportAccount": 3105, "FreightCost": 12.0, "Height": None, "Housework": False, "HouseworkType": None,
    "Manufacturer": "Acme", "ManufacturerArticleNumber": "ABC-1001", "Note": "", "OtherCost": 3.0,
    "PurchaseAccount": 4000, "PurchasePrice": 210.5, "QuantityInStock": 10, "ReservedQuantity": 2,
    "SalesAccount": 3001, "SalesPrice": 450.0, "StockAccount": 1460, "StockChangeAccount": 4990, "StockGoods": True,
    "StockPlace": "A1", "StockValue": 2105.0, "StockWarning": 2, "SupplierName": "Example Supplier Oy",
    "SupplierNumber": "2001", "Type": "STOCK", "Unit": "st", "VAT": 25, "WebshopArticle": True, "Weight": 120,
    "Width": None,
}
ARTICLE_LIST_ROW = {k: ARTICLE_FULL[k] for k in (
    "@url", "ArticleNumber", "Description", "DisposableQuantity", "EAN", "Housework", "PurchasePrice",
    "QuantityInStock", "ReservedQuantity", "SalesPrice", "StockPlace", "StockValue", "Unit", "VAT", "WebshopArticle")}
SUPPLIERS = {"itemId": "ABC-1001", "suppliers": [{"supplierNumber": "2001", "price": 210.5, "currencyCode": "EUR",
                                                     "supplierItemId": "ABC-1001", "deliveryTime": 14}]}
ORDER_POINT = [{"stockPointId": "sp-0001", "orderPointQuantity": 2, "maxStock": 10}]
STOCK = [{"itemId": "ABC-1001", "inStock": 10, "availableStock": 8, "stockPointCode": "LAGER"}]
PRICE = {"Price": {"@url": "x", "ArticleNumber": "ABC-1001", "Date": None, "FromQuantity": 0, "Percent": None,
                   "Price": 450.0, "PriceList": "A"}}
INVOICE_ROW = {"AccountNumber": 3001, "ArticleNumber": "ABC-1001", "ContributionPercent": 53.2,
               "ContributionValue": 479.0, "Cost": 421.0, "CostCenter": "", "DeliveredQuantity": "2",
               "Description": "Door roller 40 mm", "Discount": 0, "DiscountType": "PERCENT", "HouseWork": False,
               "HouseWorkHoursToReport": None, "HouseWorkType": None, "Price": 450.0, "PriceExcludingVAT": 450.0,
               "Project": "", "RowId": 1, "StockPointCode": "LAGER", "Total": 900.0, "TotalExcludingVAT": 900.0,
               "Unit": "st", "VAT": 25, "VATCode": None}
INVOICE = {"Invoice": {"DocumentNumber": "1234", "CustomerNumber": "100", "CustomerName": "Example AB", "Booked": False,
                       "Sent": False, "Cancelled": False, "Total": 1125.0, "TotalToPay": 1125.0, "TotalVAT": 225.0,
                       "Net": 900.0, "Gross": 1125.0, "ContributionPercent": 53.2, "ContributionValue": 479.0,
                       "Balance": 1125.0, "CostCenter": "", "Currency": "SEK", "InvoiceRows": [INVOICE_ROW],
                       "EmailInformation": {"EmailAddressTo": "billing@example.com"}}}
STOCKTAKING = [{"id": 5, "date": "2026-10-01", "name": "Korrigering", "responsible": "mcp", "state": "started",
                "rows": [], "costCenterCode": None, "projectId": None, "usingStockPoints": True}]
STOCKTAKING_ROWS = [{"id": 1, "itemId": "ABC-1001", "itemDescription": "Door roller 40 mm", "stockTakenQuantity": 9,
                     "totalQuantityInStock": 10, "countedBy": "mcp", "stockPointCode": "LAGER"}]
PO = {"id": 77, "supplierNumber": "2001", "supplierName": "Example Supplier Oy", "supplier": {"id": 1}, "supplierEmail": "orders@example.com",
      "orderValue": 1052.5, "orderValueInSEK": 11800.0, "currencyCode": "EUR", "purchaseOrderState": "SENT",
      "rows": [{"id": 1, "itemId": "ABC-1001", "orderedQuantity": 5, "receivedQuantity": 0,
                "remainingOrderedQuantity": 5, "price": 210.5, "currencyCode": "EUR"}]}
GOODS_RECEIPTS = [{"id": 9, "completed": False, "released": True, "supplierName": "Example Supplier Oy",
                   "supplierNumber": "2001", "unmatchedValue": 1052.5, "voided": False}]
DELIVERIES = [{"deliveryId": 3, "type": "inbound", "released": True, "voided": False, "note": "found stock"}]
STOCK_POINTS = [{"id": "sp-0001", "code": "LAGER", "name": "Standardlager", "usingCompanyAddress": True,
                 "active": True, "deliveryAddress": None}]
COMPANY_INFO = {"CompanyInformation": {"CompanyName": "Example AB", "Address": "Storgatan 1", "ZipCode": "11122",
                                       "City": "Stockholm", "CountryCode": "SE", "DatabaseNumber": 1}}
SUPPLIER = {"Supplier": {"SupplierNumber": "2001", "Name": "Example Supplier Oy", "Currency": "EUR",
                         "TermsOfPayment": "30"}}
CURRENCY = {"Currency": {"Code": "EUR", "BuyRate": 11.25, "Unit": 1}}
CUSTOMER = {"Customer": {"CustomerNumber": "100", "Name": "Example AB", "Email": "billing@example.com", "CountryCode": "SE",
                         "Currency": "SEK", "PriceList": "B"}}
CSV = ('"Leverantörsnr","Leverantörsnamn","Artikelnr","Benämning","I lager","Väntas","Reserverat","Disponibelt",'
       '"Orderbrist","Best. punkt","Best. varning"\n'
       '"2001","Example Supplier Oy","ABC-1001","Door roller 40 mm","10","5","2","8","0","2","2"\n').encode("utf-8")


class FakeFortnox:
    """Answers GETs from the fixtures; honours the real dry-run context; records anything that would really be sent."""
    def __init__(self):
        self.sent = []
        self.pdf_paths = []
        self.fail_with = None

    async def get(self, path, **params):
        return await self.request("GET", path, params=params)

    async def pdf(self, path):
        assert path.startswith(("/3/offers/", "/3/orders/", "/3/invoices/")) and path.endswith("/preview"), path
        self.pdf_paths.append(path)
        return b"%PDF-1.4 fake sales document"

    async def request(self, method, path, params=None, json=None, raw=False, accept_json=True, retry_404=0, is_write=None):
        assert "None" not in path, f"request to a placeholder path: {method} {path}"
        if self.fail_with is not None:
            e, self.fail_with = self.fail_with, None
            raise e
        if method != "GET" or is_write:
            from fortnox_mcp import client
            rec = client._dry.get()
            if rec is not None:
                rec.append({"method": method, "path": path, "json": json})
                return {"dry_run": True}
            self.sent.append((method, path, json))
            return {"ok": True}
        last = path.rstrip("/").split("/")[-1]
        if "stockpoints-v1" in path:
            return STOCK_POINTS
        if path == "/3/companyinformation":
            return COMPANY_INFO
        if path.startswith("/3/suppliers/"):
            return SUPPLIER
        if path.startswith("/3/currencies/"):
            return CURRENCY
        if path.startswith("/3/articles/"):
            return {"Article": ARTICLE_FULL}
        if path == "/3/articles":
            return {"Articles": [ARTICLE_LIST_ROW], "MetaInformation": {"@TotalPages": 1}}
        if "itemsuppliers" in path:
            return SUPPLIERS
        if "itemstockpointdata" in path:
            return ORDER_POINT
        if "stockbalance" in path:
            return STOCK
        if "stockitemlistcsv" in path:
            return CSV
        if path.startswith("/3/prices/"):
            return PRICE
        if path.startswith("/3/invoices/"):
            return INVOICE
        if path == "/3/invoices":
            return {"Invoices": [INVOICE["Invoice"]], "MetaInformation": {}}
        if path.startswith("/3/customers/"):
            return CUSTOMER
        if "stocktaking-v1" in path and path.endswith("/rows"):
            return STOCKTAKING_ROWS
        if "stocktaking-v1" in path:
            return STOCKTAKING[0] if last.isdigit() else STOCKTAKING
        if "purchaseorders-v1" in path:
            return PO if last.isdigit() else [PO]
        if "incominggoods" in path:
            return GOODS_RECEIPTS
        if "deliveries-v1" in path:
            return DELIVERIES
        return {}


def load_server(role):
    """A fresh server module with the fake client swapped in. role: "admin" (no role file) or an example role name."""
    if role == "admin":
        os.environ.pop("FORTNOX_ROLE_FILE", None)
    else:
        os.environ["FORTNOX_ROLE_FILE"] = example_file(role)
    import importlib
    sys.modules.pop("fortnox_mcp.server", None)
    server = importlib.import_module("fortnox_mcp.server")  # re-executes the module: env is read again
    server.fx = FakeFortnox()
    return server


async def call(server, name, **kwargs):
    """Through the real FastMCP tool manager (argument validation + the registered, guarded fn); raw python result."""
    return await server.mcp._tool_manager.call_tool(name, kwargs, convert_result=False)


async def call_mcp(server, name, **kwargs):
    """Exactly what an MCP client receives: (content blocks, structuredContent)."""
    out = await server.mcp.call_tool(name, kwargs)
    return out if isinstance(out, tuple) else (out, None)


def tool_names(server):
    return set(server.mcp._tool_manager._tools)


def walk_keys(obj, acc=None):
    acc = set() if acc is None else acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(k); walk_keys(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            walk_keys(v, acc)
    return acc
