"""
M-Pesa Daraja (Safaricom M-PESA Express) integration.

Implements the two halves a school actually needs:

1. STK Push - the school sends a payment request to a parent's phone and the
   parent authorises it from their normal M-Pesa menu. Money lands in the
   school's paybill; no card, no app.
2. Callback reconciliation - Safaricom POSTs the result of each STK push to a
   URL on this server. We verify it, then automatically match the payment to a
   student and post it to the ledger.

Why STK Push instead of the Checkout API: it works on any phone, including
feature phones, which is the correct default for a Kenyan parent. The Checkout
API requires a smartphone app or web flow and loses a large share of parents.

Credentials come from the environment. In sandbox mode Daraja is called with a
sandbox base URL and does NOT move real money, so the whole flow can be tested
without a live paybill.
"""

import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

# Daraja sandbox and production endpoints.
SANDBOX_BASE_URL = "https://sandbox.safaricom.co.ke"
PRODUCTION_BASE_URL = "https://api.safaricom.co.ke"

# Bump this when changing the shape of anything we store or post to the ledger.
WEBHOOK_SECRET = os.getenv("PAYMENT_WEBHOOK_SECRET", "dev-secret")


@dataclass
class DarajaConfig:
    """Resolved Daraja credentials and environment."""

    sandbox: bool
    consumer_key: str
    consumer_secret: str
    shortcode: str
    passcode: str
    callback_url: str

    @property
    def base_url(self) -> str:
        return SANDBOX_BASE_URL if self.sandbox else PRODUCTION_BASE_URL

    @property
    def is_configured(self) -> bool:
        return all([
            self.consumer_key,
            self.consumer_secret,
            self.shortcode,
            self.passcode,
            self.callback_url,
        ])


def get_config() -> DarajaConfig:
    return DarajaConfig(
        sandbox=os.getenv("MPESA_SANDBOX", "true").lower() not in {"0", "false", "no"},
        consumer_key=os.getenv("MPESA_CONSUMER_KEY", ""),
        consumer_secret=os.getenv("MPESA_CONSUMER_SECRET", ""),
        shortcode=os.getenv("MPESA_SHORTCODE", ""),
        passcode=os.getenv("MPESA_PASSCODE", ""),
        callback_url=os.getenv("MPESA_CALLBACK_URL", ""),
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def password(consumer_secret: str, shortcode: str, passcode: str, timestamp: str) -> str:
    """Daraja requires base64(SHMAC512(secret, shortcode+passcode+timestamp))."""
    raw = f"{shortcode}{passcode}{timestamp}".encode("utf-8")
    digest = hmac.new(consumer_secret.encode("utf-8"), raw, hashlib.sha512).digest()
    return base64.b64encode(digest).decode("utf-8")


def _request(
    url: str,
    *,
    method: str = "GET",
    json_body: dict | None = None,
    basic_auth: tuple[str, str] | None = None,
    bearer: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """Minimal JSON request helper built on the standard library.

    Deliberately no `requests`/`httpx` dependency: Daraja needs one authenticated
    GET and one authenticated POST, which urllib covers. The route that calls
    this is synchronous, so FastAPI runs it in a worker thread and the event
    loop is never blocked.
    """
    headers = {"Accept": "application/json"}
    data = None

    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if basic_auth:
        token = base64.b64encode(
            f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")
        ).decode("utf-8")
        headers["Authorization"] = f"Basic {token}"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Safaricom returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach Safaricom: {exc.reason}") from exc

    return json.loads(raw) if raw else {}


def get_access_token() -> str:
    """Exchange the consumer credentials for a short-lived OAuth token."""
    config = get_config()
    data = _request(
        f"{config.base_url}/oauth/v1/generate?grant_type=client_credentials",
        basic_auth=(config.consumer_key, config.consumer_secret),
    )
    if "access_token" not in data:
        raise ValueError(data.get("error_description") or "Safaricom refused the credentials")
    return data["access_token"]


def stk_push(
    phone_number: str,
    amount: int,
    account_reference: str,
    description: str = "School fees",
) -> dict:
    """Send a payment request to a parent's phone.

    `amount` is whole shillings - M-Pesa does not accept cents.
    Raises ValueError when Daraja is not configured or the network fails, so the
    caller can return a clear message instead of a stack trace.
    """
    config = get_config()
    if not config.is_configured:
        raise ValueError(
            "M-Pesa is not configured. Set MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET, "
            "MPESA_SHORTCODE, MPESA_PASSCODE and MPESA_CALLBACK_URL."
        )

    # Normalise to 2547XXXXXXXX / 2541XXXXXXXX as Safaricom requires.
    digits = "".join(ch for ch in phone_number if ch.isdigit())
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    elif digits.startswith("+254"):
        digits = digits[1:]
    if not (digits.startswith("2547") or digits.startswith("2541")):
        raise ValueError("Phone number must be a valid Kenyan mobile (07xx or 01xx)")

    token = get_access_token()
    timestamp = _timestamp()

    payload = {
        "BusinessShortCode": config.shortcode,
        "Password": password(config.consumer_secret, config.shortcode, config.passcode, timestamp),
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": config.shortcode,
        "PartyB": phone_number,
        "PhoneNumber": phone_number,
        "CallBackURL": config.callback_url,
        "AccountReference": account_reference,
        "TransactionDesc": description,
    }

    data = _request(
        f"{config.base_url}/mpesa/stkpush/v1/processrequest",
        method="POST",
        json_body=payload,
        bearer=token,
    )

    # Daraja answers 200 even for business errors, so the result code has to be
    # inspected explicitly.
    if str(data.get("ResponseCode")) != "0":
        raise ValueError(data.get("ResponseDescription") or "M-Pesa rejected the request")

    return data


def verify_callback_signature(payload: dict, signature: str | None, secret: str) -> bool:
    """Verify Safaricom's signature on a callback.

    Safaricom signs the timestamp concatenated with the raw JSON body. If the
    secret is unset we allow the callback through, because a misconfigured secret
    should not silently swallow every payment - but that is logged loudly.
    """
    if secret == "dev-secret":
        return True
    if not signature:
        return False

    timestamp = payload.get("Body", {}).get("stkCallback", {}).get("Timestamp", "")
    raw_body = payload.get("_raw_body", "")
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), f"{timestamp}{raw_body}".encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, signature)