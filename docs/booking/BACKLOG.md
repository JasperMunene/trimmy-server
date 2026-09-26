# Booking backlog

Tasks for [PLAN.md](PLAN.md). API changes the app needs to pick up are logged in [FRONTEND_CHANGES.md](FRONTEND_CHANGES.md). To pick one up, check its dependencies are `done`, then put your name in **Owner** and set **Status** to `in progress`. Update the row when you open the PR (`in review`) and when it merges (`done`). The detail for each task, including acceptance criteria, is below the table.

Sizes: **S** is up to a day, **M** is 2–3 days, **L** is about a week. Status is one of `todo`, `in progress`, `in review`, `done` or `blocked`.

| ID | Task | Milestone | Size | Depends on | Owner | Status |
| --- | --- | --- | --- | --- | --- | --- |
| BK-01 | Confirm decisions D1–D9 | M0 | S | — | | in progress |
| BK-02 | Staff identity: `Staff.id` as `staffId`, working days on `Staff` | M0 | M | BK-01 | eslieh | done |
| BK-03 | Staff ↔ services mapping | M0 | M | BK-02 | eslieh | done |
| BK-04 | Business timezone and booking settings | M0 | S | BK-01 | eslieh | done |
| BK-05 | Run tests on Postgres in CI | M0 | S | — | eslieh | in review |
| BK-10 | Availability engine (pure function) | M1 | L | BK-02, BK-03, BK-04 | eslieh | done |
| BK-11 | Availability endpoints | M1 | M | BK-10 | eslieh | done |
| BK-20 | Booking tables, state machine, exclusion constraint | M2 | M | BK-02, BK-05 | eslieh | done |
| BK-21 | Price and deposit calculator | M2 | S | — | eslieh | done |
| BK-22 | `POST /bookings` | M2 | L | BK-10, BK-20, BK-21 | eslieh | done |
| BK-23 | `GET /bookings/{id}` and `GET /me/bookings` | M2 | S | BK-20 | eslieh | done |
| BK-24 | Expire unpaid holds | M2 | S | BK-20, BK-25 | eslieh | done |
| BK-25 | Periodic jobs in the worker (`--beat`) | M2 | M | — | eslieh | done |
| BK-26 | Assign staff to an unassigned booking | M2 | S | BK-22 | eslieh | done |
| BK-30 | Daraja client with payment routing (port from `payment-routing-test`) | M3 | M | — | eslieh | done |
| BK-31 | Payments table and `POST /bookings/{id}/payments` | M3 | M | BK-22, BK-30 | eslieh | done |
| BK-32 | M-Pesa callback endpoint | M3 | M | BK-31 | eslieh | done |
| BK-33 | Reconcile stuck payments | M3 | S | BK-25, BK-31 | eslieh | done |
| BK-34 | Deposit and receipt on the booking, per-business deposit totals | M3 | S | BK-32 | | todo |
| BK-35 | Prove payment routing in production | M3 | S | BK-30 | eslieh | in progress |
| BK-36 | Payment destinations fit Daraja limits; merge bank lists | M3 | S | — | | todo |
| BK-40 | Booking confirmation SMS | M4 | S | BK-22 | | todo |
| BK-41 | Push notification tokens (Expo) | M4 | M | — | | todo |
| BK-50 | Concurrency test for double-booking | M5 | S | BK-22, BK-05 | | todo |
| BK-51 | M-Pesa production go-live | M5 | S (weeks of lead time) | BK-01 | | todo |
| BK-52 | Contract hand-off to the app team | M5 | S | BK-22, BK-31 | | todo |
| BK-60 | Cancel within policy | Next | M | BK-22 | | todo |
| BK-61 | Reschedule | Next | M | BK-22, BK-11 | | todo |
| BK-62 | Reminders 24h and 2h before | Next | M | BK-25, BK-40 | | todo |
| BK-63 | Owner and front-desk bookings (list, walk-in, on-behalf, status) | Next | L | BK-22 | | todo |
| BK-64 | No-show marking and fee | Next | S | BK-63 | | todo |

**Start here:** BK-05, BK-21, BK-25, BK-30 and BK-36 have no dependencies and can be picked up now. BK-51 should start on day one because of Safaricom's lead time, and BK-35 follows as soon as it lands: it's the biggest open risk.

---

## M0: Foundations

### BK-01 · Confirm decisions D1–D9
Walk through PLAN.md section 3 and record each outcome there.

