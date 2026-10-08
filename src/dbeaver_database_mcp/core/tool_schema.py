"""Protocol-level shapes for database-specific optional tools."""

from .query_schema import obj

LEVELS = [
    "DEBUG5",
    "DEBUG4",
    "DEBUG3",
    "DEBUG2",
    "DEBUG1",
    "INFO",
    "NOTICE",
    "WARNING",
    "ERROR",
    "LOG",
    "FATAL",
    "PANIC",
]
LOG_FILTER_SCHEMA = obj(
    {
        "levels": {
            "type": "array",
            "items": {"enum": LEVELS},
            "minItems": 1,
            "maxItems": len(LEVELS),
            "uniqueItems": True,
        },
        "since": {"type": "string", "minLength": 20, "maxLength": 40},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 65536},
    }
)
