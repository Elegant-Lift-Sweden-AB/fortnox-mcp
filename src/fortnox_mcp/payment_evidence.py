"""Invoice-scoped payment evidence: the payments and linked vouchers of ONE invoice, read-only.

No generic ledger browsing and no payment execution. Voucher rows are limited to the accounts that explain a payment
in the Swedish BAS chart (VAT 26xx, receivables 151x, bank/cash 19xx, revenue 3xxx, currency differences 7960); stock
and cost postings are left out and counted in `omitted_rows`.
"""
import re
from urllib.parse import quote


def pick(obj, fields):
    return {k: obj[k] for k in fields.split() if k in obj}


def permitted_voucher_row(row):
    # VAT, receivables, bank, revenue/rounding and FX effects; never stock cost postings.
    account=str(row.get('Account',''))
    return bool(re.fullmatch(r'(?:26[0-9]{2}|151[0-9]|19[0-9]{2}|3[0-9]{3}|7960)',account))


def invoice_number(value):
    if not re.fullmatch(r'[0-9]{1,12}', value):
        raise ValueError('A numeric Fortnox invoice number is required')
    return value


async def invoice_evidence(fx, document_number):
    number = invoice_number(document_number)
    inv = (await fx.get(f'/3/invoices/{number}'))['Invoice']
    payments=[]
    complete=True
    for page in range(1, 11):
        listing=await fx.get('/3/invoicepayments', invoicenumber=number, page=page, limit=100)
        for row in listing.get('InvoicePayments', []):
            pid=invoice_number(str(row['Number']))
            p=(await fx.get(f'/3/invoicepayments/{pid}'))['InvoicePayment']
            if str(p.get('InvoiceNumber')) != number:
                raise ValueError('Payment does not belong to requested invoice')
            method={}
            if p.get('ModeOfPayment'):
                raw=await fx.get('/3/modesofpayments/'+quote(str(p['ModeOfPayment']),safe=''))
                method=pick(raw.get('ModeOfPayment',{}),'Code Description AccountNumber')
            projected=pick(p,'Number InvoiceNumber Amount AmountCurrency Currency PaymentDate Booked ModeOfPayment Account VoucherSeries VoucherNumber VoucherYear')
            projected['payment_method']=method
            payments.append(projected)
        if page >= int(listing.get('MetaInformation',{}).get('@TotalPages',1)):
            break
    else:
        complete=False
    vouchers=[]
    seen=set()
    for kind, source in [('invoice',inv)]+[('payment',p) for p in payments]:
        series, num, year=(source.get(k) for k in ('VoucherSeries','VoucherNumber','VoucherYear'))
        if not all((series,num,year)):continue
        key=(str(series),str(num),str(year))
        if key in seen:continue
        seen.add(key)
        if not re.fullmatch('[A-Za-z0-9]+',str(series)) or not str(num).isdigit() or not str(year).isdigit():
            raise ValueError('Invalid linked voucher reference')
        raw=(await fx.get(f'/3/vouchers/{series}/{num}',financialyear=year))['Voucher']
        v=pick(raw,'VoucherSeries VoucherNumber Year TransactionDate')
        v.update({'source':kind,'linked_reference':{'series':series,'number':num,'financial_year':year},
                  'rows':[pick(r,'Account Debit Credit Removed') for r in raw.get('VoucherRows',[]) if permitted_voucher_row(r)],
                  'omitted_rows':sum(not permitted_voucher_row(r) for r in raw.get('VoucherRows',[]))})
        vouchers.append(v)
    return {'invoice':pick(inv,'DocumentNumber CustomerNumber CustomerName YourOrderNumber InvoiceDate Currency Total TotalVAT Balance Booked Cancelled CreditInvoiceReference Credit'),
            'payments':payments,'vouchers':vouchers,'payments_complete':complete,
            'authority':'Fortnox is the accounting master. Payment registrations are accounting entries, not proof of cash movement.'}
