# Single source of truth for Smart Meeting System vocabulary — mirrors the
# pattern in app/core/onboarding_constants.py. Both app/models/meeting.py and
# app/schemas/meeting.py import from here so the two can't drift apart.

MEETING_PRIORITIES = {"low", "medium", "high"}
MEETING_VISIBILITIES = {"private", "team", "organization"}
MEETING_RECURRENCES = {"none", "daily", "weekly", "biweekly", "monthly"}

# The 9 prebuilt meeting types from the plan (section 5) — "custom" has no
# default agenda; the rest seed a MeetingTemplate with a sensible starting
# agenda the user can edit.
MEETING_TYPES = {
    "weekly_sync", "project_review", "sprint_planning", "daily_standup",
    "one_on_one", "client_meeting", "retrospective", "leadership_review", "custom",
}
