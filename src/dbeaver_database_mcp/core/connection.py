from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .config import Settings
from .errors import translate_error

_DBEAVER_AES_KEY = bytes.fromhex("babb4a9f774ab853c96c2d653dfe544a")
_DBEAVER_AES_IV = bytes(16)


@dataclass(frozen=True)
class DBeaverConnection:
    connection_id: str
    name: str
    provider: str
    driver: str
    database: str
    default_schema: str
    dbeaver_read_only: bool
    host: str = field(repr=False)
    port: int = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    sslmode: str | None = field(default=None, repr=False)

    options: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def database_type(self) -> str:
        return self.provider

    def for_database(self, database: str) -> DBeaverConnection:
        target = database.strip()
        if not target:
            raise ValueError("database is required")
        if "\x00" in target or len(target) > 128:
            raise ValueError("database name is invalid")
        return replace(
            self,
            database=target,
            default_schema=self.default_schema if target == self.database else "",
        )

    def model_identity(self, schema: str) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "connection_name": self.name,
            "database": self.database,
            "schema": schema,
            "database_type": self.database_type,
            "connection_source": "DBeaver workspace",
        }


class DBeaverConnectionStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.unavailable: list[dict[str, str]] = []

    def connections(self, database_type: str | None = None) -> list[DBeaverConnection]:
        from .registry import PROVIDERS

        sources = self._read_data_sources()
        credentials = self._read_credentials()
        result: list[DBeaverConnection] = []
        self.unavailable = []
        for connection_id, raw in sources.items():
            kind = PROVIDERS.get(str(raw.get("provider") or "").lower())
            if not kind or (database_type and kind != database_type):
                continue
            try:
                result.append(self._build_connection(connection_id, raw, credentials))
            except (ValueError, TypeError) as exc:
                self.unavailable.append(
                    {
                        "connection_id": connection_id,
                        "connection_name": str(raw.get("name") or connection_id),
                        "database_type": kind,
                        "code": translate_error(exc).code,
                    }
                )
        return sorted(
            result, key=lambda item: (item.name.casefold(), item.connection_id)
        )

    def resolve(self, identifier: str) -> DBeaverConnection:
        from .registry import PROVIDERS

        wanted = identifier.strip()
        if not wanted:
            raise ValueError("DBeaver connection name or id is required")
        matches = [
            (key, raw)
            for key, raw in self._read_data_sources().items()
            if str(raw.get("provider") or "").lower() in PROVIDERS
            and (key == wanted or raw.get("name") == wanted)
        ]
        if not matches:
            raise ValueError(f"DBeaver connection not found: {wanted}")
        if len(matches) > 1:
            raise ValueError(
                f"DBeaver connection name is ambiguous; use the connection id: {wanted}"
            )
        key, raw = matches[0]
        return self._build_connection(key, raw, self._read_credentials())

    def _read_data_sources(self) -> dict[str, dict[str, Any]]:
        try:
            document = json.loads(
                self.settings.data_sources_file.read_text(encoding="utf-8-sig")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("DBeaver data-sources.json could not be read") from exc
        connections = document.get("connections")
        if not isinstance(connections, dict):
            raise TypeError("DBeaver data-sources.json has no connections object")
        return {
            str(key): value
            for key, value in connections.items()
            if isinstance(value, dict)
        }

    def _read_credentials(self) -> dict[str, dict[str, Any]]:
        try:
            encrypted = self.settings.credentials_file.read_bytes()
            decryptor = Cipher(
                algorithms.AES(_DBEAVER_AES_KEY), modes.CBC(_DBEAVER_AES_IV)
            ).decryptor()
            padded = decryptor.update(encrypted) + decryptor.finalize()
            unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
            decrypted = unpadder.update(padded) + unpadder.finalize()
            if len(decrypted) <= 16:
                raise ValueError("DBeaver credential payload is too short")
            document = json.loads(decrypted[16:].decode("utf-8").strip())
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                "DBeaver credentials-config.json could not be decrypted"
            ) from exc
        finally:
            if "padded" in locals():
                padded = b""
            if "decrypted" in locals():
                decrypted = b""
        if not isinstance(document, dict):
            raise TypeError("DBeaver credential storage has an invalid structure")
        return {
            str(key): value
            for key, value in document.items()
            if isinstance(value, dict)
        }

    def _build_connection(
        self,
        connection_id: str,
        raw: dict[str, Any],
        credentials: dict[str, dict[str, Any]],
    ) -> DBeaverConnection:
        configuration = raw.get("configuration")
        if not isinstance(configuration, dict):
            raise TypeError(f"DBeaver connection has no configuration: {connection_id}")
        self._reject_network_handlers(configuration, connection_id)

        auth = credentials.get(connection_id, {}).get("#connection")
        if not isinstance(auth, dict):
            raise TypeError(
                f"DBeaver credentials are unavailable for connection: {connection_id}"
            )
        username = str(auth.get("user") or "")
        password = str(auth.get("password") or "")
        if not username:
            raise ValueError(
                f"DBeaver username is unavailable for connection: {connection_id}"
            )

        from .registry import PROVIDERS, adapter_class

        database_type = PROVIDERS[str(raw.get("provider") or "").lower()]
        options = adapter_class(database_type).parse_configuration(configuration)
        return DBeaverConnection(
            connection_id=connection_id,
            name=str(raw.get("name") or connection_id),
            provider=database_type,
            driver=str(raw.get("driver") or database_type),
            dbeaver_read_only=bool(raw.get("read-only", False)),
            username=username,
            password=password,
            **asdict(options),
        )

    @staticmethod
    def _reject_network_handlers(
        configuration: dict[str, Any], connection_id: str
    ) -> None:
        handlers = configuration.get("handlers")
        if not isinstance(handlers, dict):
            return
        enabled = [
            str(name)
            for name, value in handlers.items()
            if isinstance(value, dict) and bool(value.get("enabled"))
        ]
        if enabled:
            raise ValueError(
                "DBeaver network handlers are not supported by this MCP for connection "
                f"{connection_id}: {', '.join(sorted(enabled))}"
            )
