# Frontend changes from the booking work

What the app needs to integrate or update, newest first. Each entry says which file in `mobile/reference/api/` it belongs to, and gives the endpoint in the same JSON shape as those files so it can be pasted in. Task ids link back to [BACKLOG.md](BACKLOG.md).

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
