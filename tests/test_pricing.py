import uuid

import pytest

from app.modules.booking.pricing import DepositPolicy, Line, PricingError, quote_booking

CUT, COLOUR, NAILS = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
CART = [Line(CUT, 800, 45), Line(COLOUR, 2500, 90), Line(NAILS, 600, 30, quantity=2)]  # total 4500, 195 min


def test_total_and_duration_include_quantity():
    quote = quote_booking(CART, None)
    assert (quote.total, quote.duration_minutes, quote.currency, quote.deposit) == (4500, 195, "KES", None)


@pytest.mark.parametrize("policy,deposit", [
    # Not required
    (None, None),
    (DepositPolicy(required=False, type="fixed", amount=500), None),
    # All services
    (DepositPolicy(required=True, type="fixed", amount=500), 500),
    (DepositPolicy(required=True, type="fixed", amount=9000), 4500),  # capped at the total
    (DepositPolicy(required=True, type="percent", percent=30), 1350),
    (DepositPolicy(required=True, type="percent", percent=33), 1485),
    (DepositPolicy(required=True, type="percent", percent=100), 4500),
    (DepositPolicy(required=True, type="percent", percent=0), None),
    (DepositPolicy(required=True, type="fixed", amount=0), None),
    # Selected services: based only on those lines
    (DepositPolicy(required=True, type="percent", percent=50, applies_to="selected_services", service_ids=frozenset({COLOUR})), 1250),
    (DepositPolicy(required=True, type="percent", percent=50, applies_to="selected_services", service_ids=frozenset({NAILS})), 600),  # 2 x 600
    (DepositPolicy(required=True, type="fixed", amount=1000, applies_to="selected_services", service_ids=frozenset({CUT})), 800),  # capped at that line
    (DepositPolicy(required=True, type="fixed", amount=300, applies_to="selected_services", service_ids=frozenset({CUT, COLOUR})), 300),
    # Selected services, none in the cart
    (DepositPolicy(required=True, type="fixed", amount=300, applies_to="selected_services", service_ids=frozenset({uuid.uuid4()})), None),
    (DepositPolicy(required=True, type="fixed", amount=300, applies_to="selected_services", service_ids=frozenset()), None),
])
def test_deposit(policy, deposit):
    assert quote_booking(CART, policy).deposit == deposit


def test_percent_rounds_up_to_a_whole_shilling():
    assert quote_booking([Line(CUT, 999, 30)], DepositPolicy(required=True, type="percent", percent=10)).deposit == 100


def test_selected_service_ids_may_be_strings():
    policy = DepositPolicy(required=True, type="percent", percent=50, applies_to="selected_services", service_ids=frozenset({str(COLOUR)}))
    assert quote_booking(CART, policy).deposit == 1250


@pytest.mark.parametrize("lines", [[], [Line(CUT, 800, 45, quantity=0)], [Line(CUT, 800, 45), Line(COLOUR, 20, 90, currency="USD")]])
def test_rejects_bad_carts(lines):
    with pytest.raises(PricingError):
        quote_booking(lines, None)


def test_unknown_deposit_type():
    with pytest.raises(PricingError):
        quote_booking(CART, DepositPolicy(required=True, type="bogus"))
