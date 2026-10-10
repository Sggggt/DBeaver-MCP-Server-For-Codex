"""Discover workspace metadata and retain one selected project per MCP session."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from xml.etree import ElementTree

from .config import Settings
from .errors import ToolError

STORAGE_FILES = ("data-sources.json", "credentials-config.json")


@dataclass(frozen=True)
class DBeaverProject:
    project_id: str
    project_name: str
    config_dir: Path = field(repr=False)

    def identity(self) -> dict[str, str]:
        return {"project_id": self.project_id, "project_name": self.project_name}

    def summary(self) -> dict:
        missing = [
            name for name in STORAGE_FILES if not (self.config_dir / name).is_file()
        ]
        return {
            **self.identity(),
            "has_data_sources": "data-sources.json" not in missing,
            "has_credentials": "credentials-config.json" not in missing,
            "connection_storage_ready": not missing,
            "missing_files": missing,
        }


def project_from_config(config_dir: Path) -> DBeaverProject:
    directory = config_dir.parent if config_dir.name == ".dbeaver" else config_dir
    name = directory.name
    marker = directory / ".project"
    try:
        # Metadata never supplies paths; oversized/entity-bearing XML uses the folder name.
        if marker.resolve().parent == directory.resolve():
            with marker.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) <= 65536 and not any(
                token in raw.upper() for token in (b"<!DOCTYPE", b"<!ENTITY")
            ):
                root = ElementTree.fromstring(raw)
                display_name = root.findtext("name")
                if root.tag == "projectDescription" and display_name:
                    name = display_name.strip() or name
    except (OSError, ValueError, ElementTree.ParseError):
        pass
    return DBeaverProject(directory.name, name, config_dir)


def discover_projects(settings: Settings) -> list[DBeaverProject]:
    workspace = settings.dbeaver_workspace_dir
    if workspace is None:
        return [project_from_config(settings.dbeaver_config_dir)]
    try:
        root = workspace.resolve()
        result = []
        for directory in root.iterdir():
            config_dir = directory / ".dbeaver"
            if (
                directory.name.startswith(".")
                or not directory.is_dir()
                or not directory.resolve().is_relative_to(root)
                or not config_dir.resolve().is_relative_to(root)
            ):
                continue
            if (directory / ".project").is_file() or config_dir.is_dir():
                result.append(project_from_config(config_dir))
        return sorted(
            result, key=lambda item: (item.project_name.casefold(), item.project_id)
        )
    except (OSError, ValueError) as exc:
        raise ToolError(
            "无法读取 DBeaver 工作区项目目录。",
            code="PROJECT_STORAGE_ERROR",
            stage="configuration",
            hint="检查当前用户的 DBeaver workspace6 是否存在且可读。",
        ) from exc


def initial_project(projects: list[DBeaverProject]) -> DBeaverProject:
    candidates = [item for item in projects if item.summary()["has_data_sources"]]
    if len(candidates) == 1:
        return candidates[0]
    raise ToolError(
        "尚未选择 DBeaver 项目。",
        code="PROJECT_REQUIRED",
        stage="configuration",
        path="project",
        hint="调用 list_dbeaver_projects，再用 select_dbeaver_project 选择精确 project_id。",
    )


def project_settings(settings: Settings, project: DBeaverProject) -> Settings:
    return replace(
        settings, dbeaver_config_dir=project.config_dir, dbeaver_workspace_dir=None
    )


class ProjectSession:
    """Selection lives in this STDIO process; discovery never changes it."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings
        self.active_project: DBeaverProject | None = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = Settings.from_dbeaver()
        return self._settings

    def list_projects(self) -> dict:
        projects = discover_projects(self.settings)
        return {
            "projects": [item.summary() for item in projects],
            "returned": len(projects),
            "active_project": (
                self.active_project.identity() if self.active_project else None
            ),
        }

    def select(self, project_id: str) -> dict:
        # Match discovered IDs instead of interpreting input as a filesystem path.
        matches = [
            item
            for item in discover_projects(self.settings)
            if item.project_id == project_id
        ]
        if not matches:
            raise ToolError(
                "未找到指定的 DBeaver 项目。",
                code="PROJECT_NOT_FOUND",
                stage="configuration",
                path="project",
                hint="调用 list_dbeaver_projects，使用返回的精确 project_id。",
            )
        self.active_project = matches[0]
        return {**self.active_project.summary(), "session_scope": "mcp_process"}

    def require_project(self) -> tuple[Settings, DBeaverProject]:
        if self.active_project is None:
            self.active_project = initial_project(discover_projects(self.settings))
        return project_settings(self.settings, self.active_project), self.active_project
