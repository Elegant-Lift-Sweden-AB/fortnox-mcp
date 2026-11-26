# Fortnox MCP

An MCP server for **day-to-day work in [Fortnox](https://www.fortnox.se)** from an AI assistant such as Claude,
ChatGPT or Cursor. It covers articles and prices, stock and stock corrections, purchase orders and goods receipt,
customers, suppliers, offers, orders, invoices, credit notes and payments: 78 tools in all.

## Quick start: let your AI agent install it

Paste this into Claude Code, Codex, Cursor or another agent that can run commands. It does the technical parts and
tells you the few steps only you can do in Fortnox.

```text
Install the Fortnox MCP server from https://github.com/Elegant-Lift-Sweden-AB/fortnox-mcp and connect it to my
Fortnox. Read the repository's README first and follow it. Answer me in the language I write in.

1. Make sure `uv` is installed (install it if not). Check that the server starts with
   `uvx --from git+https://github.com/Elegant-Lift-Sweden-AB/fortnox-mcp fortnox-mcp`, then stop it.
2. Walk me through creating the Fortnox integration (README section "1. Fortnox: create the integration"),
   one step at a time, and wait for me after each step: an app in the Fortnox Developer Portal with service
   account enabled, the scopes, and activation by our Fortnox system administrator.
3. Add the server to your own MCP configuration: stdio, command `uvx`, args
   `--from git+https://github.com/Elegant-Lift-Sweden-AB/fortnox-mcp fortnox-mcp`, with the environment
   variables FORTNOX_CLIENT_ID, FORTNOX_CLIENT_SECRET and FORTNOX_TENANT_ID. Put placeholders there and tell me
   exactly which file and line to open so I can paste the real values in myself. Do not ask me to paste the
   client secret into this chat, and never print it, commit it or copy it to any other file. Ask me whether we
   have more than one stock point or use another price list than A, and set FORTNOX_STOCK_POINT /
   FORTNOX_PRICE_LIST if so.
4. If you support skills, install skills/fortnox/SKILL.md from the repository as a skill.
5. After reloading the MCP server, test with read-only tools only: company_info, then search_articles and
   get_stock for one article. Do not create or change anything in Fortnox.
6. Show me the Swedish note about bookkeeping accounts ("Bokföringskonton") in the README and ask whether I want to set
   FORTNOX_ARTICLE_DEFAULTS now.

Do everything you can yourself. Tell me in short, plain steps only what I have to do myself.
```

## Why we built it

We built it at [Elegant Lift Sweden AB](https://lift-spares.se) (Lift-Spares) because we needed an AI assistant to do
the real work in our Fortnox, stock and purchasing included, and the servers we found only covered bookkeeping.
We use it every day and are sharing it as it is.

> **Status:** provided as-is under the MIT licence. We update it when we have time and don't offer support.
> Issues and pull requests are welcome, but answers may take a while or never come.
>
> **Know what you're getting:** the code was written with an AI coding assistant, and we haven't read it line by
> line ourselves. What we can vouch for is that we use it every day in our own Fortnox.

> [!IMPORTANT]
> **Bokföringskonton – läs detta innan du börjar**
>
> Servern väljer inga bokföringskonton själv. En ny artikel får de standardkonton och den moms som är inställda i
> ert Fortnox. Har ni aldrig ändrat dem är det Fortnox grundinställningar, och de passar inte alltid er verksamhet.
>
> - Gå igenom med er redovisningskonsult vilka konton era affärer ska bokföras på: försäljning i Sverige, till andra
>   EU-länder, export utanför EU, inköp och lager.
> - Ställ in dem i Fortnox, eller ange dem i `FORTNOX_ARTICLE_DEFAULTS` så att varje ny artikel får rätt konton, t.ex.
>   `{"VAT": 25, "SalesAccount": 3001, "EUAccount": 3108, "EUVATAccount": 3106, "ExportAccount": 3105, "PurchaseAccount": 4000}`.
>   Siffrorna är bara ett exempel ur BAS-kontoplanen, ingen rekommendation för just er.
> - Fakturor bokförs på artikelns konton och inbetalningar på betalsättets konto. Fel konto på en artikel ger alltså
>   fel bokföring på varje faktura där artikeln finns med.
> - Kontrollera kundens land, momsregistreringsnummer och momstyp i Fortnox. De avgör om fakturan får svensk moms,
>   omvänd skattskyldighet eller exportmoms.
> - Prova nya flöden med `dry_run=true` först. Då visar servern exakt vad den skulle skicka utan att ändra något.


## What is different about it

- **Warehouse work, not only bookkeeping.** Stock balances, the low-stock list, stock corrections (stocktaking),
  manual in/outbound deliveries, order points, purchase orders, goods receipt and supplier entries on articles,
  using Fortnox's warehouse API (`/api/warehouse`).
- **Every write tool has `dry_run`.** With `dry_run=true` it returns the exact request(s) it would send to Fortnox
  and changes nothing.
- **Guards for the mistakes Fortnox lets you make:**
  - Titles are cleaned of the characters Fortnox silently deletes ("(±1%)" would otherwise be stored as "(1%)").
  - EANs are checked (GS1 check digit, not already used on another article).
  - A webshop article can't be published without a price, so it never shows up at 0.
  - New customers are checked against every existing one by VAT/organisation number first.
  - Sent invoices are credited, never cancelled.
  - Supplier entries are read, changed and written back, so one change doesn't wipe the other suppliers.
- **Optional role files.** If you want staff to see less (for example no purchase prices for sales, no selling
  prices for purchasing), you write a role file and the server enforces it: it removes tools, refuses fields and
  hides keys from every answer. Without one, everything is available.
- **Parallel reads.** Read-only tools carry `readOnlyHint`, so clients can run several of them at once.
- **`api_request`** reaches any Fortnox endpoint the named tools don't cover.

## Requirements

- A Fortnox subscription. The stock and purchasing tools need Fortnox **Lager** (the warehouse module).
- A Fortnox integration (an app in the [Fortnox Developer Portal](https://www.fortnox.se/developer/developer-portal))
  that uses a **service account** with the client-credentials flow. There are no refresh tokens to keep alive.
- Python 3.10+ and [uv](https://docs.astral.sh/uv/) (or pip).

## 1. Fortnox: create the integration

1. In the Developer Portal, create an app and enable **service account** for it.
2. Give it the scopes for the parts you will use: `companyinformation` (required for service accounts),
   `article`, `price`, `customer`, `supplier`, `offer`, `order`, `invoice`, `payment`,
   `supplierinvoice`, `currency`, `print` (PDFs), `bookkeeping` (vouchers), `settings` (modes of payment)
   and `warehouse` (Lager). Check the names against Fortnox's current scope list.
3. A system administrator of your Fortnox company activates the integration with `account_type=service`.
4. Note the **client ID**, the **client secret** and your **tenant ID**. The tenant ID is your company's Fortnox
   database number. Fortnox sends it when the integration is activated (consent webhook) and returns it as
   `DatabaseNumber` from the company information endpoint; its client-credentials guide below shows both.

Fortnox's own guides:
[client credentials](https://www.fortnox.se/developer/authorization/get-access-token-using-client-credentials) ·
[service accounts](https://www.fortnox.se/developer/blog/service-accounts).

## 2. Add it to your AI client

**Claude Desktop, Cursor and other stdio clients** (in `claude_desktop_config.json` or the client's MCP settings):

```json
{
  "mcpServers": {
    "fortnox": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Elegant-Lift-Sweden-AB/fortnox-mcp", "fortnox-mcp"],
      "env": {
        "FORTNOX_CLIENT_ID": "...",
        "FORTNOX_CLIENT_SECRET": "...",
        "FORTNOX_TENANT_ID": "..."
      }
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add fortnox \
  -e FORTNOX_CLIENT_ID=... -e FORTNOX_CLIENT_SECRET=... -e FORTNOX_TENANT_ID=... \
  -- uvx --from git+https://github.com/Elegant-Lift-Sweden-AB/fortnox-mcp fortnox-mcp
```

**As an HTTP server (Docker):**

```bash
docker build -t fortnox-mcp .
# fortnox.env holds FORTNOX_CLIENT_ID=..., FORTNOX_CLIENT_SECRET=..., FORTNOX_TENANT_ID=... (one per line)
docker run --env-file fortnox.env -p 127.0.0.1:8000:8000 fortnox-mcp
# MCP endpoint: http://localhost:8000/mcp
```

HTTP mode has **no authentication of its own**. Anyone who can reach the port can use your Fortnox, so keep it on
localhost (as above) or put an authenticating proxy or MCP gateway in front of it. The server only accepts requests
addressed to `localhost`/`127.0.0.1` (this stops web pages from reaching it through DNS rebinding). Behind a proxy
under another name, list that name in `MCP_ALLOWED_HOSTS`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FORTNOX_CLIENT_ID`, `FORTNOX_CLIENT_SECRET`, `FORTNOX_TENANT_ID` | (required) | The service-account credentials. |
| `FORTNOX_ROLE_FILE` | (none) | Path to a role file that narrows what this server offers. See *Restricting access*. |
| `FORTNOX_PRICE_LIST` | `A` | The sales price list the tools read and write; offers, orders and invoices use the customer's own price list when it has one. |
| `FORTNOX_STOCK_POINT` | the only active one | Stock point code (e.g. `LAGER`). Needed only if you have several stock points. |
| `FORTNOX_ARTICLE_DEFAULTS` | (none) | JSON fields every new article gets on top of `Type: STOCK` and `StockGoods: true`, e.g. `{"VAT": 25, "SalesAccount": 3001}`. Without it Fortnox uses the company defaults. |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http`. |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | HTTP mode only. |
| `MCP_ALLOWED_HOSTS` | localhost, 127.0.0.1 | HTTP mode only: comma-separated host names the server answers to (`*` turns the check off). |

## Restricting access (optional)

By default the server offers every tool to whoever connects to it. Who may do what in your company is your
decision, so the server ships with no built-in roles.

If you want someone to have less, write a **role file** (JSON) and run a separate server for them with
`FORTNOX_ROLE_FILE=/path/to/role.json`. The server then enforces the file, not the AI:

- only the tools listed in `"tools"` exist for that server;
- `"params"` refuses arguments per tool: `"forbid"` (may not be given), `"only"` (allowed values) and
  `"writable"` (allowed keys in free-form fields and rows);
- keys in `"hide_fields"` are removed from every answer, and CSV columns matching `"hide_csv_columns"` are dropped;
- Fortnox error text is replaced by a reference number (the full text goes to the server log), so hidden values
  can't leak through an error message.

Two tools can't be narrowed by a role file, so leave them out of a restricted role unless you mean to give that
access: `api_request` reaches any Fortnox endpoint with any body, and `get_document_pdf` returns the PDF exactly as
your Fortnox print template prints it (a PDF can't have fields removed; `"only"` can limit which document types).

[`examples/roles/`](examples/roles) has two examples of the format: a sales file (no costs, margins or suppliers)
and a sourcing file (no selling prices or customers). Copy and change them, or write your own. The format is
described at the top of [`roles.py`](src/fortnox_mcp/roles.py). A file the server can't fully understand stops it
from starting.

## Tools

**Articles and prices**

| Tool | What it does |
|---|---|
| `get_article` | Everything about one article (SKU): register data, stock (in stock / available / reserved), sales price, order point + max stock, and supplier entries (supplier, supplier part number, purchase price). |
| `search_articles` | Search the article register. |
| `get_price` | Sales price of an article on a price list (default: the server's price list, FORTNOX_PRICE_LIST). |
| `create_article` | Create a stock article. |
| `update_article` | Change article fields. |
| `set_price` | Set the sales price (company currency, ex VAT) on a price list (default: the server's price list, FORTNOX_PRICE_LIST). |
| `set_webshop_article` | Tick/untick 'Webbshopsartikel'. |
| `set_order_point` | Set 'Beställningspunkt' (order point) and max stock on the server's stock point. |
| `set_article_supplier` | Add/update/remove one supplier entry on an article ('Leverantörsinformation': supplier part number, purchase price in the supplier's currency, lead time, MOQ). |
| `deactivate_article` | Retire an article (articles that were ever used can't be deleted): webshop off, inactive, EAN cleared so it can move to a new article, description prefixed 'UTGÅTT' (plus 'ersatt av X' with replaced_by). |

**Stock**

| Tool | What it does |
|---|---|
| `get_stock` | Current stock per article on the server's stock point: inStock and availableStock (= in stock minus reservations, what a webshop should show). |
| `stock_warnings` | The 'Lågt saldo' stock warning list (Lager → Lagerlista): supplier, article, in stock, on order, reserved, available, order shortage, order point, stock warning. |
| `article_movements` | Stock history for one article, rebuilt from its documents (Fortnox has no movement log): goods receipts, manual in/outbound deliveries, stock corrections and purchase orders. |
| `start_stock_correction` | Step 1 of correcting a stock balance (= a stocktaking with one counted row). |
| `release_stock_correction` | Apply a started stock correction: stock is set to the counted quantity and bookkept. |
| `cancel_stock_correction` | Throw away a stock correction that has not been released (no effect on stock). |
| `list_stock_corrections` | Stocktakings / stock corrections, optionally for one article. |
| `get_stock_correction` | One stocktaking / stock correction with its rows (counted vs current balance). |
| `create_manual_inbound` | Goods in WITHOUT a purchase order (found stock, sample, warranty return, SKU swap). |
| `create_manual_outbound` | Goods out WITHOUT a sale (scrap, damage, loss, SKU swap). |
| `release_manual_delivery` | Apply a manual inbound/outbound delivery: stock and value change and are bookkept. |
| `void_manual_delivery` | Void a manual inbound/outbound delivery. |
| `list_manual_deliveries` | Manual inbound/outbound deliveries (and transfers). |
| `get_manual_delivery` | One manual inbound/outbound delivery with rows. |

**Customers and suppliers**

| Tool | What it does |
|---|---|
| `search_customers` | Find customers. |
| `get_customer` | One customer. |
| `create_customer` | Create a customer. |
| `update_customer` | Change customer fields (Fortnox field names, e.g. {"Address1": "...", "VATNumber": "...", "Email": "..."}). |
| `search_suppliers` | Find suppliers by name, organisation number, e-mail or supplier number. |
| `get_supplier` | One supplier. |
| `create_supplier` | Create a supplier (checks name / org number for an existing one first). |
| `update_supplier` | Change supplier fields (Fortnox field names). |

**Offers, orders, invoices**

| Tool | What it does |
|---|---|
| `list_offers` | List offers (quotes). |
| `get_offer` | One offer with rows. |
| `create_offer` | Create an offer (quote). |
| `update_offer` | Change an offer: fields = Fortnox header fields (e.g. {"ExpireDate": "2026-11-30"}); rows replaces ALL rows. |
| `cancel_offer` | Cancel an offer (it stays in the list, marked cancelled, total zeroed). |
| `offer_to_order` | Confirm an offer: creates an order from it. |
| `send_offer_email` | E-mail the offer PDF to the customer from Fortnox (customer-facing). |
| `list_orders` | List orders. |
| `get_order` | One order with rows and delivery/warehouse state. |
| `create_order` | Create an order. |
| `update_order` | Change an order (Fortnox header fields; rows replaces ALL rows). |
| `cancel_order` | Cancel an order. |
| `order_to_invoice` | Create the invoice from an order (gets the next invoice number in the books). |
| `send_order_email` | E-mail the order confirmation PDF to the customer from Fortnox (customer-facing). |
| `list_invoices` | List invoices. |
| `get_invoice` | One invoice with rows, payment and booking state. |
| `get_document_pdf` | The PDF of an offer, order or invoice (as Fortnox prints it). |
| `create_invoice` | Create an invoice (unbooked). |
| `update_invoice` | Change an UNBOOKED invoice (Fortnox header fields; rows replaces ALL rows — include RowId in `extra` to keep rows). |
| `bookkeep_invoice` | Book an invoice in the ledger (Fortnox only credits booked invoices). |
| `cancel_invoice` | Cancel (makulera) an invoice that was NEVER sent and is not booked. |
| `credit_invoice` | Create a FULL credit note for an invoice (refund/return). |
| `send_invoice_email` | E-mail the invoice from Fortnox to the address on the invoice (customer-facing). |
| `mark_invoice_sent` | Mark an invoice as sent without e-mailing it (external print), e.g. when the PDF is sent another way. |
| `set_invoice_warehouse_ready` | Mark an invoice as delivered/done in the warehouse. |
| `register_invoice_payment` | Register an incoming customer payment on an invoice (amount in SEK; amount_currency for foreign-currency invoices). |
| `get_invoice_payment_evidence` | Payments and the vouchers linked to ONE invoice: payment method, amounts, and the VAT / receivable / bank / revenue rows of each voucher. |

**Purchasing and goods receipt**

| Tool | What it does |
|---|---|
| `list_purchase_orders` | Purchase orders (rows not included — use get_purchase_order). |
| `get_purchase_order` | One purchase order with rows (ordered / received / remaining) and its matched goods receipts / supplier invoices. |
| `create_purchase_order` | Create a purchase order (not sent). |
| `update_purchase_order` | Change purchase-order head fields (partial update, e.g. {"deliveryDate": "2026-10-20", "internalReference": "..."}). |
| `send_purchase_order` | E-mail the purchase order to the supplier (supplier-facing). |
| `set_purchase_order_response` | Record the supplier's answer (e.g. when the order was placed by e-mail, phone or a web shop rather than sent from Fortnox). |
| `complete_purchase_order` | Close a purchase order manually (nothing more will arrive). |
| `void_purchase_order` | Void a purchase order. |
| `list_goods_receipts` | Goods receipts (incoming goods). |
| `get_goods_receipt` | One goods receipt with rows. |
| `create_goods_receipt` | Book a delivery against a purchase order (draft; stock goes up on release_goods_receipt). |
| `release_goods_receipt` | Put a goods receipt in stock: stock and value are booked, the PO's received quantities update. |
| `complete_goods_receipt` | Finalise a goods receipt's bookkeeping. |
| `void_goods_receipt` | Void a goods receipt (only while not completed and not matched to a supplier invoice). |
| `list_supplier_invoices` | Supplier invoices (bills we have to pay). |
| `get_supplier_invoice` | One supplier invoice. |

**Other**

| Tool | What it does |
|---|---|
| `get_currency_rate` | Fortnox's current exchange rate for a currency (SEK per `Unit` units). |
| `company_info` | Company settings in Fortnox (name, org number, address). |
| `api_request` | Any Fortnox API call the other tools don't cover. |

## Using it well

[`skills/fortnox/SKILL.md`](skills/fortnox/SKILL.md) is a short skill for Claude (or any agent) with the standard
Fortnox workflows: receiving goods, quote to invoice, refunds, stock corrections. Add it to your agent so it uses the
tools in the right order.

Writes go straight into your live Fortnox. Try new workflows with `dry_run=true` first, or in a Fortnox test company.

## Development

```bash
uv venv && uv pip install -e '.[test]'
.venv/bin/pytest
```

The tests run the real server against a fake Fortnox client: no network, no credentials.

## Licence

MIT, see [LICENSE](LICENSE). Not affiliated with or endorsed by Fortnox AB.
