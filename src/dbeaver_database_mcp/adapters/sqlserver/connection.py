"""Normalize saved Microsoft JDBC direct connections, never caller transport input."""

import re

from ...core.adapter import ConnectionParameters


def parse_configuration(configuration: dict) -> ConnectionParameters:
    properties = {}
    url = str(configuration.get("url") or "")
    endpoint = ""
    if url:
        prefix = "jdbc:sqlserver://"
        if not url.startswith(prefix):
            raise ValueError("DBeaver SQL Server URL is unsupported")
        endpoint, *parts = url[len(prefix) :].split(";")
        for part in parts:
            if not part:
                continue
            key, separator, value = part.partition("=")
            if not separator or "{" in value or "}" in value:
                raise ValueError("DBeaver SQL Server URL options are unsupported")
            properties[key.lower()] = value
    for category in ("properties", "provider-properties"):
        saved = configuration.get(category) or {}
        if not isinstance(saved, dict):
            raise ValueError("DBeaver SQL Server properties are invalid")
        properties.update({str(k).lower(): v for k, v in saved.items()})
    allowed = {
        "servername",
        "portnumber",
        "databasename",
        "encrypt",
        "trustservercertificate",
        "ssltrustservercertificate",
        "sslencrypt",
        "integratedsecurity",
        "authentication",
        "applicationintent",
        "@dbeaver-show-all-schemas@",
        "@dbeaver-show-all-databases@",
    }
    if set(properties) - allowed:
        raise ValueError("DBeaver SQL Server has unsupported driver options")
    if str(configuration.get("auth-model") or "sqlserver_database") not in {
        "sqlserver_database",
        "native",
        "database",
    }:
        raise ValueError("DBeaver SQL Server authentication is unsupported")
    if str(properties.get("integratedsecurity", "false")).lower() != "false":
        raise ValueError("DBeaver SQL Server integrated authentication is unsupported")
    if str(properties.get("authentication", "SqlPassword")).lower() != "sqlpassword":
        raise ValueError("DBeaver SQL Server authentication is unsupported")
    # Do not silently downgrade explicit TLS, trust stores or certificate settings.
    # Only saved, non-TLS direct connections are supported.
    for key in ("encrypt", "sslencrypt"):
        if str(properties.get(key, "false")).lower() != "false":
            raise ValueError("DBeaver SQL Server TLS configuration is unsupported")
    if endpoint and not re.fullmatch(r"[\w.\-]*(?::\d+)?", endpoint):
        raise ValueError(
            "DBeaver SQL Server named instances or endpoint are unsupported"
        )
    host_part, _, port_part = endpoint.partition(":")
    host = str(properties.get("servername") or configuration.get("host") or host_part)
    port = int(
        properties.get("portnumber", configuration.get("port", port_part or 1433))
    )
    database = str(
        properties.get("databasename") or configuration.get("database") or "master"
    )
    if not re.fullmatch(r"[\w.\-]+", host) or not 1 <= port <= 65535:
        raise ValueError("DBeaver SQL Server host or port is invalid")
    if not database or len(database) > 128 or "\x00" in database:
        raise ValueError("DBeaver SQL Server database is invalid")
    return ConnectionParameters(
        host=host,
        port=port,
        database=database,
        default_schema=str(configuration.get("default-schema") or ""),
    )
