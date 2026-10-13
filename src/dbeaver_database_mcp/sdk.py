"""Public in-process SDK sharing the exact MCP tool validation and dispatch.

Each client owns its project session. Credentials remain inside the connection
store and adapters; SDK callers submit the same closed JSON arguments as MCP.
"""

from copy import deepcopy

from .core.adapter import ConnectionParameters, DatabaseAdapter, QueryMode, unsupported
from .core.catalog import Table
from .core.config import Settings
from .core.errors import QueryRejected, register_error_translator
from .core.mcp_server import TOOLS, _handle_tool
from .core.projects import ProjectSession

__all__ = [
    "ConnectionParameters",
    "DatabaseAdapter",
    "DatabaseClient",
    "QueryMode",
    "QueryRejected",
    "Settings",
    "Table",
    "register_error_translator",
    "unsupported",
]


class DatabaseClient:
    def __init__(self, settings: Settings | None = None):
        self._session = ProjectSession(settings)

    @staticmethod
    def tools() -> list[dict]:
        return deepcopy(TOOLS)

    def call(self, name: str, arguments: dict | None = None) -> dict:
        return _handle_tool(
            name, {} if arguments is None else arguments, project_session=self._session
        )

    def list_projects(self) -> dict:
        return self.call("list_dbeaver_projects")

    def select_project(self, project: str) -> dict:
        return self.call("select_dbeaver_project", {"project": project})

    def capabilities(self, database_type: str) -> dict:
        return self.call("get_query_capabilities", {"database_type": database_type})
