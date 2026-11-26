---
name: fortnox
description: Use when doing day-to-day work in Fortnox through the Fortnox MCP server — articles, prices, stock, stock corrections, purchase orders, goods receipt, customers, offers, orders, invoices, credit notes or payments. Gives the order of tool calls for the standard workflows and the rules that keep Fortnox consistent.
---

# Working in Fortnox

Fortnox is the register for articles (ArticleNumber = SKU), stock, purchasing and sales documents. Use the Fortnox
MCP tools; reach for `api_request` only when no named tool covers the job.

## Ground rules

1. **Preview first.** For any write you have not done before in this conversation, call the tool with
   `dry_run=true`, show the user what would be sent, and only then run it for real.
2. **Customer- and supplier-facing steps need the user's yes:** `send_offer_email`, `send_order_email`,
   `send_invoice_email`, `send_purchase_order`. Prepare the document, report it ready, wait.
3. **Stock never changes by editing an article.** It changes only through documents:
   goods receipt (purchases), invoices with delivered quantity (sales), stock correction (the count was wrong),
   manual inbound/outbound (a real event without a purchase or sale: found, scrapped, damaged).
4. **Nothing is deleted.** Offers and orders are cancelled, articles are deactivated, and an invoice that was sent
   is credited, never cancelled.
5. **Look before you create.** Search customers by VAT or organisation number and suppliers by name before creating
   one. Search articles by EAN and article number before creating an article.

## Workflows

**Receive a supplier delivery**
1. `list_purchase_orders(supplier_number=…)`, then `get_purchase_order` to see what is still expected.
   No purchase order yet? `create_purchase_order` first (every article must exist), then
   `set_purchase_order_response` if it was ordered outside Fortnox.
2. `create_goods_receipt(purchase_order_id, received=[{article_number, quantity}], delivery_note=…)` with what
   physically arrived. `rest_still_expected=false` if the supplier will not send the rest.
3. `release_goods_receipt` puts it in stock. `complete_goods_receipt` only after the supplier invoice is matched.

**Quote → order → invoice**
1. `search_customers` (create with `create_customer` only if none exists).
2. `create_offer(customer_number, rows=[{article_number, quantity, price}])`: use the prices you were given.
3. Customer accepts: `offer_to_order`. Then `order_to_invoice`.
4. Send (`send_invoice_email`, with the user's yes) or `mark_invoice_sent` if it went out another way.
5. Payment in: `register_invoice_payment`.

**Refund or return**
1. `credit_invoice(document_number)` makes a full credit note (`bookkeep_first=true` if the invoice is not booked).
   A booked full credit note puts the goods back in stock, so don't add a manual inbound as well.
2. Partial refund: credit, then `update_invoice` on the credit note to trim its rows.

**The stock figure is wrong**
1. `get_stock` / `article_movements` to see what Fortnox thinks and why.
2. `start_stock_correction(article_number, counted_quantity, reason)` shows the current balance next to the count.
3. `release_stock_correction` applies it (cannot be undone) or `cancel_stock_correction`.

**New article**
1. `search_articles(ean=…)` and `search_articles(article_number=…)` to make sure it doesn't exist.
2. `create_article(...)` with `sales_price` if it is known. The tool sets the price before the webshop flag.
3. `set_article_supplier` for the supplier part number, purchase price and lead time.

**Retire an article**
Move any stock out first (manual outbound, or manual inbound on the replacement), then `deactivate_article`.

## Reading answers

- `get_stock`: `availableStock` = in stock minus reservations. A missing row means 0.
- `stock_warnings`: Fortnox's low-stock list (in stock, on order, reserved, order point).
- Fortnox error codes are worth quoting to the user; the message is usually Swedish.
