# Frontend changes from the booking work

What the app needs to integrate or update, newest first. Each entry says which file in `mobile/reference/api/` it belongs to, and gives the endpoint in the same JSON shape as those files so it can be pasted in. Task ids link back to [BACKLOG.md](BACKLOG.md).

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