- [x] D1 payment routing, D2 sandbox first, D4 unassigned "any available", D5 15-minute grid, D6 10-minute hold (decided 2026-09-26).
- [x] D3 implemented in BK-02.
- [x] D8 implemented in BK-04.
- [x] D7 implemented in BK-22.
- [ ] D9 (technical proposal) accepted or changed once BK-05/BK-50 run it on Postgres.

### BK-02 · Staff identity: `Staff.id` as `staffId`, working days on `Staff`
Today one person is a `TeamInvitation` (roster, `workingDays`, `commissionPercent`) and a `Staff` row (created on accept). Availability and bookings need one id.

- Add `working_days` and `commission_percent` to `Staff`, and `staff_id` to `TeamInvitation` (set when the invite is accepted). Migrate existing values across.
- On accept, copy the invitation's working days and commission to the new `Staff` row. Team-member `PATCH` updates `Staff` once the member has accepted.
- Add `staffId` (null while pending) to the team-member responses.

- [x] A solo owner, an accepted member and the business profile's `staff[]` all expose the same `staffId` (`tests/test_staff_identity.py`).
- [x] Migration `006` moves existing data and downgrades cleanly.

### BK-03 · Staff ↔ services mapping
- New `staff_services` table plus a `Staff.performs_all_services` flag (default true). The flag is explicit so deleting someone's last mapped service can't silently make them "perform everything".
- `GET`/`PUT /businesses/{id}/staff/{staffId}/services` with `{ allServices, serviceIds }` (owner only; ids must belong to the business).
- `allServices` and `serviceIds` on profile staff.

- [x] Deleting a service or category removes its mapping rows.
- [x] Tests cover the "all services" default and "performs nothing" (`tests/test_staff_services.py`).

### BK-04 · Business timezone and booking settings
- Columns on `businesses`: `timezone` (default `Africa/Nairobi`, validated against the IANA zone list), `slot_interval_minutes` (15), `buffer_minutes` (0), `min_lead_minutes` (30), `max_days_ahead` (30).
- `PATCH /businesses/{id}/booking-settings`; the values appear in `GET /businesses/{id}`.

- [x] Invalid values (for example an interval that isn't 5–60 or doesn't divide 60) are rejected (`tests/test_booking_settings.py`).

### BK-05 · Run tests on Postgres in CI
The exclusion constraint (D9) can't be tested on SQLite.

- Add a `postgres:16` service to `.github/workflows/test.yml` and run the suite against it (keep SQLite for fast local runs).
- Add a pytest marker `postgres_only` for tests that need it, skipped on SQLite.

Done so far: `test.yml` runs a `[sqlite, postgres]` matrix; `TEST_DATABASE_URL` switches the suite to Postgres (creating `btree_gist` first); `@pytest.mark.postgres_only` skips on SQLite; a guard refuses any test database host other than localhost/127.0.0.1/postgres, because every test drops all tables.

- [ ] CI runs green on Postgres. Not verified yet: no local Postgres was used, and `test.yml` only runs on pushes and PRs to `sandbox`/`main`, so the first such PR is the check. Expect possible fixes for SQLite-vs-Postgres differences (timezones, JSON, ordering).

## M1: Availability

### BK-10 · Availability engine (pure function)
`app/modules/booking/availability.py`, as specified in PLAN.md section 4.3. No database access: callers pass in hours, staff, bookings, settings and `now`.

- [x] Covers: closed days, staff not working that weekday, a duration that doesn't fit before close, buffer on both sides of a booking, expired `pending_payment` bookings not blocking, the lead time on the current day, `max_days_ahead`, and "any staff" listing every free staff member.
- [x] Unassigned bookings use up capacity through the matching rule in PLAN.md section 4.3, step 5. Tests include two staff with different services, where one unassigned booking blocks a slot for a specific staff request.
- [x] `now` is always passed in; the function never calls the clock itself.
- [x] Runs in under 50 ms for 10 staff × 40 bookings in a day (`tests/test_availability.py`).
- Known limit: unassigned bookings that overlap a new slot but not each other are still counted as needing separate people, so a slot is occasionally hidden (never overbooked).

### BK-11 · Availability endpoints
- `GET /businesses/{id}/availability?date=&serviceIds=&staffId=` and `GET /businesses/{id}/availability/days?from=&days=&serviceIds=&staffId=` (PLAN.md section 5).
- Only published businesses; `serviceIds` must belong to the business; a given `staffId` must perform every chosen service.

