"""Send one real STK push through the payment-routing client and wait for the result.

Uses the M-Pesa settings in .env (MPESA_ENV decides sandbox vs production). With
production credentials this moves REAL money, so it asks before sending.

    python scripts/mpesa_stk.py --phone 0712345678 --amount 1 --till 123456
    python scripts/mpesa_stk.py --phone 0712345678 --amount 1 --paybill 400200 --account SALON12
    python scripts/mpesa_stk.py --phone 0712345678 --amount 1 --bank 247247 --account 0123456789

The result is read with STK Query (every 5s, up to 2 minutes), so no public
callback server is needed. Without MPESA_CALLBACK_URL a placeholder https URL
is sent; Daraja's callback to it is simply lost.
"""
import argparse
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.modules.payments.mpesa import MpesaClient, MpesaError, route_for  # noqa: E402

PLACEHOLDER_CALLBACK = "https://example.com/trimmy-mpesa-callback"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--phone", required=True, help="customer phone to prompt")
    p.add_argument("--amount", type=int, default=1, help="KES, whole number (default 1)")
    dest = p.add_mutually_exclusive_group(required=True)
    dest.add_argument("--till", help="route to this Buy Goods till")
    dest.add_argument("--paybill", help="route to this paybill (needs --account)")
    dest.add_argument("--bank", help="route to this bank's paybill shortcode (needs --account)")
    p.add_argument("--account", help="paybill account / bank account number (max 12 chars)")
    p.add_argument("--reference", default="TRIMMYTEST", help="label for till payments")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    if (args.paybill or args.bank) and not args.account:
        print("--account is required with --paybill and --bank")
        return 2

    destination = SimpleNamespace(
        type="mpesa_till" if args.till else "mpesa_paybill" if args.paybill else "bank_account",
        till_number=args.till,
        paybill_number=args.paybill,
        paybill_account_number=args.account,
        bank_shortcode=args.bank,
        bank_account_number=args.account,
    )
    try:
        route = route_for(destination, args.reference)
        callback = settings.MPESA_CALLBACK_URL or PLACEHOLDER_CALLBACK
        client = MpesaClient.from_settings() if settings.MPESA_CALLBACK_URL else MpesaClient(
            env=settings.MPESA_ENV, consumer_key=settings.MPESA_CONSUMER_KEY,
            consumer_secret=settings.MPESA_CONSUMER_SECRET, shortcode=settings.MPESA_SHORTCODE,
            passkey=settings.MPESA_PASSKEY, callback_url=callback,
        )
    except MpesaError as e:
        print(f"Can't send: {e.message}")
        return 2

    print(f"Environment : {client.env} ({client.base_url})")
    print(f"From        : shortcode {client.shortcode}")
    print(f"Prompt      : {args.phone} for KES {args.amount}")
    print(f"Route       : {route.transaction_type} -> PartyB {route.party_b}, AccountReference {route.account_reference!r}")
    if client.env == "production" and not args.yes:
        if input("This sends a REAL M-Pesa prompt. Type 'yes' to send: ").strip().lower() != "yes":
            print("Not sent.")
            return 1

    try:
        push = await client.stk_push(phone=args.phone, amount=args.amount, route=route, description="Trimmy test")
    except MpesaError as e:
        print(f"STK push failed: [{e.code}] {e.message}")
        return 1
    print(f"Sent: {push.customer_message} (CheckoutRequestID {push.checkout_request_id})")

    for _ in range(24):
        await asyncio.sleep(5)
        try:
            result = await client.stk_query(push.checkout_request_id)
        except MpesaError as e:
            print(f"  query error: [{e.code}] {e.message}")
            continue
        if result.status != "pending":
            print(f"Result: {result.status} ({result.failure_reason or 'ok'}) code={result.result_code} {result.result_desc!r}")
            return 0 if result.status == "succeeded" else 1
        print("  waiting for the customer...")
    print("No result after 2 minutes.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
