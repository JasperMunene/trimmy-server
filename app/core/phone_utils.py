"""International phone number normaliser (wraps the `phonenumbers` library).

    from app.core.phone_utils import normalise_phone, normalise_for_sms

    normalise_phone("0712345678", default_country="KE")     # → "+254712345678"
    normalise_for_sms("0712345678", default_country="KE")   # → "254712345678"
"""

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

_DEFAULT_COUNTRY = "KE"


class InvalidPhoneNumberError(ValueError):
    """Raised when a phone number cannot be parsed or is invalid."""


def normalise_phone(raw: str, default_country: str = _DEFAULT_COUNTRY) -> str:
    """Return the number in full E.164 format (with leading +)."""
    raw = (raw or "").strip()
    if not raw:
        raise InvalidPhoneNumberError("Phone number is empty")

    try:
        parsed = phonenumbers.parse(raw, default_country)
    except NumberParseException as e:
        raise InvalidPhoneNumberError(f"Cannot parse phone number '{raw}': {e}")

    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumberError(f"Phone number '{raw}' is not valid")

    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def normalise_for_sms(raw: str, default_country: str = _DEFAULT_COUNTRY) -> str:
    """Like normalise_phone() but strips the leading '+' (HostPinnacle wants '254...')."""
    return normalise_phone(raw, default_country).lstrip("+")


def is_valid_phone(raw: str, default_country: str = _DEFAULT_COUNTRY) -> bool:
    try:
        normalise_phone(raw, default_country)
        return True
    except InvalidPhoneNumberError:
        return False