- [x] Typed `response_model`s, and errors in `{ error, message }` (`tests/test_availability_api.py`).
- [x] One query per request for the day's bookings, not one per staff member (`AvailabilityService._existing_bookings`, filled in by BK-20).

## M2: Bookings without a deposit

### BK-20 · Booking tables, state machine, exclusion constraint
- Tables `bookings`, `booking_services`, `booking_events` (PLAN.md section 4.1); `staff_id` is nullable (D4). On Postgres, add the `btree_gist` extension and the exclusion constraint on active **assigned** bookings per staff member.
- `transition(booking, to, actor, reason)`, which enforces the diagram in PLAN.md section 4.2 and writes a `booking_events` row.

- [x] An illegal transition raises a clear error; tests cover every allowed and disallowed pair (`tests/test_booking_states.py`).
- [ ] Overlapping active bookings for one staff member are rejected by the database. The constraint is in migration `009` and the model (Postgres only); its test is written but skips until BK-05 runs the suite on Postgres.
- Also added: a short `reference` per booking (`BK` + 6 characters), usable as the M-Pesa till `AccountReference` and on receipts.

### BK-21 · Price and deposit calculator
A pure function: `(services with quantities, policies) -> { total, deposit | None }`.

- Fixed: `min(amount, total)`. Percent: `ceil(total × percent / 100)` in whole KES. `selected_services`: the deposit is based only on those lines, and there's no deposit when none of them are in the cart.
- [x] Table-driven tests for every combination above, plus the "required = false" case (`tests/test_pricing.py`, `app/modules/booking/pricing.py`).

### BK-22 · `POST /bookings`
- Request and response as in PLAN.md section 5. The server recomputes prices (D7) and takes a per-business, per-day advisory lock (`pg_advisory_xact_lock`), then checks the slot with the availability engine and inserts, all in one transaction. "Any available" is stored unassigned (D4). If the constraint fires, map it to `409 slot_unavailable`.
- No deposit gives `confirmed`. A deposit gives `pending_payment` with `expires_at = now + 10 min` (D6).
- Snapshot the customer's name and phone from their account.

