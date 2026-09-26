"""M-Pesa Daraja client: STK push with payment routing, STK query, callback parsing.

Payment routing (docs/booking/PLAN.md, D1): Trimmy's Daraja app and shortcode
sign every push, and PartyB is the business's own payment destination, so the
money lands with the business and Trimmy never holds it. Ported from the
payment-routing-test prototype (mpesa.go).

    client = MpesaClient.from_settings()
    push = await client.request_payment(destination, phone="0712345678", amount=500,
                                        reference="BK7F3A", description="Deposit")
    ...later, from the callback or a reconciliation job:
    result = parse_callback(body)                  # or await client.stk_query(push.checkout_request_id)
"""
import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Protocol
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.core.phone_utils import InvalidPhoneNumberError, normalise_for_sms

logger = logging.getLogger(__name__)

BASE_URLS = {
    "sandbox": "https://sandbox.safaricom.co.ke",
    "production": "https://api.safaricom.co.ke",
}

# Daraja timestamps are Kenyan local time.
_NAIROBI = ZoneInfo("Africa/Nairobi")
# Refresh the OAuth token this long before Daraja says it expires.
_TOKEN_SAFETY_SECONDS = 60
# STK query answers with this error code while the customer hasn't responded yet.
_STILL_PROCESSING = "500.001.1001"

_SHORTCODE_RE = re.compile(r"^\d{5,7}$")
# Daraja caps AccountReference at 12 characters.
_ACCOUNT_REF_RE = re.compile(r"^[A-Za-z0-9\-_/. ]{1,12}$")
_SAFARICOM_PHONE_RE = re.compile(r"^254[17]\d{8}$")


