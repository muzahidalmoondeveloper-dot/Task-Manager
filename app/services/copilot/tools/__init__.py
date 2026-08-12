"""Domain Tool Registry package. Importing this package registers every
available tool (via each domain module's module-level register_tool()
calls) into registry.TOOL_REGISTRY."""

from app.services.copilot.tools.registry import (  # noqa: F401
    ToolContext,
    ToolResult,
    ToolSpec,
    TOOL_REGISTRY,
    check_read_authorized,
    check_write_authorized,
    register_tool,
    run_tool,
)
from app.services.copilot.tools import task_tools  # noqa: F401 — registers task_* tools
from app.services.copilot.tools import read_tools  # noqa: F401 — registers search_* read tools
from app.services.copilot.tools import issue_tools  # noqa: F401 — registers create_issue/update_issue_status
from app.services.copilot.tools import rock_tools  # noqa: F401 — registers create_rock/update_rock_status
from app.services.copilot.tools import kpi_tools  # noqa: F401 — registers record_kpi_value
from app.services.copilot.tools import client_request_tools  # noqa: F401 — registers submit_client_request
from app.services.copilot.tools import meeting_tools  # noqa: F401 — registers schedule_meeting/update_meeting
from app.services.copilot.tools import org_structure_tools  # noqa: F401 — registers create_project/create_team
from app.services.copilot.tools import knowledge_tools  # noqa: F401 — registers search_everything (operational) + search_knowledge/create_knowledge_document (Knowledge/RAG)
from app.services.copilot.tools import reporting_tools  # noqa: F401 — registers generate_project_report
from app.services.copilot.tools import scoreboard_tools  # noqa: F401 — registers get_team_scoreboard/get_org_scoreboard
