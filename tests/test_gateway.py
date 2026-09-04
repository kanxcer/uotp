import sys, json, hashlib, hmac
sys.path.insert(0,"src")
from uotpbot.gateway import FamGateway, verify_webhook_signature, FamGatewayError
from decimal import Decimal

KEY="testkey123"
class FakeResp:
    def __init__(self, b): self._b=b
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self,*a): pass

class FakeOpener:
    def __init__(self, payload): self.payload=payload; self.urls=[]
    def urlopen(self, req, timeout=12):
        self.urls.append(req.full_url)
        return FakeResp(json.dumps(self.payload).encode())

# create_order
op=FakeOpener({"status":"success","data":{"order_id":"fg_ABC","amount":"100.00","payable_amount":"100.04","upi_id":"merchant@fam","qr_url":"https://famgateway.in/api/qr-image.php?order_id=fg_ABC","checkout_url":"https://famgateway.in/pay.php?order_id=fg_ABC","upi_intent":"upi://pay?pa=merchant@fam","expires_at_ist":"02-09-2026 14:35:00"}})
fg=FamGateway(KEY,opener=op)
o=fg.create_order(100)
assert o.order_id=="fg_ABC", o
assert o.payable_amount==Decimal("100.04"), o.payable_amount
assert o.qr_url.endswith("fg_ABC")
assert "api_key=testkey123" in op.urls[0]
print("create_order OK, payable", o.payable_amount)

# verify paid
op2=FakeOpener({"status":"success","data":{"order_id":"fg_ABC","utr":"420987654321","sender_name":"Rahul Sharma","amount":100}})
fg2=FamGateway(KEY,opener=op2)
s=fg2.verify("fg_ABC")
assert s.is_paid and s.utr=="420987654321" and s.sender_name=="Rahul Sharma"
print("verify paid OK utr", s.utr)

# verify pending
op3=FakeOpener({"status":"pending"})
s3=FamGateway(KEY,opener=op3).verify("fg_X")
assert s3.state=="pending" and not s3.is_paid
print("verify pending OK")

# webhook signature
body=json.dumps({"event":"payment.success","order_id":"fg_ABC","amount":100,"utr":"420987654321","sender_name":"Rahul Sharma","status":"success"},separators=(",",":")).encode()
sig=hmac.new(KEY.encode(),body,hashlib.sha256).hexdigest()
assert verify_webhook_signature(body,sig,KEY) is True
assert verify_webhook_signature(body,sig,"wrongkey") is False
assert verify_webhook_signature(body,None,KEY) is False
print("webhook signature OK")

# error mapping
class ErrResp:
    def read(self): return b""
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self,*a): pass
import urllib.error
class ErrOpener:
    def urlopen(self,req,timeout=12): raise urllib.error.HTTPError("u",404,b"",{},ErrResp())
try:
    FamGateway(KEY,opener=ErrOpener()).create_order(50)
    print("FAIL should raise")
except FamGatewayError as e:
    print("error mapping OK:", e)
print("ALL GATEWAY TESTS PASSED")


def test_request_sends_a_real_user_agent():
    """FamGateway rejects urllib's default 'Python-urllib/*' user-agent with a
    403, so the client must send a browser-ish UA on every request. This guards
    against the fix being dropped (which made 'Add money -> create payment'
    fail with 'Could not create your payment')."""
    import json

    class _Resp:
        def __init__(self, b):
            self._b = b
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    class _Opener:
        def __init__(self):
            self.req = None
            self.ua = None
        def urlopen(self, req, timeout=12):
            self.req = req
            self.ua = req.get_header("User-agent")
            return _Resp(json.dumps({"status": "success", "data": {
                "order_id": "fg_UA", "amount": "50",
                "payable_amount": "50"}}).encode())

    op = _Opener()
    fg = FamGateway("k", opener=op)
    fg.create_order(50)
    assert op.ua and "Python-urllib" not in op.ua, \
        f"must not use urllib's blocked UA; got {op.ua!r}"
