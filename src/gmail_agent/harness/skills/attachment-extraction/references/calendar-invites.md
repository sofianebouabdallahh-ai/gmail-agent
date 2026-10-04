# Calendar invites (.ics)

The text is raw iCalendar. The useful fields are:

- `SUMMARY`: the event title.
- `DTSTART` / `DTEND`: start and end. A trailing `Z` means UTC; a `TZID=` parameter
  names the time zone. Report the time with its zone.
- `LOCATION`, and any video-call URL in `DESCRIPTION` or `X-GOOGLE-CONFERENCE`.
- `ORGANIZER` and `ATTENDEE` lines: names/emails, and `PARTSTAT` (NEEDS-ACTION means the
  owner has not answered yet).
- `METHOD:REQUEST` is an invitation, `METHOD:CANCEL` a cancellation, and `SEQUENCE`
  greater than 0 an update to an earlier invite.

Add the event date to `deadlines` and the organizer to `entities`.
