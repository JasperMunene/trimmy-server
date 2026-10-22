"""Payment destination choices shown on the app's Payment Destination screen.

Served by GET /payment-destinations/options so the app renders the picker from
server data: adding a bank or tweaking a label doesn't need an app release.
The field patterns match the checks in BusinessService._validate_payment_destination.
"""

# Kenyan banks that accept M-Pesa deposits via a paybill ("bank shortcode"),
# with the account number as the paybill account. Verify against each bank's
# published M-Pesa paybill before going live.
BANKS = [
    {"name": "Absa Bank Kenya", "shortcode": "303030"},
    {"name": "Co-operative Bank of Kenya", "shortcode": "400200"},
    {"name": "Diamond Trust Bank (DTB)", "shortcode": "516600"},
    {"name": "Equity Bank Kenya", "shortcode": "247247"},
    {"name": "Family Bank", "shortcode": "222111"},
    {"name": "Gulf African Bank", "shortcode": "985050"},
    {"name": "HFC (HF Group)", "shortcode": "100400"},
    {"name": "I&M Bank", "shortcode": "542542"},
    {"name": "KCB Bank", "shortcode": "522522"},
    {"name": "National Bank of Kenya", "shortcode": "547700"},
    {"name": "NCBA Bank", "shortcode": "880100"},
    {"name": "Prime Bank", "shortcode": "982800"},
    {"name": "Sidian Bank", "shortcode": "111999"},
    {"name": "Stanbic Bank Kenya", "shortcode": "600100"},
    {"name": "Standard Chartered Kenya", "shortcode": "329329"},
]

TYPES = [
    {
        "type": "mpesa_till",
        "label": "M-Pesa Till (Buy Goods)",
        "description": "Customers pay to your Buy Goods till number.",
        "fields": [
            {
                "name": "tillNumber",
                "label": "Till number",
                "keyboard": "number",
                "pattern": r"^\d{5,7}$",
                "hint": "5-7 digits",
            },
        ],
    },
    {
        "type": "mpesa_paybill",
        "label": "M-Pesa Paybill",
        "description": "Customers pay to your business paybill and account number.",
        "fields": [
            {
                "name": "paybillNumber",
                "label": "Paybill number",
                "keyboard": "number",
                "pattern": r"^\d{5,6}$",
                "hint": "5-6 digits",
            },
            {
                "name": "paybillAccountNumber",
                "label": "Account number",
                "keyboard": "default",
                "pattern": r"^[A-Za-z0-9\-_/. ]{1,20}$",
                "hint": "Up to 20 characters: the account customers enter when paying",
            },
        ],
    },
    {
        "type": "bank_account",
        "label": "Bank account",
        "description": "Payments go straight to your bank account via the bank's M-Pesa paybill.",
        "fields": [
            {
                "name": "bankName",
                "label": "Bank",
                "keyboard": "picker",
                "pattern": r"^.+$",
                "hint": "Pick from banks; selecting one also fills bankShortcode",
            },
            {
                "name": "bankShortcode",
                "label": "Bank paybill",
                "keyboard": "number",
                "pattern": r"^\d{5,6}$",
                "hint": "Filled from the chosen bank; editable for banks not listed",
            },
            {
                "name": "bankAccountNumber",
                "label": "Account number",
                "keyboard": "default",
                "pattern": r"^[A-Za-z0-9]{5,20}$",
                "hint": "5-20 letters or digits",
            },
        ],
    },
]
