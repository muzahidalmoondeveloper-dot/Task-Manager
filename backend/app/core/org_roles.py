# Single source of truth for all role strings in the application.
# OrganizationMembership.role is the authoritative permission field;
# User.role (global column) is only a display/default value.

OWNER           = "owner"           # full control: billing, delete org, transfer ownership
ADMIN           = "admin"           # manage members, settings, invite/revoke
TEAM_MANAGER    = "team_manager"    # manage assigned teams and their tasks
PROJECT_MANAGER = "project_manager" # scoped to assigned project(s): view all teams, create tasks
TEAM_MEMBER     = "team_member"     # standard access: view and work on assigned tasks
CLIENT          = "client"          # external: read-only on assigned project(s) + task requests

ALL_ORG_ROLES        = {OWNER, ADMIN, TEAM_MANAGER, PROJECT_MANAGER, TEAM_MEMBER, CLIENT}
ORG_MANAGEMENT_ROLES = {OWNER, ADMIN}                # org-level admin (settings/billing/members)
APP_MANAGEMENT_ROLES = {OWNER, ADMIN, TEAM_MANAGER}  # app-level management (teams/projects/tasks)

ORG_ROLE_LABELS: dict[str, str] = {
    OWNER:           "Owner",
    ADMIN:           "Admin",
    TEAM_MANAGER:    "Team Manager",
    PROJECT_MANAGER: "Project Manager",
    TEAM_MEMBER:     "Team Member",
    CLIENT:          "Client",
}

# ---------------------------------------------------------------------------
# Legacy aliases — allow existing imports using the old ORG_OWNER / ORG_ADMIN /
# ORG_MEMBER names to keep resolving while each call site migrates to the
# short canonical names above.
# ---------------------------------------------------------------------------
ORG_OWNER  = OWNER
ORG_ADMIN  = ADMIN
ORG_MEMBER = TEAM_MEMBER
