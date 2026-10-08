from urllib.parse import parse_qs, unquote, urlparse

from ...core.adapter import ConnectionParameters


def parse_configuration(configuration: dict) -> ConnectionParameters:
    url = str(configuration.get("url") or "")
    parsed = None
    if url:
        if not url.startswith("jdbc:postgresql://"):
            raise ValueError("DBeaver connection is not PostgreSQL")
        parsed = urlparse(url[5:])
    host = str(configuration.get("host") or (parsed.hostname if parsed else "") or "")
    port_raw = configuration.get("port") or (parsed.port if parsed else None) or 5432
    database = str(configuration.get("database") or "")
    if not database and parsed:
        database = unquote(parsed.path.lstrip("/"))
    if not host or not database:
        raise ValueError("DBeaver connection is missing host or database")
    try:
        port = int(port_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("DBeaver connection has an invalid port") from exc

    properties = configuration.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    query = parse_qs(parsed.query) if parsed else {}
    sslmode = str(properties.get("sslmode") or (query.get("sslmode") or [""])[0] or "")
    if not 1 <= port <= 65535:
        raise ValueError("DBeaver connection has an invalid port")
    return ConnectionParameters(
        host=host,
        port=port,
        database=database,
        default_schema=str(configuration.get("default-schema") or ""),
        sslmode=sslmode or None,
    )
