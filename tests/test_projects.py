"""Project discovery and session isolation, using only synthetic saved metadata."""

import json

import pytest

from dbeaver_database_mcp.core import mcp_server
from dbeaver_database_mcp.core.config import Settings
from dbeaver_database_mcp.core.connection import DBeaverConnectionStore
from dbeaver_database_mcp.core.errors import ToolError, error_payload
from dbeaver_database_mcp.core.projects import ProjectSession, discover_projects
from dbeaver_database_mcp.core.registry import adapter_class


def workspace_settings(root):
    return Settings(root / "General" / ".dbeaver", dbeaver_workspace_dir=root)


def saved_project(
    root, project_id, name=None, with_sources=True, with_credentials=True
):
    directory = root / project_id
    directory.mkdir(parents=True)
    (directory / ".project").write_text(
        f"<projectDescription><name>{name or project_id}</name></projectDescription>",
        encoding="utf-8",
    )
    config = directory / ".dbeaver"
    config.mkdir()
    if with_sources:
        (config / "data-sources.json").write_text(
            json.dumps(
                {
                    "connections": {
                        "shared-id": {
                            "provider": "postgresql",
                            "name": "same-name",
                            "configuration": {
                                "host": "localhost",
                                "database": project_id,
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    if with_credentials:
        # Deliberately invalid ciphertext: metadata discovery must never decrypt it.
        (config / "credentials-config.json").write_bytes(b"do-not-read-secret-canary")
    return config


def fake_credentials(monkeypatch):
    monkeypatch.setattr(
        DBeaverConnectionStore,
        "_read_credentials",
        lambda self: {"shared-id": {"#connection": {"user": "reader", "password": ""}}},
    )


def test_default_settings_discover_workspace_without_general(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "dbeaver_database_mcp.core.config._default_dbeaver_config_dir",
        lambda: tmp_path / "General" / ".dbeaver",
    )
    saved_project(tmp_path, "renamed")
    settings = Settings.from_dbeaver()
    assert settings.dbeaver_workspace_dir == tmp_path
    assert [item.project_id for item in discover_projects(settings)] == ["renamed"]


def test_discovery_reads_names_and_file_presence_without_credentials(tmp_path):
    saved_project(tmp_path, "folder-id", "医院项目")
    saved_project(tmp_path, "empty", with_sources=False, with_credentials=False)
    (tmp_path / "unrelated").mkdir()
    (tmp_path / ".metadata").mkdir()
    session = ProjectSession(workspace_settings(tmp_path))
    result = session.list_projects()
    projects = {item["project_id"]: item for item in result["projects"]}
    assert set(projects) == {"folder-id", "empty"}
    assert projects["folder-id"]["project_name"] == "医院项目"
    assert projects["folder-id"]["connection_storage_ready"] is True
    assert projects["empty"]["missing_files"] == [
        "data-sources.json",
        "credentials-config.json",
    ]
    assert result["active_project"] is None
    assert str(tmp_path) not in json.dumps(result)
    assert "secret-canary" not in json.dumps(result)


@pytest.mark.parametrize(
    "marker",
    [
        "<broken",
        "x" * 65537,
        '<!DOCTYPE x [<!ENTITY label "secret-canary">]><projectDescription>'
        "<name>&label;</name></projectDescription>",
    ],
    ids=["malformed", "oversized", "entity"],
)
def test_invalid_metadata_uses_directory_name(tmp_path, marker):
    saved_project(tmp_path, "fallback")
    (tmp_path / "fallback" / ".project").write_text(marker, encoding="utf-8")
    project = discover_projects(workspace_settings(tmp_path))[0]
    assert project.project_name == "fallback"


def test_missing_workspace_is_safe_configuration_error(tmp_path):
    with pytest.raises(ToolError) as caught:
        discover_projects(workspace_settings(tmp_path / "private-canary"))
    result = error_payload(caught.value)
    assert result["code"] == "PROJECT_STORAGE_ERROR"
    assert "private-canary" not in json.dumps(result)


def test_multiple_projects_require_selection(tmp_path):
    saved_project(tmp_path, "first")
    saved_project(tmp_path, "second")
    session = ProjectSession(workspace_settings(tmp_path))
    with pytest.raises(ToolError) as caught:
        session.require_project()
    assert caught.value.code == "PROJECT_REQUIRED"
    assert session.active_project is None


def test_initial_selection_is_sticky_even_after_new_project_appears(
    tmp_path, monkeypatch
):
    saved_project(tmp_path, "first")
    saved_project(tmp_path, "empty", with_sources=False, with_credentials=False)
    session = ProjectSession(workspace_settings(tmp_path))
    settings, project = session.require_project()
    assert project.project_id == "first"
    saved_project(tmp_path, "second")
    assert session.list_projects()["active_project"]["project_id"] == "first"
    monkeypatch.setattr(
        "dbeaver_database_mcp.core.projects.discover_projects",
        lambda *a: pytest.fail("active calls must not rediscover projects"),
    )
    assert session.require_project() == (settings, project)


@pytest.mark.parametrize(
    "project_id", ["../outside", "C:\\outside", "missing", "医院项目"]
)
def test_unknown_project_does_not_change_session(tmp_path, project_id):
    saved_project(tmp_path, "first", "医院项目")
    session = ProjectSession(workspace_settings(tmp_path))
    session.select("first")
    with pytest.raises(ToolError) as caught:
        session.select(project_id)
    assert caught.value.code == "PROJECT_NOT_FOUND"
    assert session.active_project.project_id == "first"


def test_duplicate_ids_and_names_are_isolated_by_selected_project(
    tmp_path, monkeypatch
):
    saved_project(tmp_path, "first")
    saved_project(tmp_path, "second")
    fake_credentials(monkeypatch)
    session = ProjectSession(workspace_settings(tmp_path))
    for project_id in ("first", "second"):
        session.select(project_id)
        settings, project = session.require_project()
        store = DBeaverConnectionStore(settings, project)
        for identifier in ("shared-id", "same-name"):
            source = store.resolve(identifier)
            assert source.database == project_id
            assert source.model_identity("")["project_id"] == project_id
            assert source.for_database("other").project_id == project_id


def test_missing_active_storage_does_not_fall_back(tmp_path):
    config = saved_project(tmp_path, "first")
    saved_project(tmp_path, "second")
    session = ProjectSession(workspace_settings(tmp_path))
    session.select("first")
    (config / "data-sources.json").unlink()
    settings, project = session.require_project()
    with pytest.raises(ToolError) as caught:
        DBeaverConnectionStore(settings, project).connections()
    result = error_payload(caught.value)
    assert result["code"] == "CONNECTION_STORAGE_ERROR"
    assert result["details"]["project_id"] == "first"
    assert result["details"]["missing_files"] == ["data-sources.json"]
    assert session.active_project.project_id == "first"


def test_session_state_is_not_shared_between_process_sessions(tmp_path):
    saved_project(tmp_path, "first")
    saved_project(tmp_path, "second")
    old = ProjectSession(workspace_settings(tmp_path))
    old.select("first")
    new = ProjectSession(workspace_settings(tmp_path))
    assert new.list_projects()["active_project"] is None


def test_dispatch_uses_active_project_for_every_connection_call(tmp_path, monkeypatch):
    saved_project(tmp_path, "first")
    saved_project(tmp_path, "second")
    fake_credentials(monkeypatch)
    session = ProjectSession(workspace_settings(tmp_path))
    monkeypatch.setattr(mcp_server, "PROJECT_SESSION", session)
    monkeypatch.setattr(
        adapter_class("postgresql"),
        "status",
        lambda self, source, database: source.model_identity(""),
    )
    monkeypatch.setattr(
        adapter_class("postgresql"),
        "list_databases",
        lambda self, source: {"databases": [{"name": source.database}]},
    )
    assert mcp_server._handle_tool("list_dbeaver_projects", {})["returned"] == 2
    for project_id in ("first", "second"):
        selected = mcp_server._handle_tool(
            "select_dbeaver_project", {"project": project_id}
        )
        assert selected["project_id"] == project_id
        for _ in range(2):
            status = mcp_server._handle_tool(
                "dbeaver_status",
                {
                    "connection": "shared-id",
                    "database": project_id,
                    "database_type": "postgresql",
                },
            )
            assert status["project_id"] == project_id
        connections = mcp_server._handle_tool("list_dbeaver_connections", {})
        assert connections["project_id"] == project_id
        assert connections["connections"][0]["project_id"] == project_id


def test_invalid_project_request_is_rejected_before_discovery(monkeypatch):
    monkeypatch.setattr(
        mcp_server.PROJECT_SESSION,
        "select",
        lambda *a: pytest.fail("invalid payload reached project discovery"),
    )
    with pytest.raises(ToolError):
        mcp_server._handle_tool(
            "select_dbeaver_project", {"project": "first", "path": "private-canary"}
        )


def test_explicit_config_dir_remains_supported(tmp_path, monkeypatch):
    config = saved_project(tmp_path, "first")
    fake_credentials(monkeypatch)
    settings = Settings.from_dbeaver(config)
    assert settings.dbeaver_workspace_dir is None
    source = DBeaverConnectionStore(settings).resolve("shared-id")
    assert source.project_id == "first"
