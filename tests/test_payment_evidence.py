import pytest
from conftest import load_server, tool_names
from fortnox_mcp.payment_evidence import invoice_evidence

class FX:
    def __init__(self): self.paths=[]
    async def get(self,path,**params):
        self.paths.append((path,params))
        if path == '/3/invoices/5001': return {'Invoice': {'DocumentNumber':'5001','CustomerName':'Example Customer Srl','Currency':'EUR','YourOrderNumber':'WEB-1001','VoucherSeries':'B','VoucherNumber':101,'VoucherYear':1,'TotalVAT':9.10,'Secret':'no'}}
        if path == '/3/invoicepayments': return {'InvoicePayments':[{'Number':7001}], 'MetaInformation':{'@TotalPages':1}}
        if path == '/3/invoicepayments/7001': return {'InvoicePayment':{'Number':7001,'InvoiceNumber':5001,'ModeOfPayment':'BANK','AmountCurrency':50.40,'VoucherSeries':'C','VoucherNumber':202,'VoucherYear':1}}
        if path == '/3/modesofpayments/BANK': return {'ModeOfPayment':{'Description':'Bank EUR','Secret':'no'}}
        if path.startswith('/3/vouchers/'): return {'Voucher':{'VoucherNumber':int(path.split('/')[-1]),'VoucherRows':[{'Account':2662,'Credit':88.40,'Debit':0,'Description':'private','Secret':'no'},{'Account':4000,'Debit':133.70,'Credit':0},{'Account':1400,'Credit':133.70,'Debit':0}], 'Secret':'no'}}
        raise AssertionError(path)

@pytest.mark.asyncio
async def test_linked_projection():
    f=FX(); r=await invoice_evidence(f,'5001')
    assert len(r['vouchers'])==2
    assert r['payments'][0]['payment_method']['Description']=='Bank EUR'
    assert 'Secret' not in str(r) and 'private' not in str(r)
    assert r['vouchers'][0]['omitted_rows']==2
    assert '133.7' not in str(r)
    assert f.paths[1][1]['invoicenumber']=='5001'

@pytest.mark.asyncio
async def test_no_arbitrary_ledger_or_payment_crosslink():
    with pytest.raises(ValueError): await invoice_evidence(FX(),'../vouchers')
    f=FX(); original=f.get
    async def wrong(path,**params):
        r=await original(path,**params)
        if 'InvoicePayment' in r:r['InvoicePayment']['InvoiceNumber']=9999
        return r
    f.get=wrong
    with pytest.raises(ValueError): await invoice_evidence(f,'5001')
    assert not any(p[0].startswith('/3/vouchers/') for p in f.paths)

def test_roles():
    new={'get_invoice_payment_evidence'}
    assert new <= tool_names(load_server('sales'))
    assert not new & tool_names(load_server('sourcing'))
