import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from app.modules.payments.mpesa import (
    MpesaClient,
    MpesaConfigError,
    MpesaError,
    Route,
    interpret_result_code,
    normalise_mpesa_phone,
    parse_callback,
    route_for,
)


def destination(**fields):
    base = dict(
        type="mpesa_till", till_number=None, paybill_number=None, paybill_account_number=None,
        bank_shortcode=None, bank_account_number=None,
    )
    base.update(fields)
    return SimpleNamespace(**base)


class FakeDaraja:
    """Records requests and answers like Daraja."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.push_response = (200, {
            "MerchantRequestID": "29115-34620561-1", "CheckoutRequestID": "ws_CO_191220191020363925",
            "ResponseCode": "0", "ResponseDescription": "Success. Request accepted for processing",
            "CustomerMessage": "Success. Request accepted for processing",
        })
        self.query_response = (200, {"ResponseCode": "0", "ResultCode": "0", "ResultDesc": "The service request is processed successfully."})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth/v1/generate":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"token-{self.token_calls}", "expires_in": "3599"})
        if request.url.path == "/mpesa/stkpush/v1/processrequest":
            return httpx.Response(self.push_response[0], json=self.push_response[1])
        if request.url.path == "/mpesa/stkpushquery/v1/query":
            return httpx.Response(self.query_response[0], json=self.query_response[1])
        return httpx.Response(404)

    def last_json(self, path):
        req = [r for r in self.requests if r.url.path == path][-1]
        return json.loads(req.content)


@pytest.fixture
def daraja():
    return FakeDaraja()


def make_client(daraja, env="production", **kwargs):
    now = [1000.0]
    client = MpesaClient(
        env=env, consumer_key="key", consumer_secret="secret", shortcode="1234567",
        passkey="passkey", callback_url="https://api.trimmy.com/api/v1/payments/mpesa/callback/s3cret",
        transport=httpx.MockTransport(daraja.handler), clock=lambda: now[0], **kwargs,
    )
    return client, now


# ─── Routing ─────────────────────────────────────────────────


def test_route_till_uses_buy_goods_and_reference():
    route = route_for(destination(type="mpesa_till", till_number="123 456"), "BK7F3A")
    assert route == Route("CustomerBuyGoodsOnline", "123456", "BK7F3A")


def test_route_paybill_uses_business_account():
    route = route_for(destination(type="mpesa_paybill", paybill_number="400200", paybill_account_number="SALON-12"), "BK7F3A")
    assert route == Route("CustomerPayBillOnline", "400200", "SALON-12")


def test_route_bank_uses_bank_paybill_and_account():
    route = route_for(destination(type="bank_account", bank_shortcode="247247", bank_account_number="0123456789"), "BK7F3A")
    assert route == Route("CustomerPayBillOnline", "247247", "0123456789")


@pytest.mark.parametrize("dest,code", [
    (destination(type="bank_account", bank_shortcode="247247", bank_account_number="1234567890123"), "invalid_account_reference"),
    (destination(type="mpesa_paybill", paybill_number="400200", paybill_account_number=""), "invalid_account_reference"),
    (destination(type="mpesa_till", till_number="12"), "invalid_destination"),
    (destination(type="cheque"), "unsupported_destination"),
])
def test_route_rejects_what_daraja_would(dest, code):
    with pytest.raises(MpesaError) as e:
        route_for(dest, "BK7F3A")
    assert e.value.code == code


@pytest.mark.parametrize("raw", ["0712345678", "+254712345678", "254712345678", "712345678", "0112345678"])
def test_normalise_phone(raw):
    assert normalise_mpesa_phone(raw) in ("254712345678", "254112345678")


@pytest.mark.parametrize("raw", ["", "0201234567", "12345", "+14155550123"])
def test_normalise_phone_rejects(raw):
    with pytest.raises(MpesaError) as e:
        normalise_mpesa_phone(raw)
    assert e.value.code == "invalid_phone"


# ─── STK push ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stk_push_request(daraja):
    client, _ = make_client(daraja)
    result = await client.request_payment(
        destination(type="bank_account", bank_shortcode="247247", bank_account_number="0123456789"),
        phone="0712345678", amount=500, reference="BK7F3A", description="Trimmy deposit",
    )
    assert result.checkout_request_id == "ws_CO_191220191020363925"

    body = daraja.last_json("/mpesa/stkpush/v1/processrequest")
    assert body["BusinessShortCode"] == "1234567"
    assert body["TransactionType"] == "CustomerPayBillOnline"
    assert body["PartyA"] == body["PhoneNumber"] == "254712345678"
    assert body["PartyB"] == "247247"
    assert body["AccountReference"] == "0123456789"
    assert body["Amount"] == "500"
    assert body["CallBackURL"].endswith("/callback/s3cret")
    assert len(body["TransactionDesc"]) <= 13
    assert base64.b64decode(body["Password"]).decode() == f"1234567passkey{body['Timestamp']}"
    assert len(body["Timestamp"]) == 14

    push = [r for r in daraja.requests if r.url.path.endswith("processrequest")][-1]
    assert push.url.host == "api.safaricom.co.ke"
    assert push.headers["authorization"] == "Bearer token-1"


@pytest.mark.asyncio
async def test_sandbox_host(daraja):
    client, _ = make_client(daraja, env="sandbox")
    await client.stk_push(phone="0712345678", amount=1, route=Route("CustomerBuyGoodsOnline", "123456", "X"), description="t")
    assert all(r.url.host == "sandbox.safaricom.co.ke" for r in daraja.requests)


@pytest.mark.asyncio
async def test_token_is_cached_until_near_expiry(daraja):
    client, now = make_client(daraja)
    route = Route("CustomerBuyGoodsOnline", "123456", "X")
    await client.stk_push(phone="0712345678", amount=1, route=route, description="t")
    await client.stk_push(phone="0712345678", amount=1, route=route, description="t")
    assert daraja.token_calls == 1

    now[0] += 3599 - 60 + 1  # past expiry minus the safety margin
    await client.stk_push(phone="0712345678", amount=1, route=route, description="t")
    assert daraja.token_calls == 2


@pytest.mark.asyncio
async def test_stk_push_error_surfaces_daraja_message(daraja):
    daraja.push_response = (400, {"requestId": "1", "errorCode": "400.002.02", "errorMessage": "Bad Request - Invalid PartyB"})
    client, _ = make_client(daraja)
    with pytest.raises(MpesaError) as e:
        await client.stk_push(phone="0712345678", amount=1, route=Route("CustomerBuyGoodsOnline", "123456", "X"), description="t")
    assert e.value.code == "400.002.02"
    assert "Invalid PartyB" in e.value.message


@pytest.mark.asyncio
async def test_stk_push_rejects_bad_amount_before_calling(daraja):
    client, _ = make_client(daraja)
    with pytest.raises(MpesaError):
        await client.stk_push(phone="0712345678", amount=0, route=Route("CustomerBuyGoodsOnline", "123456", "X"), description="t")
    assert daraja.requests == []


# ─── STK query ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stk_query_success(daraja):
    client, _ = make_client(daraja)
    result = await client.check_payment("ws_CO_1")
    assert (result.status, result.failure_reason, result.result_code) == ("succeeded", None, 0)
    assert daraja.last_json("/mpesa/stkpushquery/v1/query")["CheckoutRequestID"] == "ws_CO_1"


@pytest.mark.asyncio
async def test_stk_query_cancelled(daraja):
    daraja.query_response = (200, {"ResponseCode": "0", "ResultCode": "1032", "ResultDesc": "Request cancelled by user"})
    client, _ = make_client(daraja)
    result = await client.stk_query("ws_CO_1")
    assert (result.status, result.failure_reason) == ("failed", "cancelled")


@pytest.mark.asyncio
async def test_stk_query_still_processing_is_pending(daraja):
    daraja.query_response = (500, {"requestId": "1", "errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"})
    client, _ = make_client(daraja)
    result = await client.stk_query("ws_CO_1")
    assert result.status == "pending"


# ─── Callback + result codes ─────────────────────────────────


def test_parse_callback_success():
    result = parse_callback({"Body": {"stkCallback": {
        "MerchantRequestID": "29115-34620561-1", "CheckoutRequestID": "ws_CO_1", "ResultCode": 0,
        "ResultDesc": "The service request is processed successfully.",
        "CallbackMetadata": {"Item": [
            {"Name": "Amount", "Value": 500.00}, {"Name": "MpesaReceiptNumber", "Value": "NLJ7RT61SV"},
            {"Name": "TransactionDate", "Value": 20191219102115}, {"Name": "PhoneNumber", "Value": 254712345678},
        ]},
    }}})
    assert result.status == "succeeded"
    assert (result.checkout_request_id, result.receipt, result.amount, result.phone) == ("ws_CO_1", "NLJ7RT61SV", 500, "254712345678")


def test_parse_callback_cancelled_has_no_metadata():
    result = parse_callback({"Body": {"stkCallback": {
        "MerchantRequestID": "1", "CheckoutRequestID": "ws_CO_2", "ResultCode": 1032, "ResultDesc": "Request cancelled by user",
    }}})
    assert (result.status, result.failure_reason, result.receipt) == ("failed", "cancelled", None)


def test_parse_callback_rejects_garbage():
    with pytest.raises(MpesaError) as e:
        parse_callback({"hello": "world"})
    assert e.value.code == "invalid_callback"


@pytest.mark.parametrize("code,expected", [
    (0, ("succeeded", None)), (1, ("failed", "insufficient_funds")), (1032, ("failed", "cancelled")),
    (1037, ("timeout", "timeout")), (2001, ("failed", "wrong_pin")), (9999, ("failed", "failed")),
])
def test_result_codes(code, expected):
    assert interpret_result_code(code) == expected


# ─── Config ──────────────────────────────────────────────────


def test_missing_config_is_explicit():
    with pytest.raises(MpesaConfigError) as e:
        MpesaClient(env="production", consumer_key="", consumer_secret="s", shortcode="1", passkey="p", callback_url="https://x")
    assert "MPESA_CONSUMER_KEY" in e.value.message


def test_unknown_env():
    with pytest.raises(MpesaConfigError):
        MpesaClient(env="staging", consumer_key="k", consumer_secret="s", shortcode="1", passkey="p", callback_url="https://x")


def test_from_settings_appends_callback_secret(monkeypatch):
    from app.core.config import settings
    for name, value in dict(
        MPESA_ENV="production", MPESA_CONSUMER_KEY="k", MPESA_CONSUMER_SECRET="s", MPESA_SHORTCODE="1234567",
        MPESA_PASSKEY="p", MPESA_CALLBACK_URL="https://api.trimmy.com/api/v1/payments/mpesa/callback/", MPESA_CALLBACK_SECRET="abc",
    ).items():
        monkeypatch.setattr(settings, name, value)
    assert MpesaClient.from_settings().callback_url == "https://api.trimmy.com/api/v1/payments/mpesa/callback/abc"
    monkeypatch.setattr(settings, "MPESA_CALLBACK_SECRET", "")
    with pytest.raises(MpesaConfigError):
        MpesaClient.from_settings()


# ─── Callback route ──────────────────────────────────────────

CALLBACK = {"Body": {"stkCallback": {
    "MerchantRequestID": "1", "CheckoutRequestID": "ws_CO_9", "ResultCode": 1032, "ResultDesc": "Request cancelled by user",
}}}


@pytest.fixture
def callback_secret(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "MPESA_CALLBACK_SECRET", "s3cret-value")
    return "s3cret-value"


@pytest.mark.asyncio
async def test_callback_route_acknowledges(client, callback_secret):
    resp = await client.post(f"/api/v1/payments/mpesa/callback/{callback_secret}", json=CALLBACK)
    assert resp.status_code == 200
    assert resp.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}


@pytest.mark.asyncio
async def test_callback_route_wrong_secret_is_404(client, callback_secret):
    resp = await client.post("/api/v1/payments/mpesa/callback/guess", json=CALLBACK)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_callback_route_disabled_without_secret(client, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "MPESA_CALLBACK_SECRET", "")
    resp = await client.post("/api/v1/payments/mpesa/callback/anything", json=CALLBACK)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_callback_route_rejects_garbage(client, callback_secret):
    resp = await client.post(f"/api/v1/payments/mpesa/callback/{callback_secret}", json={"nope": 1})
    assert resp.status_code == 400
