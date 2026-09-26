# Frontend changes from the booking work

What the app needs to integrate or update, newest first. Each entry says which file in `mobile/reference/api/` it belongs to, and gives the endpoint in the same JSON shape as those files so it can be pasted in. Task ids link back to [BACKLOG.md](BACKLOG.md).

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
