from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .config import Settings

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
            "connection_source": "DBeaver workspace",
        }


class DBeaverConnectionStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    def connections(self) -> list[DBeaverConnection]:
        sources = self._read_data_sources()
        credentials = self._read_credentials()
        result: list[DBeaverConnection] = []
        for connection_id, raw in sources.items():
            if str(raw.get("provider") or "").lower() != "postgresql":
                continue
            result.append(self._build_connection(connection_id, raw, credentials))
        return sorted(
            result, key=lambda item: (item.name.casefold(), item.connection_id)
        )

    def resolve(self, identifier: str) -> DBeaverConnection:
        wanted = identifier.strip()
        if not wanted:
            raise ValueError("DBeaver connection name or id is required")
        matches = [
            item
            for item in self.connections()
            if item.connection_id == wanted or item.name == wanted
        ]
        if not matches:
            raise ValueError(f"DBeaver PostgreSQL connection not found: {wanted}")
        if len(matches) > 1:
            raise ValueError(
                f"DBeaver connection name is ambiguous; use the connection id: {wanted}"
            )
        return matches[0]

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

        url = str(configuration.get("url") or "")
        parsed = None
        if url:
            if not url.startswith("jdbc:postgresql://"):
                raise ValueError(
                    f"DBeaver connection is not PostgreSQL: {connection_id}"
                )
            parsed = urlparse(url[5:])
        host = str(
            configuration.get("host") or (parsed.hostname if parsed else "") or ""
        )
        port_raw = (
            configuration.get("port") or (parsed.port if parsed else None) or 5432
        )
        database = str(configuration.get("database") or "")
        if not database and parsed:
            database = unquote(parsed.path.lstrip("/"))
        if not host or not database:
            raise ValueError(
                f"DBeaver connection is missing host or database: {connection_id}"
            )
        try:
            port = int(port_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"DBeaver connection has an invalid port: {connection_id}"
            ) from exc

        properties = configuration.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        query = parse_qs(parsed.query) if parsed else {}
        sslmode = str(
            properties.get("sslmode") or (query.get("sslmode") or [""])[0] or ""
        )
        return DBeaverConnection(
            connection_id=connection_id,
            name=str(raw.get("name") or connection_id),
            provider="postgresql",
            driver=str(raw.get("driver") or "postgres-jdbc"),
            database=database,
            default_schema=str(configuration.get("default-schema") or ""),
            dbeaver_read_only=bool(raw.get("read-only", False)),
            host=host,
            port=port,
            username=username,
            password=password,
            sslmode=sslmode or None,
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