class MpesaError(Exception):
    """A failure the caller can show or log. `code` is stable, `message` is for humans."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MpesaConfigError(MpesaError):
    pass


# ─── Routing ─────────────────────────────────────────────────


class Destination(Protocol):
    """The fields of a business PaymentDestination that routing needs."""

    type: str
    till_number: Optional[str]
    paybill_number: Optional[str]
    paybill_account_number: Optional[str]
    bank_shortcode: Optional[str]
    bank_account_number: Optional[str]


@dataclass(frozen=True)
class Route:
    transaction_type: str  # CustomerBuyGoodsOnline | CustomerPayBillOnline
    party_b: str
    account_reference: str


def route_for(destination: Destination, reference: str) -> Route:
    """Where an STK push for this business goes.

    `reference` labels till payments (tills have no account number); paybill and
    bank payments must carry the business's account number instead.
    """
    if destination.type == "mpesa_till":
        return Route(
            "CustomerBuyGoodsOnline",
            _shortcode(destination.till_number, "till number"),
            _account_reference(reference, "reference"),
        )
    if destination.type == "mpesa_paybill":
        return Route(
            "CustomerPayBillOnline",
            _shortcode(destination.paybill_number, "paybill number"),
            _account_reference(destination.paybill_account_number, "paybill account number"),
        )
    if destination.type == "bank_account":
        return Route(
            "CustomerPayBillOnline",
            _shortcode(destination.bank_shortcode, "bank paybill"),
            _account_reference(destination.bank_account_number, "bank account number"),
        )
    raise MpesaError("unsupported_destination", f"Unknown payment destination type {destination.type!r}")


def _shortcode(value: Optional[str], label: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if not _SHORTCODE_RE.match(digits):
        raise MpesaError("invalid_destination", f"The business's {label} must be 5-7 digits")
    return digits


def _account_reference(value: Optional[str], label: str) -> str:
    value = (value or "").strip()
    if not _ACCOUNT_REF_RE.match(value):
        raise MpesaError(
            "invalid_account_reference",
            f"The {label} must be 1-12 characters (letters, digits, -_/. and spaces) for M-Pesa",
        )
    return value


def normalise_mpesa_phone(raw: str) -> str:
    """'0712345678', '+254712345678', '712345678' → '254712345678'."""
    try:
        phone = normalise_for_sms(raw, default_country="KE")
    except InvalidPhoneNumberError:
        phone = ""
    if not _SAFARICOM_PHONE_RE.match(phone):
        raise MpesaError("invalid_phone", "Enter a Kenyan mobile number, e.g. 0712 345 678")
    return phone


# ─── Results ─────────────────────────────────────────────────


@dataclass(frozen=True)
class StkPushResult:
    merchant_request_id: str
    checkout_request_id: str
    customer_message: str


@dataclass(frozen=True)
class PaymentResult:
    """The outcome of one STK push, from the callback or an STK query."""

    status: str  # succeeded | failed | timeout | pending
    failure_reason: Optional[str]  # cancelled | insufficient_funds | wrong_pin | timeout | busy | failed
    result_code: Optional[int]
    result_desc: str
    checkout_request_id: str = ""
    receipt: Optional[str] = None
    amount: Optional[int] = None
    phone: Optional[str] = None


# Daraja ResultCode → (status, failure_reason). Anything unlisted is a generic failure.
_RESULT_CODES: dict[int, tuple[str, Optional[str]]] = {
    0: ("succeeded", None),
    1: ("failed", "insufficient_funds"),
    1001: ("failed", "busy"),  # another M-Pesa transaction is in progress on this phone
    1019: ("timeout", "timeout"),  # transaction expired
    1032: ("failed", "cancelled"),  # customer dismissed the prompt
    1037: ("timeout", "timeout"),  # phone unreachable / no response
    2001: ("failed", "wrong_pin"),
}


def interpret_result_code(code: int) -> tuple[str, Optional[str]]:
    return _RESULT_CODES.get(code, ("failed", "failed"))


def parse_callback(body: dict[str, Any]) -> PaymentResult:
    """Parse the JSON Daraja POSTs to the callback URL."""
    try:
        cb = body["Body"]["stkCallback"]
        code = int(cb["ResultCode"])
        checkout_id = str(cb["CheckoutRequestID"])
    except (KeyError, TypeError, ValueError) as e:
        raise MpesaError("invalid_callback", f"Unrecognised M-Pesa callback: {e}")

    items = {
        item.get("Name"): item.get("Value")
        for item in (cb.get("CallbackMetadata") or {}).get("Item", [])
        if isinstance(item, dict)
    }
    status, reason = interpret_result_code(code)
    amount = items.get("Amount")
    phone = items.get("PhoneNumber")
    return PaymentResult(
        status=status,
        failure_reason=reason,
        result_code=code,
        result_desc=str(cb.get("ResultDesc", "")),
        checkout_request_id=checkout_id,
        receipt=items.get("MpesaReceiptNumber"),
        amount=int(float(amount)) if amount is not None else None,
        phone=str(phone) if phone is not None else None,
    )


# ─── Client ──────────────────────────────────────────────────


class PaymentProvider(Protocol):
    """What booking code depends on, so another provider could replace M-Pesa (PLAN.md risks)."""

    async def request_payment(
        self, destination: Destination, *, phone: str, amount: int, reference: str, description: str
    ) -> StkPushResult: ...

    async def check_payment(self, checkout_request_id: str) -> PaymentResult: ...


class MpesaClient:
    def __init__(
        self,
        *,
        env: str,
        consumer_key: str,
        consumer_secret: str,
        shortcode: str,
        passkey: str,
        callback_url: str,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if env not in BASE_URLS:
            raise MpesaConfigError("mpesa_not_configured", f"MPESA_ENV must be one of {', '.join(BASE_URLS)}")
        missing = [
            name
            for name, value in (
                ("MPESA_CONSUMER_KEY", consumer_key),
                ("MPESA_CONSUMER_SECRET", consumer_secret),
                ("MPESA_SHORTCODE", shortcode),
                ("MPESA_PASSKEY", passkey),
                ("MPESA_CALLBACK_URL", callback_url),
            )
            if not value
        ]
        if missing:
            raise MpesaConfigError("mpesa_not_configured", f"M-Pesa is missing settings: {', '.join(missing)}")

        self.env = env
        self.base_url = BASE_URLS[env]
        self._key, self._secret = consumer_key, consumer_secret
        self.shortcode, self._passkey = shortcode, passkey
        self.callback_url = callback_url
        self._transport = transport
        self._clock = clock
        self._token: Optional[str] = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, **overrides: Any) -> "MpesaClient":
        callback = settings.MPESA_CALLBACK_URL.rstrip("/")
        if callback and settings.MPESA_CALLBACK_SECRET:
            callback = f"{callback}/{settings.MPESA_CALLBACK_SECRET}"
        elif callback:
            raise MpesaConfigError("mpesa_not_configured", "M-Pesa is missing settings: MPESA_CALLBACK_SECRET")
        return cls(
            env=settings.MPESA_ENV,
            consumer_key=settings.MPESA_CONSUMER_KEY,
            consumer_secret=settings.MPESA_CONSUMER_SECRET,
            shortcode=settings.MPESA_SHORTCODE,
            passkey=settings.MPESA_PASSKEY,
            callback_url=callback,
            **overrides,
        )

    # PaymentProvider

    async def request_payment(
        self, destination: Destination, *, phone: str, amount: int, reference: str, description: str
    ) -> StkPushResult:
        return await self.stk_push(
            phone=phone, amount=amount, route=route_for(destination, reference), description=description
        )

    async def check_payment(self, checkout_request_id: str) -> PaymentResult:
        return await self.stk_query(checkout_request_id)

    # Daraja calls

    async def stk_push(self, *, phone: str, amount: int, route: Route, description: str) -> StkPushResult:
        if amount < 1:
            raise MpesaError("invalid_amount", "Amount must be at least KES 1")
        msisdn = normalise_mpesa_phone(phone)
        timestamp = self._timestamp()
        body = {
            "BusinessShortCode": self.shortcode,
            "Password": self._password(timestamp),
            "Timestamp": timestamp,
            "TransactionType": route.transaction_type,
            "Amount": str(int(amount)),
            "PartyA": msisdn,
            "PartyB": route.party_b,
            "PhoneNumber": msisdn,
            "CallBackURL": self.callback_url,
            "AccountReference": route.account_reference,
            # Shown to the customer on the prompt; Daraja allows up to 13 characters.
            "TransactionDesc": description[:13] or "Payment",
        }
        data = await self._post("/mpesa/stkpush/v1/processrequest", body)
        if str(data.get("ResponseCode")) != "0":
            raise MpesaError("stk_push_rejected", data.get("ResponseDescription") or "M-Pesa rejected the request")
        return StkPushResult(
            merchant_request_id=str(data.get("MerchantRequestID", "")),
            checkout_request_id=str(data["CheckoutRequestID"]),
            customer_message=str(data.get("CustomerMessage", "")),
        )

    async def stk_query(self, checkout_request_id: str) -> PaymentResult:
        timestamp = self._timestamp()
        body = {
            "BusinessShortCode": self.shortcode,
            "Password": self._password(timestamp),
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }
        try:
            data = await self._post("/mpesa/stkpushquery/v1/query", body)
        except MpesaError as e:
            if e.code == _STILL_PROCESSING:
                return PaymentResult("pending", None, None, e.message, checkout_request_id)
            raise
        code = int(data.get("ResultCode", -1))
        status, reason = interpret_result_code(code)
        return PaymentResult(status, reason, code, str(data.get("ResultDesc", "")), checkout_request_id)

    # Plumbing

    def _timestamp(self) -> str:
        return datetime.now(_NAIROBI).strftime("%Y%m%d%H%M%S")

    def _password(self, timestamp: str) -> str:
        return base64.b64encode(f"{self.shortcode}{self._passkey}{timestamp}".encode()).decode()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=30, transport=self._transport)

    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._token and self._clock() < self._token_expires_at:
                return self._token
            async with self._client() as client:
                try:
                    resp = await client.get(
                        "/oauth/v1/generate",
                        params={"grant_type": "client_credentials"},
                        auth=(self._key, self._secret),
                    )
                except httpx.HTTPError as e:
                    raise MpesaError("mpesa_unavailable", f"Could not reach M-Pesa: {e}")
            if resp.status_code != 200:
                raise MpesaError("mpesa_auth_failed", f"M-Pesa rejected the app credentials ({resp.status_code})")
            data = resp.json()
            self._token = data["access_token"]
            expires_in = int(data.get("expires_in", 3599))
            self._token_expires_at = self._clock() + max(expires_in - _TOKEN_SAFETY_SECONDS, 0)
            return self._token

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token = await self._access_token()
        async with self._client() as client:
            try:
                resp = await client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                raise MpesaError("mpesa_unavailable", f"Could not reach M-Pesa: {e}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 401:
            # Token revoked early: drop it so the next call fetches a new one.
            self._token = None
        if resp.status_code != 200:
            raise MpesaError(
                str(data.get("errorCode") or f"http_{resp.status_code}"),
                str(data.get("errorMessage") or resp.text[:300] or "M-Pesa request failed"),
            )
        return data
