# Frontend changes from the booking work

What the app needs to integrate or update, newest first. Each entry says which file in `mobile/reference/api/` it belongs to, and gives the endpoint in the same JSON shape as those files so it can be pasted in. Task ids link back to [BACKLOG.md](BACKLOG.md).

---

## 2026-09-30 · Day summary and submit to owner (F4)

**Now live → `fulfillment.json#get-day-summary` and `#submit-day-summary`**, owner or front desk.

- `GET /businesses/{businessId}/day-summary?date=2026-10-05` → the contract's shape, plus `timezone`, and `status` on each `unpaid` row:
  - `appointments` = booked ahead (online or on the customer's behalf) and not cancelled; `walkIns` = recorded as walk-ins; `completed`, `noShows`, `cancelled` = that day's visits by status.
  - `collected` = the **drawer**: payments that succeeded on that day, whichever day the visit was. `cash` / `mpesa` by method; `depositsCollected` / `balancePayments` (checkout charges) by kind. The two splits cover the same money, so don't add them together.
  - `refundsPaid` = refunds marked paid back that day.
  - `unpaid` = that day's visits `in_progress` or `completed` and not yet paid, with `balanceDue`.
  - `submission` = `{ submittedAt, submittedBy (name), notes }`, nulls until submitted.
- `POST /businesses/{businessId}/day-summary/{date}/submit` `{ "notes": "KSh 200 short due to change" }` → the same DaySummary with `submission` filled in. Resubmitting replaces the note. A day in the future is `400`. When the front desk submits, the owner gets an SMS (to their account phone) and a push `{ type: "day_summary_submitted", businessId, date }`.
- Staff `403`; anyone else `404`.

---

## 2026-09-30 · Push notifications (BK-41)

**Now live → `auth.json#register-push-token`**, as specified, plus the logout call.

- `POST /me/push-tokens` `{ "token": "ExponentPushToken[…]", "platform": "ios" | "android" }` → `{ "ok": true }`. Any signed-in user; idempotent. If another account registers the same token (someone else signs in on that phone), it moves to them. `400 invalid_token` if it isn't an Expo token.
- `DELETE /me/push-tokens/{token}` → `204`, on logout. Idempotent; only removes your own.
- Tokens Expo reports as `DeviceNotRegistered` are deleted automatically.

**What gets pushed** (every notification's `data` has `type` plus the ids to open):

| `data.type` | To | When | `data` |
| --- | --- | --- | --- |
| `booking_confirmed` | customer | booking confirmed (no deposit / deposit paid / booked on their behalf / rescheduled) | `bookingId`, `businessId` |
| `booking_cancelled_by_business` | customer | the business cancelled it | `bookingId`, `businessId` |
| `booking_reminder_24h`, `booking_reminder_2h` | customer | the BK-62 reminders | `bookingId`, `businessId` |
| `booking_assigned` | staff | a booking made with them, or (re)assigned to them | `bookingId`, `businessId` |
| `payout_responded` | staff | their payout approved or declined | `payoutId`, `businessId` |

The owner's "day summary submitted" push comes with the day-summary endpoints (next).

**App code to update**

- After login (all roles): get the Expo token and `POST /me/push-tokens`; on logout `DELETE /me/push-tokens/{token}`.
- Notification tap: route on `data.type` → Appointment Detail (`bookingId`) or Payout Detail (`payoutId`).

---

## 2026-09-30 · Staff see their own services (P1)

**Now live → `team.json#get-my-staff-services`**, as specified: `GET /me/staff-services?businessId=uuid` for the staff member or front desk themselves.

```json
{ "staffId": "uuid", "allServices": false,
  "services": [{ "serviceId": "uuid", "name": "Classic Haircut", "durationMinutes": 45, "price": { "amount": 800, "currency": "KES" } }] }
```

- `allServices: true` → `services` lists every service the business offers (so "My services" always has something concrete to show).
- `404 not_found` "You don't work at this business" if the caller has no staff record there.

---

## 2026-09-30 · Charge a completed-but-unpaid visit (P1)

**Answers → `fulfillment.json#charge-booking` issue (P1):** option (a). `charge-booking` now accepts a booking that is `in_progress` **or** `completed` and not yet `paid`. Staff can keep finishing their own appointments with `update-booking-status`; the owner / front desk charges it afterwards, and it then counts toward earnings and the staff member's commission. Cancelled, no-show and pending bookings still get `409 not_in_progress` (message now "Only an in-progress or completed appointment can be charged").

**App code to update:** offer "Charge customer" on a completed booking whenever `paymentStatus !== "paid"` (i.e. `balanceDue.amount > 0`).

---

## 2026-09-30 · No-show fee (BK-64)

**Why:** when the business marks a booking `no_show`, the owner and the customer both see what the no-show cost.

**How it's worked out:** the business's `noShow.feePercent` (default 100%) of the booking total. There's no card on file, so only a paid deposit can be kept: the fee comes out of the deposit, capped at it. Anything left of the deposit is refunded (a `refund` owed, as in BK-60), and the part of the fee the deposit didn't cover is shown as `uncollected`.

**Changed response → `Booking`**: a new `noShowFee`, null unless the booking was marked no-show.

```json
{
  "noShowFee": {
    "percent": 100,
    "fee": { "amount": 800, "currency": "KES" },
    "kept": { "amount": 300, "currency": "KES" },
    "uncollected": { "amount": 500, "currency": "KES" }
  }
}
```

- 25% fee on an 800 booking with a 300 deposit: `fee` 200, `kept` 200, `uncollected` 0, and a `refund` of 100 due.
- No deposit: `kept` 0, `uncollected` = the whole fee.

**Changed → `fulfillment.json#get-earnings-summary`**: `money.noShowFees` = fees kept from no-shows' deposits in the range (not in `totalRevenue`).

**App code to update**

- Appointment Detail (owner and customer) for a `no_show`: "No-show fee: KES 300 kept from your deposit" (+ "KES 500 not collected" for the owner), and the refund line when `refund` is set.
- The customer's "No-show notice" (C5) can use `noShowFee.percent` to explain the policy.

---

## 2026-09-30 · Reminders by SMS (BK-62)

**Why:** C5 "reminders before my appointment". No API change; reminders go by SMS now, and push can be added once push tokens (BK-41) exist.

- **Day before:** once the booking is 24h away, if it was booked at least a day ahead. e.g. `Trimmy reminder: Glow Beauty, Classic Haircut with Wanjiku, tomorrow 14:30. Ref BK7F3AQ9. To change it, open the Trimmy app.`
- **2 hours before:** with a map link when the business has coordinates. e.g. `Trimmy: see you at 14:30 at Glow Beauty (Classic Haircut). Ref BK7F3AQ9. Directions: https://maps.google.com/?q=-1.29,36.78`
- Only for `confirmed` bookings with a `customerPhone`; each once per booking. Rescheduling resets them for the new time; cancelled and unpaid bookings get none.
- Not built yet: the customer's reminder preferences (C5 "configurable", Notification Preferences). Every confirmed customer gets both for now.

---

## 2026-09-30 · Reschedule (BK-61)

**Why:** C3 "manage my appointments": move an appointment instead of cancelling and rebooking.

**New endpoint → `booking.json`**

```json
{
  "id": "reschedule-booking",
  "status": "live",
  "screen": "Reschedule (modal): reuses Select Date & Time; owner calendar drag / edit time",
  "method": "POST",
  "path": "/bookings/{bookingId}/reschedule",
  "auth": "the customer who booked, or the business's owner / front desk",
  "description": "Moves a pending_payment or confirmed booking that hasn't started. Customer: to a slot from get-availability (the booking's own old slot doesn't block it), and only while cancelling would still be free (before cancel-preview's freeUntil); later, 409 reschedule_window_passed. Owner / front desk: any time inside working hours and in the future, off the slot grid allowed. staffId is optional (omit to keep the current staff). Services, price, deposit, paymentStatus, reference and an unpaid hold's expiresAt all stay the same. A confirmed booking's customer gets a new SMS with the new time.",
  "request": { "date": "2026-10-05", "time": "15:30", "staffId": "uuid (optional)" },
  "response": "Booking (new date, time, startsAt, staff)",
  "errors": [
    { "status": 409, "code": "slot_unavailable", "message": "The selected time is no longer available" },
    { "status": 409, "code": "reschedule_window_passed", "message": "It's too late to reschedule in the app; please contact the business" },
    { "status": 409, "code": "cannot_reschedule", "message": "A cancelled booking can't be rescheduled / This appointment has already started" },
    { "status": 400, "code": "validation_error", "message": "time is not within the business's working hours for that date (owner / front desk)" },
    { "status": 404, "code": "not_found", "message": "No booking found for this id" }
  ]
}
```

**App code to update**

- Appointment Detail → Reschedule: open Select Date & Time with the booking's services (and staff), then call this. Use `cancel-preview`'s `freeUntil` to decide whether to show Reschedule at all, or show "contact the business" after it.
- On `409 slot_unavailable`, refresh the slots and let them pick again.

---

## 2026-09-30 · staffId in GET /me/businesses

**Why:** staff and front desk can get into their app after accepting an invite (no more testing shortcut): the app needs their `staffId` for My Chair, earnings, payouts and status changes.

**Changed response → `auth.json#my-businesses`** (`GET /me/businesses`): each entry gains `staffId`.

```json
{ "businesses": [
  { "businessId": "uuid", "name": "Fade Studio", "status": "published", "role": "owner", "staffId": "uuid | null" },
  { "businessId": "uuid", "name": "Cuts Co", "status": "published", "role": "front_desk | staff", "staffId": "uuid" }
] }
```

- `staff` / `front_desk`: always set, the same id as the roster's `staffId` and the profile's `staff[].staffId`.
- `owner`: set when the owner takes appointments themselves (a solo business, once published); null for a team-mode owner who doesn't.

**App code to update**

- Post-login routing: `role` picks the app (owner / front desk / staff); keep `staffId` for the session and use it for `GET /businesses/{id}/bookings` (staff see their own), `/me/payouts/balance`, earnings and status updates.

---

## 2026-09-30 · Earnings summary (get-earnings-summary)

**Why:** Earnings, Team Member Detail and the staff's own Earnings screens from real paid bookings, replacing `src/utils/earnings.ts`. The server follows that file rule for rule (buckets, labels, sorting, what counts), so the charts look the same.

**Now live → `fulfillment.json#get-earnings-summary`**

- `GET /businesses/{businessId}/earnings?from=2026-10-05&to=2026-10-11[&staffId=uuid][&transactionsLimit=10]`
- Auth: the owner (whole business, or one person with `staffId`); staff always get their own numbers (`staffId` optional, any other is 403); front desk 403.
- `from`/`to` are business-local days, inclusive, at most 366 days apart.
- Every contract field, unchanged: `totalRevenue, completed, noShow, cancelled, avgTicket, buckets[{label, amount}], paymentMethods[{method, amount, count}], services[{serviceId, name, amount, bookingsCount}], transactions[{bookingId, serviceId, date, time, customerName, serviceName, amount, paymentMethod}]`.
  - Buckets: 3-hour bars for one day ("9 AM"), daily up to 16 days ("Mon"), weekly up to 70 ("Oct 5"), monthly beyond ("Oct"), like the app.
  - `services` lists every current service (0 rows included), plus any since-deleted service that earned money in the range (`serviceId: null`), sorted by amount then name.
  - `transactions`: one row per service performed, newest first; `transactionsLimit` returns the newest N (the app's "cap at 10"), and `transactionsTotal` says how many there are.

**Extras in the response** (use or ignore):

```json
{
  "currency": "KES", "from": "2026-10-05", "to": "2026-10-11", "timezone": "Africa/Nairobi",
  "staffId": null, "granularity": "day", "paidBookings": 14, "commission": 4960, "transactionsTotal": 18,
  "buckets": [{ "label": "Mon", "amount": 1600, "start": "2026-10-05T00:00:00+03:00", "end": "2026-10-06T00:00:00+03:00", "bookingsCount": 2 }],
  "staff": [{ "staffId": "uuid | null", "name": "Wanjiku | Unassigned", "amount": 8000, "bookingsCount": 10, "commission": 3200 }],
  "money": {
    "depositsCollected": 1500, "cancellationFees": 300, "refundsDue": 500, "refundsPaid": 0,
    "collected": [{ "method": "mpesa", "kind": "deposit | charge", "amount": 9600, "count": 11 }]
  },
  "previous": { "from": "2026-09-28", "to": "2026-10-04", "totalRevenue": 10000, "completed": 12, "avgTicket": 833, "revenueChangePercent": 24.0 }
}
```

- `staff` is the per-person breakdown on the whole-business view (empty when scoped to one person). `commission` is the scoped person's, or everyone's.
- `previous` is the same-length period just before `from`, for "+24% vs last week"; `revenueChangePercent` is null when that period earned nothing.
- `money` covers what isn't sales: deposits taken, cancellation fees kept, refunds still owed or paid back, and every successful payment by method and kind.

**App code to update**

- Replace `getEarningsBuckets` / `getPaymentMethodBreakdown` / `getServicesBreakdown` / `getTransactionLines` / `getAppointmentStats` with one call per screen and range. Presets: send the same from/to the app computes today (Today = one day, Week = last 7 days, and so on).
- Team Member Detail: `staffId=<member's staffId>`. Staff's own Earnings: no `staffId` needed.

---

## 2026-09-30 · Payouts (request-payout, list-business-payouts, respond-to-payout) + balance

**Why:** staff wallets (S3) and the owner's payout queue (O4), from real paid bookings. Trimmy never moves this money: "approve" records that the owner paid the staff member themselves.

**Changed contract → keyed by `staffId`, not `invitationId`** (BK-02). `invitationId` is gone from requests and responses.

```json
{
  "id": "request-payout",
  "status": "live",
  "method": "POST",
  "path": "/payouts",
  "auth": "staff (for themselves only)",
  "description": "staffId is optional and defaults to the caller's own staff record at businessId; any other staffId is 403. amount must be ≤ the available balance, checked on the server. Always comes back pending.",
  "request": { "businessId": "uuid", "staffId": "uuid (optional)", "amount": 320 },
  "response": { "payoutId": "uuid", "businessId": "uuid", "staffId": "uuid", "staffName": "Wanjiku", "amount": 320, "currency": "KES", "status": "pending", "requestedAt": "…", "respondedAt": null, "rejectionReason": null },
  "errors": [
    { "status": 409, "code": "insufficient_balance", "message": "Amount exceeds available wallet balance (KES 120)" },
    { "status": 403, "code": "forbidden", "message": "You can only request payouts for yourself" },
    { "status": 404, "code": "not_found", "message": "You don't work at this business" }
  ]
},
{
  "id": "list-business-payouts",
  "status": "live",
  "method": "GET",
  "path": "/businesses/{businessId}/payouts?status=pending&staffId=uuid",
  "auth": "owner (everyone's); staff (their own)",
  "response": { "payouts": ["Payout"] }
},
{
  "id": "respond-to-payout",
  "status": "live",
  "method": "POST",
  "path": "/payouts/{payoutId}/respond",
  "auth": "owner",
  "request": { "action": "approve | reject", "rejectionReason": "only kept for reject" },
  "response": "Payout (status paid | rejected, respondedAt set)",
  "errors": [{ "status": 409, "code": "already_responded", "message": "This payout has already been approved or rejected" }]
},
{
  "id": "get-payout-balance",
  "status": "live (new)",
  "screen": "Staff Earnings (wallet); owner Member Detail",
  "method": "GET",
  "path": "/me/payouts/balance?businessId=uuid  |  /businesses/{businessId}/staff/{staffId}/payout-balance",
  "auth": "/me: the staff member; /businesses/…: the owner, or that staff member",
  "description": "earned = commission on paid bookings; available = earned - pending - paid. Commission is fixed on each booking when it's paid (total x the staff member's commissionPercent at that moment), so changing someone's rate only affects later bookings. Unassigned bookings earn no commission.",
  "response": { "businessId": "uuid", "staffId": "uuid", "currency": "KES", "earned": 320, "pending": 200, "paid": 0, "available": 120, "commissionPercent": 40 }
}
```

**App code to update**

- `types/payout.ts`: `invitationId` → `staffId`, plus `staffName`.
- Staff wallet: show `available` from the balance endpoint instead of computing it client-side; the server re-checks every request anyway.
- Owner Team tab: `list-business-payouts?status=pending` for the queue; Member Detail can show that person's balance.
- Earnings only count once bookings are paid through charge-booking (above), so flip `USE_MOCK_CHARGE` off first.

---

## 2026-09-30 · Customers (save-customer, list-customers)

**Why:** the Customers screen and New appointment's customer picker, from real data. Flip `USE_MOCK_CUSTOMERS` off.

**Now live → `fulfillment.json#save-customer` and `#list-customers`** (owner or front desk)

- The list fills itself: every booking with a phone (customer bookings, walk-ins, on-behalf) adds or refreshes that person, deduplicated by phone in any format (`0722 000 111` = `+254722000111`). Existing bookings were backfilled. The app doesn't need to call save-customer after a booking.
- `GET /businesses/{businessId}/customers?query=&limit=100&offset=0` → `{ customers: [Customer], total }`, A–Z. `query` searches name, email and phone digits. limit 1–500.
- `POST /businesses/{businessId}/customers` `{ customerId?, name, phone?, email? }` → `Customer`. With `customerId`: updates it. Without: updates whoever already has that phone, otherwise creates. Customers without a phone are allowed (not deduplicated).
- `Customer` = `{ customerId, businessId, name, phone (E.164), email, createdAt, bookingsCount, lastBookingAt }`. The last two are new: bookings with that phone, cancelled ones excluded.
- Errors: `400 invalid_phone`, `409 phone_taken` (updating to a phone another customer has), `404 not_found`, `403 forbidden` (staff).

**Behaviour change → `create-walk-in-booking` / `create-scheduled-booking`**: `customerPhone` is now normalised to E.164 and must be a valid number (`400 invalid_phone` otherwise).

**App code to update**

- `useCustomersStore`: load from `list-customers` instead of session-only data; the client-side dedupe-by-phone before `save-customer` is no longer needed (the server does it).

---

## 2026-09-30 · Charge at checkout (charge-booking)

**Why:** the last step of a visit. Until now no booking ever became `paid` on the server, so earnings, commission and payouts had nothing real to count. Flip `USE_MOCK_CHARGE` off.

**Now live → `fulfillment.json#charge-booking`**, exactly as the contract describes, with these specifics:

- `POST /bookings/{bookingId}/charge` `{ "method": "mpesa" | "cash", "phone"?: "0712345678" }`, owner or front desk, for an `in_progress` booking.
- Amount = `balanceDue`: `totalAmount` minus any paid deposit.
- **cash** → `200` with the Booking: `status: completed`, `paymentStatus: paid`, `paymentMethod: cash`, `paymentReference: null`.
- **mpesa** → `202` `{ paymentId, bookingId, status: "pending", amount, currency, phone, customerMessage, expiresAt: null }`, routed to the business's own till/paybill/bank. Poll `GET /bookings/{bookingId}` every 3s: success = `status: completed` + `paymentStatus: paid` + `paymentMethod: mpesa` + `paymentReference` (receipt); failure = `chargePayment.status` `failed`/`timeout` with a `failureReason`, and the customer can be re-prompted.
- Nothing left to pay (the deposit covered it) → `200`, completed, whatever `method` was sent.
- Errors as in the contract: `400 invalid_phone`, `409 not_in_progress | already_paid | payment_in_progress | business_cannot_take_payments`, `429 too_many_attempts`, `502 payment_provider_error`, `503 payments_unavailable`, `403 forbidden`.

**Changed response → `Booking`**: `balanceDue` (`{ amount, currency }`, 0 once paid) and `chargePayment` (`{ paymentId, status, failureReason }` or null), same shape as `depositPayment`. `paymentMethod` / `paymentReference` now come from the checkout charge once it succeeds (a paid deposit's receipt before that).

**Edge case:** if an M-Pesa charge lands after the booking was already paid another way (e.g. the customer paid cash while the prompt was open), it's recorded as a refund owed (`refund.status: due`), like BK-60.

---

## 2026-09-27 · Cancel within policy, refunds by hand (BK-60)

**Why:** C3 "cancel within policy". Deposits sit in the business's own account, so refunds are paid back by the business by hand (decision D10); the app shows what's owed and when it's done.

**New endpoints → `booking.json`**

```json
{
  "id": "cancel-preview",
  "status": "live",
  "screen": "Cancel Confirmation (modal): refund/fee breakdown",
  "method": "GET",
  "path": "/bookings/{bookingId}/cancel-preview",
  "auth": "the customer who booked",
  "description": "What cancelling right now would cost. Free before freeUntil (the business's freeCancellationHours before the start): the whole paid deposit is refunded. Later: lateFeePercent of the booking total is kept, capped at the deposit, and the rest refunded. No deposit paid: free. Show message as-is.",
  "response": {
    "bookingId": "uuid", "canCancel": true, "reasonBlocked": null,
    "freeUntil": "2026-10-04T14:30:00Z", "withinFreeWindow": true,
    "depositPaid": { "amount": 300, "currency": "KES" },
    "fee": { "amount": 0, "currency": "KES" },
    "refund": { "amount": 300, "currency": "KES" },
    "lateFeePercent": 50,
    "message": "Free cancellation: your KES 300 deposit will be refunded by the business."
  }
},
{
  "id": "cancel-booking",
  "status": "live",
  "screen": "Cancel Confirmation (modal): confirm",
  "method": "POST",
  "path": "/bookings/{bookingId}/cancel",
  "auth": "the customer who booked",
  "description": "Cancels with the same outcome the preview showed. pending_payment and confirmed bookings only, before the start time. reason is the optional note from the reason picker.",
  "request": { "reason": "Something came up" },
  "response": "Booking (status cancelled, cancelReason customer, plus cancellationFee and refund below)",
  "errors": [{ "status": 409, "code": "cannot_cancel", "message": "This appointment has already started" }]
},
{
  "id": "mark-refunded",
  "status": "live",
  "screen": "Owner / front desk: Refunds owed → Mark refunded",
  "method": "POST",
  "path": "/bookings/{bookingId}/refund",
  "auth": "owner or front desk",
  "description": "Records that the business has sent a refund it owed back to the customer. reference is optional (e.g. the M-Pesa code of the refund).",
  "request": { "reference": "SJK4R2T8QX" },
  "response": "Booking (refund.status refunded)",
  "errors": [{ "status": 409, "code": "no_refund_due", "message": "There's no refund owed on this booking" }]
}
```

**Changed response → `Booking`**: two new fields, both null unless a cancellation involved money.

```json
{
  "cancellationFee": { "amount": 300, "currency": "KES" },
  "refund": { "amount": { "amount": 100, "currency": "KES" }, "status": "due | refunded", "refundedAt": null, "reference": null }
}
```

**Changed → `fulfillment.json#list-business-bookings`**: `?refund=due` lists every booking with a refund owed (no date needed), for a "Refunds owed" screen. `update-booking-status` with `cancelled` now makes the whole paid deposit refundable (the business cancelled).

**App code to update**

- Appointment Detail → Cancel: show `cancel-preview` (its `message`, `fee`, `refund`), then `cancel-booking`. Disable Cancel when `canCancel` is false and show `reasonBlocked`.
- Show `refund` on cancelled bookings: "KES 300 refund pending from the business" while `due`, "Refunded" (with `reference`) once `refunded`.
- Owner app: a "Refunds owed" list (`?refund=due`) with a "Mark refunded" action.
- A late payment into a taken slot (BK-32) also shows up as a refund owed.

---

## 2026-09-27 · Confirmation SMS (BK-40)

**Why:** customers now get a text when their booking is confirmed. No API change.

- Sent once per booking, to the booking's `customerPhone`, when it becomes `confirmed`: straight away with no deposit, when the deposit is paid, or when the owner books on the customer's behalf. Not for walk-ins.
- Example: `Trimmy: BK7F3AQ9 confirmed. Glow Beauty Lounge, Classic Haircut with Wanjiku, Mon 5 Oct 14:30. Deposit KES 300 paid.`
- Booking Confirmed can say "We've sent you an SMS" when the booking has a `customerPhone`.

---

## 2026-09-27 · Business-side bookings: calendar, walk-ins, on-behalf, status (BK-63)

**Why:** replaces the `USE_MOCK_FULFILLMENT` mocks for Today, Calendar, New appointment and Appointment Detail's status stepper. Bookings made by customers, walk-ins and the owner all appear together.

**Replaces → `fulfillment.json`** (`list-business-bookings`, `create-walk-in-booking`, `create-scheduled-booking`, `update-booking-status`)

```json
{
  "id": "list-business-bookings",
  "status": "live",
  "screen": "Today / Calendar / Earnings (business-owner app); Front Desk calendar; Staff My Chair",
  "method": "GET",
  "path": "/businesses/{businessId}/bookings?date=2026-09-25 | ?from=2026-09-01&to=2026-09-30 [&status=confirmed,in_progress] [&staffId=uuid] [&unassigned=true]",
  "auth": "owner or front desk (all bookings); staff (only their own)",
  "description": "Soonest first. date = one day; from/to = an inclusive range of at most 62 days (one is required). status is comma-separated. unassigned=true lists 'Any available' bookings waiting for someone (see assign-booking-staff).",
  "response": { "bookings": ["Booking"] },
  "errors": [
    { "status": 400, "code": "validation_error", "message": "Pass date, or from and to" },
    { "status": 404, "code": "not_found", "message": "No business found for this id" }
  ]
},
{
  "id": "create-walk-in-booking",
  "status": "live",
  "method": "POST",
  "path": "/bookings/walk-in",
  "auth": "owner or front desk",
  "description": "The customer is here now: the server stamps the current time, status starts in_progress, no deposit. Prices come from the server. customerName defaults to 'Walk-in customer'. staffId optional (null = unassigned); it must be free right now, else 409.",
  "request": {
    "businessId": "uuid", "customerName": "Baraka", "customerPhone": "+254733000111", "customerEmail": null,
    "services": [{ "serviceId": "uuid", "quantity": 1 }], "staffId": "uuid | null", "notes": null
  },
  "response": "Booking (status in_progress, source walk_in)",
  "errors": [
    { "status": 409, "code": "slot_unavailable", "message": "This time is no longer available" },
    { "status": 403, "code": "forbidden", "message": "Only the owner or front desk can do this" }
  ]
},
{
  "id": "create-scheduled-booking",
  "status": "live",
  "method": "POST",
  "path": "/bookings/scheduled",
  "auth": "owner or front desk",
  "description": "Book on a customer's behalf: same body as the walk-in plus date and time. Starts confirmed, no deposit. Must be in the future and inside working hours, but may be off the customer slot grid (e.g. 14:05) and needs no lead time.",
  "request": { "…": "as create-walk-in-booking", "date": "2026-09-25", "time": "14:00" },
  "response": "Booking (status confirmed, source walk_in)",
  "errors": [
    { "status": 400, "code": "validation_error", "message": "time is not within the business's working hours for that date" },
    { "status": 400, "code": "validation_error", "message": "time is in the past" },
    { "status": 409, "code": "slot_unavailable", "message": "This time is no longer available" }
  ]
},
{
  "id": "update-booking-status",
  "status": "live",
  "method": "PATCH",
  "path": "/bookings/{bookingId}/status",
  "auth": "owner or front desk; staff can set in_progress and completed on their own bookings",
  "description": "status: in_progress (check in / start), completed, no_show (only after the start time), cancelled (cancelReason 'business' unless a reason is sent). Returns the full Booking.",
  "request": { "status": "in_progress", "reason": null },
  "response": "Booking",
  "errors": [
    { "status": 400, "code": "invalid_status_transition", "message": "Cannot move from completed to in_progress" },
    { "status": 403, "code": "forbidden", "message": "Only the owner or front desk can do this" },
    { "status": 404, "code": "not_found", "message": "bookingId does not exist" }
  ]
}
```

**App code to update**

- `createWalkInBooking` / `createScheduledBooking` (`src/api/booking.ts`): send `services: [{ serviceId, quantity }]` and drop names, prices, `durationMinutes`, `totalAmount`, `businessName` and `staffName`. `staffId` is `Staff.id` (see BK-02), not the invitation id.
- Today / Calendar: `GET /businesses/{id}/bookings?date=` or `?from=&to=` instead of the session-only array. Add an "Unassigned" filter using `unassigned=true`, feeding the Assign staff picker (BK-26).
- Status stepper: `update-booking-status` now returns the whole `Booking`, not just `{ bookingId, status }`. Completing is allowed here until the charge flow (`charge-booking`) exists; then completion should move there.
- `source` is `walk_in` for both walk-ins and on-behalf bookings, as `fulfillment.json` already says.

---

## 2026-09-27 · Payments now confirm bookings (BK-32)

**Why:** M-Pesa's result is now applied: a successful deposit confirms the booking. No new endpoints; the polling described under BK-31 starts working end to end.

**What the app will see when polling `GET /bookings/{bookingId}`**

| Outcome | `status` | `paymentStatus` | `depositPayment` | Other |
| --- | --- | --- | --- | --- |
| Paid | `confirmed` | `deposit_paid` | `{ status: "succeeded" }` | `paymentReference` = M-Pesa receipt, `paymentMethod` = `"mpesa"`, `expiresAt` = null |
| Dismissed / wrong PIN / no money | `pending_payment` | `unpaid` | `{ status: "failed", failureReason }` | can retry until `expiresAt` |
| No answer | `pending_payment` | `unpaid` | `{ status: "timeout", failureReason: "timeout" }` | can retry until `expiresAt` |
| Paid after the hold ran out, slot still free | `confirmed` | `deposit_paid` | `{ status: "succeeded" }` | `cancelReason` back to null |
| Paid after the hold ran out, slot taken | `cancelled` | `deposit_paid` | `{ status: "succeeded" }` | `cancelReason: "payment_expired"`; show "Your payment arrived after the booking expired; the business will refund you" |

**App code to update**

- STK Push Pending: stop polling when one of the rows above is reached, and route: confirmed → Booking Confirmed; failed/timeout → back to Payment Method with the `failureReason` explained; cancelled + `deposit_paid` → the refund message.
- The last row is rare (it needs the payment to land after the 10-minute hold), and refunds are manual for now (see BK-60).

---

## 2026-09-27 · Pay the deposit by M-Pesa (BK-31)

**Why:** replaces the mocked `confirmBookingPayment`. It's now two steps: start the STK push, then poll the booking. The money goes straight to the business's own till, paybill or bank.

**Replaces → `booking.json#confirm-booking-payment`**

```json
{
  "id": "start-booking-payment",
  "status": "live",
  "screen": "Payment Method → STK Push Pending",
  "story": "C2",
  "method": "POST",
  "path": "/bookings/{bookingId}/payments",
  "auth": "the customer who booked",
  "description": "Sends the M-Pesa prompt for a pending_payment booking's deposit. phone is optional (defaults to the phone on the booking; any Kenyan format). Returns 202 immediately, while the customer enters their PIN. Then poll GET /bookings/{bookingId} every 3 seconds until status is confirmed (paid) or depositPayment.status is failed/timeout; stop after about 2 minutes. On failure the customer can try again (a new prompt) until expiresAt. At most one prompt per 2 minutes and 5 per booking.",
  "request": { "phone": "0712345678" },
  "response": {
    "paymentId": "uuid",
    "bookingId": "uuid",
    "status": "pending",
    "amount": 300,
    "currency": "KES",
    "phone": "254712345678",
    "customerMessage": "Success. Request accepted for processing",
    "expiresAt": "2026-09-27T10:10:00Z"
  },
  "errors": [
    { "status": 400, "code": "invalid_phone", "message": "Enter a Kenyan mobile number, e.g. 0712 345 678" },
    { "status": 409, "code": "payment_in_progress", "message": "An M-Pesa prompt was just sent; complete or cancel it on your phone first", "paymentId": "uuid" },
    { "status": 409, "code": "hold_expired", "message": "This booking's hold ran out; please book again" },
    { "status": 409, "code": "no_deposit_due", "message": "This booking has no deposit to pay (status confirmed)" },
    { "status": 409, "code": "already_paid", "message": "The deposit is already paid" },
    { "status": 409, "code": "business_cannot_take_payments", "message": "This business can't take M-Pesa payments right now" },
    { "status": 429, "code": "too_many_attempts", "message": "Too many payment attempts for this booking" },
    { "status": 502, "code": "payment_provider_error", "message": "M-Pesa couldn't send the prompt; please try again" },
    { "status": 503, "code": "payments_unavailable", "message": "Payments are temporarily unavailable" }
  ]
}
```

**Changed response → `Booking`**: `depositPayment` is now `{ paymentId, status: pending | succeeded | failed | timeout, failureReason }` once a payment has been started. `failureReason` is one of `cancelled` (dismissed the prompt), `insufficient_funds`, `wrong_pin`, `timeout`, `busy` (another M-Pesa transaction in progress), `provider_error`, `failed`. `paymentReference` (the M-Pesa receipt) and `paymentMethod: "mpesa"` are set once it succeeds.

**App code to update**

- `confirmBookingPayment`: call `POST /bookings/{bookingId}/payments` with the phone from Payment Method, then show STK Push Pending and poll `GET /bookings/{bookingId}` as described. The old 402/408 errors become `depositPayment.status` `failed` / `timeout` with a `failureReason` to explain.
- On `409 payment_in_progress`, keep polling instead of sending a new prompt.
- Confirmation works from BK-32 (see above for every outcome the polling can end in).

---

## 2026-09-27 · Assign staff to a booking (BK-26)

**Why:** "Any available" bookings are left unassigned (D4); the owner or front desk now gives them to someone. Owner and front-desk apps only.

**New endpoint → `fulfillment.json`**

```json
{
  "id": "assign-booking-staff",
  "status": "live",
  "screen": "Appointment detail (owner / front desk): Assign staff",
  "method": "PATCH",
  "path": "/bookings/{bookingId}/staff",
  "auth": "the business owner, or its front desk",
  "description": "Assigns an unassigned booking, or reassigns an assigned one. The staff member must offer every service on the booking, work that weekday, and be free (including room for the business's other unassigned bookings at that time). Only pending_payment, confirmed and in_progress bookings can be (re)assigned.",
  "request": { "staffId": "uuid" },
  "response": "Booking (staffId and staffName now set)",
  "errors": [
    { "status": 409, "code": "staff_unavailable", "message": "That staff member can't take this booking: busy, off that day, or doesn't offer the service" },
    { "status": 409, "code": "booking_not_active", "message": "A cancelled booking can't be reassigned" },
    { "status": 403, "code": "forbidden", "message": "Only the owner or front desk can assign staff" },
    { "status": 404, "code": "not_found", "message": "No booking found for this id / staffId does not exist for this business" }
  ]
}
```

**App code to update**

- Owner / front-desk booking detail: an "Assign staff" picker when `staffId` is null (and a "Reassign" action otherwise). On `409 staff_unavailable`, keep the picker open and let them choose someone else.
- Finding unassigned bookings needs the business's booking list (`GET /businesses/{id}/bookings`, BK-63), which isn't built yet.

---

## 2026-09-27 · Read bookings (BK-23)

**Why:** bookings now live on the server, so My Appointments and Appointment Detail survive a reinstall and work across devices. Replaces `useBookingsStore` as the source of truth.

**New endpoints → `booking.json`**

```json
{
  "id": "get-booking",
  "status": "live",
  "screen": "Appointment Detail; STK Push Pending (poll while paying, from BK-31)",
  "method": "GET",
  "path": "/bookings/{bookingId}",
  "auth": "the customer who booked, or the business's owner or staff",
  "description": "The same Booking shape create-booking returns, plus depositPayment: null until a deposit payment is started (BK-31), then { paymentId, status: pending | succeeded | failed | timeout, failureReason }. An unpaid booking whose hold ran out shows status cancelled with cancelReason payment_expired.",
  "response": "Booking",
  "errors": [{ "status": 404, "code": "not_found", "message": "No booking found for this id" }]
},
{
  "id": "list-my-bookings",
  "status": "live",
  "screen": "My Appointments (Upcoming / Past / Cancelled)",
  "method": "GET",
  "path": "/me/bookings?filter=upcoming&limit=20&cursor={nextCursor}",
  "auth": "any signed-in, verified user",
  "description": "filter: upcoming = pending_payment, confirmed or in_progress and not yet over, soonest first. past = completed and no_show, plus confirmed bookings whose time has passed, newest first. cancelled = newest first. limit 1-50 (default 20). Pass nextCursor back as cursor for the next page; it's null on the last page.",
  "response": { "bookings": ["Booking"], "nextCursor": "string | null" },
  "errors": [
    { "status": 400, "code": "validation_error", "message": "filter must be one of upcoming, past, cancelled" },
    { "status": 400, "code": "validation_error", "message": "Invalid cursor" }
  ]
}
```

**App code to update**

- My Appointments: one request per tab (`filter=upcoming|past|cancelled`), with infinite scroll on `nextCursor`. Drop the in-memory `useBookingsStore` as the source (a cache is fine).
- Appointment Detail: load by `bookingId` with `GET /bookings/{bookingId}`.
- `Booking` gains `depositPayment` (null for now).

---

## 2026-09-27 · Create a booking (BK-22)

**Why:** replaces the mocked `createBooking` (`USE_MOCK_BOOKING`). The server prices the booking itself, checks the slot is still free, and holds it while a deposit is paid.

**Replaces → `booking.json#create-booking`** (the request is much smaller than today's)

```json
{
  "id": "create-booking",
  "status": "live",
  "screen": "Review & Policies",
  "story": "C2",
  "method": "POST",
  "path": "/bookings",
  "auth": "any signed-in, verified user",
  "description": "Send only what the customer chose: names, prices, totals and the deposit come from the server. staffId null = 'Any available' (left unassigned for the owner to assign). time must be one of the slots from get-availability. No deposit: status confirmed. Deposit due: status pending_payment, and the slot is held until expiresAt (10 minutes); paying it is the next task (BK-31). expectedTotal is optional: send the total the customer saw, and if prices changed you get 409 price_changed with the new total instead of a booking.",
  "request": {
    "businessId": "uuid",
    "staffId": "uuid | null",
    "services": [{ "serviceId": "uuid", "quantity": 1 }],
    "date": "2026-10-05",
    "time": "14:30",
    "notes": "Short on the sides",
    "expectedTotal": { "amount": 800, "currency": "KES" }
  },
  "response": {
    "bookingId": "uuid",
    "reference": "BK7F3AQ9",
    "businessId": "uuid",
    "businessName": "Glow Beauty Lounge",
    "customerName": "Amina W",
    "customerPhone": "+254722000111",
    "customerEmail": "amina@example.com",
    "staffId": "uuid | null",
    "staffName": "Jane Wanjiru | Any available",
    "services": [{ "serviceId": "uuid", "name": "Classic Haircut", "durationMinutes": 45, "price": { "amount": 800, "currency": "KES" }, "quantity": 1 }],
    "date": "2026-10-05",
    "time": "14:30",
    "startsAt": "2026-10-05T14:30:00+03:00",
    "durationMinutes": 45,
    "totalAmount": { "amount": 800, "currency": "KES" },
    "depositAmount": null,
    "status": "confirmed",
    "source": "online",
    "paymentStatus": "unpaid",
    "paymentMethod": null,
    "paymentReference": null,
    "expiresAt": null,
    "cancelReason": null,
    "notes": "Short on the sides",
    "createdAt": "2026-09-27T10:00:00Z"
  },
  "errors": [
    { "status": 409, "code": "slot_unavailable", "message": "The selected time is no longer available" },
    { "status": 409, "code": "price_changed", "message": "Prices have changed since you started; please review the new total", "totalAmount": { "amount": 900, "currency": "KES" } },
    { "status": 400, "code": "staff_cannot_perform", "message": "This staff member doesn't offer all of the chosen services" },
    { "status": 400, "code": "own_business", "message": "You can't book your own business as a customer" },
    { "status": 400, "code": "validation_error", "message": "serviceIds contains services not in this business: …" },
    { "status": 404, "code": "not_found", "message": "No published business found for this id" }
  ]
}
```

**App code to update**

- `CreateBookingInput` in `src/api/booking.ts`: drop `businessName`, `customerName`, `staffName`, each service's `name`/`durationMinutes`/`price`, `durationMinutes`, `totalAmount` and `depositAmount`. Send `services: [{ serviceId, quantity }]`, and the total shown on Review & Policies as `expectedTotal`.
- Show the returned booking as-is (it's the same `Booking` shape as today, plus `reference`, `startsAt`, `expiresAt`, `cancelReason`, `notes`).
- On `409 slot_unavailable`, send the customer back to Select Date & Time and refresh the slots. On `409 price_changed`, show the new `totalAmount` and ask them to confirm again.
- `paymentStatus` can now also be `deposit_paid`.
- For `pending_payment`, show a countdown to `expiresAt`. The payment step itself (replacing `confirmBookingPayment`) comes with BK-31.

---

## 2026-09-26 · Server-side availability (BK-11)

**Why:** replaces `getUpcomingDays` and `getTimeSlots` in `src/utils/availability.ts`. The server knows each staff member's working days and services, the business's booking settings, and (from BK-20) existing bookings. Until bookings exist on the server, slots only reflect hours, staff and settings.

**New endpoints → `booking.json`** (both public, like the business profile)

```json
{
  "id": "get-availability-days",
  "status": "live",
  "screen": "Select Date & Time: date strip",
  "method": "GET",
  "path": "/businesses/{businessId}/availability/days?serviceIds={id}&serviceIds={id}&staffId={staffId}&from=2026-09-28&days=14",
  "auth": "public",
  "description": "Which days are open and have at least one bookable slot. serviceIds is repeated once per service in the cart (required). staffId is optional; omit it for 'Any available'. from defaults to today in the business's timezone; days is 1-31 (default 14).",
  "response": {
    "timezone": "Africa/Nairobi",
    "durationMinutes": 45,
    "days": [
      { "date": "2026-09-28", "isOpen": true, "hasSlots": true },
      { "date": "2026-09-29", "isOpen": false, "hasSlots": false }
    ]
  },
  "errors": [
    { "status": 400, "code": "validation_error", "message": "serviceIds contains services not in this business: …" },
    { "status": 400, "code": "validation_error", "message": "days must be between 1 and 31" },
    { "status": 400, "code": "staff_cannot_perform", "message": "This staff member doesn't offer all of the chosen services" },
    { "status": 404, "code": "not_found", "message": "No published business found for this id" }
  ]
},
{
  "id": "get-availability",
  "status": "live",
  "screen": "Select Date & Time: time slots",
  "method": "GET",
  "path": "/businesses/{businessId}/availability?date=2026-09-28&serviceIds={id}&staffId={staffId}",
  "auth": "public",
  "description": "Bookable start times for one day. durationMinutes is the sum of the chosen services. time is business local time; startsAt is the same moment with its UTC offset. staffIds lists who is free for that slot; with no staffId the booking will be left unassigned (the owner assigns someone later), so show staffIds only as a hint.",
  "response": {
    "date": "2026-09-28",
    "timezone": "Africa/Nairobi",
    "durationMinutes": 45,
    "slots": [
      { "time": "09:00", "startsAt": "2026-09-28T09:00:00+03:00", "staffIds": ["uuid", "uuid"] },
      { "time": "09:15", "startsAt": "2026-09-28T09:15:00+03:00", "staffIds": ["uuid"] }
    ]
  },
  "errors": "same as get-availability-days (except the days range)"
}
```

**App code to update**

- The Select Date & Time step calls these instead of computing slots from `workingHours`. Pass every service in the cart as a repeated `serviceIds`, and `staffId` only when the customer picked someone.
- Slots are 15 minutes apart by default (was 30), and the earliest slot is at least 30 minutes from now. Both are per-business settings (BK-04).
- Keep showing times as given (`time`); they're already in the business's timezone.

---

## 2026-09-26 · Booking settings (BK-04)

**Why:** availability will use these rules, and all working hours and booking times are in the business's timezone. Fits the owner's "Working Hours & Holidays" screen (buffer time input).

**New endpoint → `business-setup.json`**

```json
{
  "id": "update-booking-settings",
  "status": "live",
  "screen": "Manage Business → Working Hours & Holidays (buffer time), booking rules",
  "method": "PATCH",
  "path": "/businesses/{businessId}/booking-settings",
  "auth": "owner",
  "description": "Only the fields sent change. timezone is an IANA name. slotIntervalMinutes is the grid customers pick start times on (5-60, must divide 60). bufferMinutes is kept free after each booking. minLeadMinutes is how soon the earliest slot is. maxDaysAhead is how far ahead customers can book.",
  "request": { "slotIntervalMinutes": 15, "bufferMinutes": 10 },
  "response": {
    "businessId": "uuid", "timezone": "Africa/Nairobi", "slotIntervalMinutes": 15,
    "bufferMinutes": 10, "minLeadMinutes": 30, "maxDaysAhead": 30
  },
  "errors": [
    { "status": 400, "code": "validation_error", "message": "slotIntervalMinutes must divide 60 evenly (5, 10, 15, 20, 30 or 60)" },
    { "status": 400, "code": "validation_error", "message": "Unknown timezone 'X'; use an IANA name like Africa/Nairobi" }
  ]
}
```

**Changed response → `business-setup.json#get-business-draft`**: gains `bookingSettings` (the same object as the response above). Defaults: Africa/Nairobi, 15-minute grid, no buffer, 30-minute lead time, 30 days ahead.

---

## 2026-09-26 · Staff services (BK-03)

**Why:** availability and booking need to know who can do which service. The owner's Member Detail screen ("services they perform, multi-select") can now save that.

**New endpoints → `team.json`**

```json
{
  "id": "get-staff-services",
  "status": "live",
  "screen": "Member Detail (business-owner app): services they perform",
  "method": "GET",
  "path": "/businesses/{businessId}/staff/{staffId}/services",
  "auth": "owner",
  "response": { "staffId": "uuid", "allServices": false, "serviceIds": ["uuid"] },
  "errors": [{ "status": 404, "code": "not_found", "message": "staffId does not exist for this business" }]
},
{
  "id": "set-staff-services",
  "status": "live",
  "screen": "Member Detail (business-owner app): services they perform",
  "method": "PUT",
  "path": "/businesses/{businessId}/staff/{staffId}/services",
  "auth": "owner",
  "description": "allServices: true = performs every service, including ones added later (the default for everyone). allServices: false = performs exactly serviceIds, which may be empty (e.g. front desk). Replaces the whole list. Deleting a service removes it from everyone's list; it never flips someone back to allServices.",
  "request": { "allServices": false, "serviceIds": ["uuid", "uuid"] },
  "response": { "staffId": "uuid", "allServices": false, "serviceIds": ["uuid", "uuid"] },
  "errors": [
    { "status": 400, "code": "validation_error", "message": "serviceIds must be empty when allServices is true" },
    { "status": 400, "code": "validation_error", "message": "serviceIds contains services not in this business: …" },
    { "status": 404, "code": "not_found", "message": "staffId does not exist for this business" }
  ]
}
```

**Changed response → `discovery.json#get-business-profile`**: each `staff[]` item gains `allServices` and `serviceIds`, with the same meaning as above. The Select Staff step can use them to hide staff who can't do everything in the cart.

```json
{ "staffId": "uuid", "name": "Wanjiku", "role": "staff", "workingDays": ["monday", "friday"], "allServices": false, "serviceIds": ["uuid"], "rating": 4.8, "…": "…" }
```

---

## 2026-09-26 · One staff id (BK-02)

**Why:** a person was two ids: the profile's `staffId` (a `Staff` row) and the roster's `invitationId`. Bookings, availability and earnings will all use **`staffId`**.

**Changed response → `team.json#list-team-members`, `#add-team-member`, `#update-team-member`, and `business-setup.json#invite-team-member`**: each member gains `staffId`. It's `null` while the invitation is pending and set once it's accepted. It's the same id as `staff[].staffId` on the business profile.

```json
{ "invitationId": "uuid", "staffId": "uuid | null", "name": "Wanjiku", "status": "accepted", "commissionPercent": 45, "workingDays": ["monday", "friday"], "…": "…" }
```

**Changed response → `discovery.json#get-business-profile`**: each `staff[]` item gains `workingDays` (`null` = works whenever the business is open). `commissionPercent` stays private to the team endpoints.

**App code to update**

- `src/api/booking.ts` (`CreateOwnerBookingInput.staffId`) and `TeamMemberDetailScreen`'s earnings currently use `invitationId` as `staffId`. Switch them to the member's `staffId`, and only offer accepted members (non-null `staffId`) as bookable.
- Editing commission or working days is unchanged (still `PATCH /team/members/{invitationId}`); the server keeps the staff record in sync.