- [ ] Two parallel requests for the same slot give one 201 and one 409. Sequential double-booking is tested; the parallel case needs Postgres (BK-50).
- [x] A changed `expectedTotal` returns 409 `price_changed` with the current total.
- [x] The booking can't be for your own business's staff when you're the owner (400 `own_business`).
- Also: expired holds in that business are released on the spot before the slot check (so they can't trip the constraint), and times off the slot grid or outside hours return 409 (`tests/test_create_booking.py`).

### BK-23 · `GET /bookings/{id}` and `GET /me/bookings`
- The customer who booked, and the business's owner or staff, can read a booking; everyone else gets 404.
- `filter=upcoming` (`starts_at` in the future and active), `past`, `cancelled`; newest first, paginated with `?cursor=`.

- [x] The response includes `depositPayment` (`null` until BK-31 fills it).
- Also: reading a booking releases expired holds on the spot, so an unpaid booking never shows as still pending (`tests/test_read_bookings.py`).

### BK-24 · Expire unpaid holds
- `booking.expire_holds`, run every minute by `--beat`: `pending_payment` bookings past `expires_at` move to `cancelled` with `cancelReason: "payment_expired"`.

- [x] Idempotent and safe to run on two workers at once (`SELECT … FOR UPDATE SKIP LOCKED` on Postgres; `app/modules/booking/tasks.py`, `tests/test_expire_holds.py`).
- Moved to BK-32: a payment that succeeds after expiry.

### BK-25 · Periodic jobs in the worker (`--beat`)
- `python worker.py --beat` runs a small scheduler that enqueues registered periodic tasks (`@periodic("booking.expire_holds", every=60)`). It doesn't run them itself; normal workers do.
- Add a `beat` service to the three compose files (one replica).

- [x] Documented in `app/workers/registry.py` next to `@task`; `beat` service in all three compose files.
- [x] Stopping `beat` doesn't stop other tasks from running: beat only enqueues; workers run the jobs (`app/workers/beat.py`, `tests/test_beat.py`).

### BK-26 · Assign staff to an unassigned booking
`PATCH /bookings/{id}/staff` `{ staffId }` for the owner or front desk (D4). The chosen staff member must perform every service on the booking and be free, checked with the availability engine under the same advisory lock as BK-22.

- [x] Reassigning an already-assigned booking works the same way, and writes a `booking_events` row.
- [x] The customer sees the assigned `staffName` on their booking afterwards (`tests/test_assign_staff.py`).
- Note: the owner has no list of the business's bookings yet to find unassigned ones; that's `GET /businesses/{id}/bookings` in BK-63.

## M3: Deposits (sandbox)

### BK-30 · Daraja client with payment routing
`app/modules/payments/mpesa.py`, behind a small `PaymentProvider` interface so an aggregator could replace it. Port the routing from `payment-routing-test/mpesa.go` (D1): given a business's payment destination, build `TransactionType`, `PartyB` and `AccountReference`.

| Destination | `TransactionType` | `PartyB` | `AccountReference` |
| --- | --- | --- | --- |
| `mpesa_till` | `CustomerBuyGoodsOnline` | till number | booking reference |
| `mpesa_paybill` | `CustomerPayBillOnline` | paybill number | the business's paybill account |
| `bank_account` | `CustomerPayBillOnline` | bank's paybill shortcode | bank account number |

- Settings: `MPESA_ENV` (`sandbox` | `production`), `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_SHORTCODE`, `MPESA_PASSKEY`, `MPESA_CALLBACK_URL` (public URL of the callback route), `MPESA_CALLBACK_SECRET` (appended to it as the last path segment). Add them to `.env.example`, compose and the deploy workflows. `INITIATOR_NAME` and `SECURITY_CREDENTIAL` from the prototype's `.env` are only needed later, for reversals.
- Cache the OAuth token until shortly before it expires (the prototype fetches one per request). Normalise phones with `app/core/phone_utils.py` to `2547…`.

- [ ] Unit tests against recorded sandbox responses (no network in CI).
- [ ] A manual script shows a real sandbox STK push reaching a test phone.

### BK-31 · Payments table and `POST /bookings/{id}/payments`
- The `payments` table (PLAN.md section 4.1). The endpoint accepts `{ phone }`, rejects bookings that aren't `pending_payment` or have expired, calls STK push, stores the attempt and returns `202 { paymentId, status: "pending" }`.
- Limit retries to 5 attempts per booking and one pending attempt at a time.

- [x] A Daraja error returns 502 `payment_provider_error`, and the booking stays retryable (`tests/test_payments.py`).
- Also: the payment row is written before the push (a crash mid-call still leaves a trace) and records the routing used; the booking's `depositPayment` shows the latest attempt; tests blank all M-Pesa settings so they can never send a real push.

### BK-32 · M-Pesa callback endpoint
`POST /payments/mpesa/callback/{secret}`: public, but the secret in the path must match.

*Started in BK-30:* the route exists in `app/modules/payments/router.py` and verifies the secret, parses the payload and logs it. What's left is everything below that touches the database.

- Look up by `CheckoutRequestID`. Ignore duplicates for already-settled payments (still answer 200).
- Map result codes as in PLAN.md section 4.4. On success, store the receipt, set the payment to `succeeded`, move the booking to `confirmed` through `transition()` and enqueue the confirmation SMS.
- Always answer Daraja with `{ "ResultCode": 0, "ResultDesc": "Accepted" }` once the callback is stored.
- A payment that succeeds after the hold expired (from BK-24): confirm the booking if the slot is still free; otherwise flag it for a refund (log and a `booking_events` entry for now).

- [x] Replaying the same callback twice has no extra effect.
- [x] A wrong secret returns 404 and changes nothing (`tests/test_mpesa_callback.py`, `tests/test_mpesa.py`).
- Late payment: reinstated through a guarded `cancelled → confirmed` transition (only after `payment_expired`, under the booking lock); if the slot was taken, the booking stays cancelled with a `late_payment_refund_due` event and an error log. The shared `apply_result()` in `app/modules/payments/results.py` is what BK-33 reuses.

### BK-33 · Reconcile stuck payments
- `payments.reconcile_pending`, run every minute: STK Query for payments pending more than 60 seconds, applying the same result handling as the callback. After 3 minutes with no answer, mark the attempt `timeout`.

- [x] Shares one code path with the callback for applying a result (`apply_result()`; `app/modules/payments/tasks.py`, `tests/test_reconcile.py`).
- Also: a payment that never got a CheckoutRequestID (process died mid-push) is marked failed after 3 minutes; an unconfigured provider is logged, not raised.

### BK-34 · Deposit and receipt on the booking, per-business deposit totals
With payment routing (D1) the money is already in the business's account, so there's nothing to settle. Record what was paid, and where it went, for the owner's view.

- [ ] The booking shows `depositPaid`, the M-Pesa receipt and the destination it was routed to.
- [ ] Owner earnings (a later phase) can sum deposits per business and date range from `payments`.

### BK-35 · Prove payment routing in production
The sandbox doesn't validate `PartyB`, so routing to a till or paybill Trimmy doesn't own is unproven. As soon as production credentials exist (BK-51), push KES 1 to one real third-party till, one paybill and one bank.

- [x] Paybill: a production STK push routed to a third-party paybill succeeded (2026-09-26, `scripts/mpesa_stk.py`).
- [ ] Till (`CustomerBuyGoodsOnline`) and bank (bank paybill + account) each succeed, or the failure (result code and description) is recorded here and D1 is revisited with the aggregator fallback.
- [ ] Ask Safaricom during go-live whether this routing needs special approval on Trimmy's shortcode.

### BK-36 · Payment destinations fit Daraja limits; merge bank lists
Daraja caps `AccountReference` at 12 characters, but the server accepts bank and paybill account numbers up to 34 and 50.

- [ ] Payment-destination validation limits `paybillAccountNumber` and `bankAccountNumber` to 12 characters (letters, digits, `-_/.` and spaces, as in the prototype), with a clear message. Check existing saved destinations and list any that no longer pass.
- [ ] Merge the bank lists: the prototype has Bank of Africa, ABC Bank and Bank of Baroda, which `app/modules/business/payment_options.py` doesn't. Verify every shortcode before it goes live.

## M4: Notifications

### BK-40 · Booking confirmation SMS
- `booking.send_confirmation` on the `sms` queue, using the existing SMS gateway (`app/modules/auth/sms.py`). Content: business, service, staff, local date and time, deposit paid.
- Sent when a booking becomes `confirmed`, from either path.

- [ ] Idempotent: a retried task doesn't send twice (record `confirmation_sent_at`).

### BK-41 · Push notification tokens (Expo)
- `POST /me/push-tokens` to register the device's Expo push token, and a `notifications.send_push` task. The first use is the booking confirmation.

- [ ] Invalid tokens reported by Expo are deleted.

## M5: Go-live

### BK-50 · Concurrency test for double-booking
- [ ] A Postgres test fires 20 concurrent `POST /bookings` for the same staff and slot: exactly one succeeds and the rest get 409.
- [ ] The same test with "Any available" and 3 free staff: exactly 3 unassigned bookings succeed, and the 4th gets 409.

### BK-51 · M-Pesa production go-live
Start on day one; Safaricom's approval takes weeks.

- [ ] Go-live application submitted for the paybill decided in D1.
- [ ] Production passkey and credentials stored as GitHub environment secrets.
- [ ] Production callback URL (https) registered, and Safaricom's IP ranges allowed at the proxy.

### BK-52 · Contract hand-off to the app team
- [ ] `mobile/reference/api/booking.json` updated with PLAN.md section 5, marked `live` per endpoint as they ship.
- [ ] A walkthrough of the two-step payment and polling with the app team.

## Next phase

### BK-60 · Cancel within policy
`POST /bookings/{id}/cancel`, with a preview (`GET /bookings/{id}/cancel-preview`) showing the refund or fee from `cancellation_free_hours` and `late_fee_percent`. With payment routing, the deposit sits in the business's account, so Trimmy can't refund it itself. Decide first: the business refunds by hand (and marks it refunded), or deposits become credit for the next booking.

### BK-61 · Reschedule
`POST /bookings/{id}/reschedule` `{ date, time }`, which reuses availability and the conflict check atomically and keeps any paid deposit.

### BK-62 · Reminders 24h and 2h before
Scheduled per booking by `--beat` (scan for confirmed bookings entering each window), idempotent per booking and window. Respect the customer's notification preferences once those exist.

### BK-63 · Owner and front-desk bookings
The `fulfillment.json` endpoints: `GET /businesses/{id}/bookings` (calendar), `POST /bookings/walk-in`, `POST /bookings/scheduled`, `PATCH /bookings/{id}/status` through `transition()`.

### BK-64 · No-show marking and fee
The business marks `no_show` after the start time; the fee comes from `no_show_fee_percent`, and is recorded for now.
