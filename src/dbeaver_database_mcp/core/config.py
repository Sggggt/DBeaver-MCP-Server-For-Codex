from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _default_dbeaver_config_dir() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "DBeaverData" / "workspace6" / "General" / ".dbeaver"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "DBeaverData"
            / "workspace6"
            / "General"
            / ".dbeaver"
        )
    return (
        Path.home()
        / ".local"
        / "share"
        / "DBeaverData"
        / "workspace6"
        / "General"
        / ".dbeaver"
    )


@dataclass(frozen=True)
class Settings:
    dbeaver_config_dir: Path
    connect_timeout_seconds: int = 8
    statement_timeout_ms: int = 30_000
    lock_timeout_ms: int = 5_000
    hard_max_rows: int = 500
    default_max_rows: int = 50
    max_cell_chars: int = 4_000
    max_result_bytes: int = 524_288
    dbeaver_workspace_dir: Path | None = None

    @classmethod
    def from_dbeaver(cls, config_dir: Path | None = None) -> Settings:
        if config_dir is None:
            default_dir = _default_dbeaver_config_dir().expanduser()
            return cls(
                dbeaver_config_dir=default_dir.resolve(),
                dbeaver_workspace_dir=default_dir.parent.parent.resolve(),
            )
        resolved = config_dir.expanduser().resolve()
        settings = cls(dbeaver_config_dir=resolved)
        missing = [
            path.name
            for path in (settings.data_sources_file, settings.credentials_file)
            if not path.is_file()
        ]
        if missing:
            raise FileNotFoundError(
                "DBeaver connection storage is incomplete; missing: "
                + ", ".join(missing)
            )
        return settings

    @property
    def data_sources_file(self) -> Path:
        return self.dbeaver_config_dir / "data-sources.json"

    @property
    def credentials_file(self) -> Path:
        return self.dbeaver_config_dir / "credentials-config.json"
