"""Translate saved basic Oracle JDBC settings into Thin-driver parameters."""

import re
from urllib.parse import urlsplit

from ...core.adapter import ConnectionParameters


def parse_configuration(configuration: dict) -> ConnectionParameters:
    properties = configuration.get("properties") or {}
    if not isinstance(properties, dict):
        raise ValueError("DBeaver Oracle properties are invalid")
    mode = str(properties.get("@dbeaver-sid-service@") or "SID").upper()
    connection_type = str(
        properties.get("@dbeaver-connection-type@") or "BASIC"
    ).upper()
    if connection_type not in {"BASIC", "DIRECT", "CUSTOM"}:
        raise ValueError("DBeaver Oracle connection requires an unsupported transport")
    if any(
        any(token in str(key).lower() for token in ("wallet", "ssl", "tns", "kerberos"))
        and value not in (None, "", False, "false")
        for key, value in properties.items()
    ):
        raise ValueError("DBeaver Oracle TLS/wallet authentication is not supported")
    host, port, database = "", 1521, ""
    url = str(configuration.get("url") or "")
    if url:
        prefix = "jdbc:oracle:thin:@"
        if not url.startswith(prefix):
            raise ValueError("DBeaver Oracle URL is not a supported Thin connection")
        target = url[len(prefix) :]
        if target.startswith("//") or "/" in target:
            parsed = urlsplit("oracle://" + target.removeprefix("//"))
            if parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("DBeaver Oracle URL has unsupported options")
            host, port, database = (
                parsed.hostname or "",
                parsed.port or 1521,
                parsed.path.lstrip("/"),
            )
            mode = "SERVICE"
        else:
            match = re.fullmatch(r"([^:/?@()]+):(\d+):([^:/?@()]+)", target)
            if not match:
                raise ValueError("DBeaver Oracle URL is not a basic SID or service URL")
            host, port, database = match.group(1), int(match.group(2)), match.group(3)
            mode = "SID"
    else:
        host = str(configuration.get("host") or "")
        port = int(configuration.get("port") or 1521)
        database = str(configuration.get("database") or "")
    if (
        not re.fullmatch(r"[\w.:-]+", host)
        or not re.fullmatch(r"[\w.$#-]{1,128}", database)
        or not 1 <= port <= 65535
        or mode not in {"SID", "SERVICE"}
    ):
        raise ValueError("DBeaver Oracle host, port or service identifier is invalid")
    return ConnectionParameters(
        host=host,
        port=port,
        database=database,
        default_schema=str(configuration.get("default-schema") or ""),
        options={"sid" if mode == "SID" else "service_name": database},
    )
