"""Fixed, non-themeable constants for the Project Report Module.

Status colors must remain consistent across every report regardless of the
selected theme, for readability and print-accessibility reasons.
"""

STATUS_COLORS = {
    "on_track": "#22C55E",
    "at_risk": "#F97316",
    "off_track": "#EF4444",
    "completed": "#3B82F6",
    "not_started": "#94A3B8",
    "on_hold": "#A855F7",
}

REPORT_TYPES = {"weekly", "monthly", "client", "employee_performance", "team_performance"}
REPORT_STATUSES = {"draft", "finalized", "archived"}

THEME_PRESETS = [
    {
        "name": "Corporate Blue",
        "primary_color": "#1E3A8A",
        "accent_color": "#3B82F6",
        "secondary_color": "#0F172A",
    },
    {
        "name": "Modern Purple",
        "primary_color": "#5B21B6",
        "accent_color": "#8B5CF6",
        "secondary_color": "#1E1B2E",
    },
    {
        "name": "Emerald Green",
        "primary_color": "#065F46",
        "accent_color": "#10B981",
        "secondary_color": "#0F2A22",
    },
    {
        "name": "Premium Dark",
        "primary_color": "#111827",
        "accent_color": "#60A5FA",
        "secondary_color": "#000000",
    },
    {
        "name": "Warm Orange",
        "primary_color": "#9A3412",
        "accent_color": "#F97316",
        "secondary_color": "#2A1706",
    },
    {
        "name": "Minimal Gray",
        "primary_color": "#334155",
        "accent_color": "#64748B",
        "secondary_color": "#0F172A",
    },
]
