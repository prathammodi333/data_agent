"""Recognize explicit requests for the approved demo database's structure."""

import re

from utils.database import DEMO_TABLES


_SCOPE = r"(?:in|of|for) (?:the )?(?:db|database)"
_TABLES = re.compile(
    rf"(?:what|which) tables (?:are available|are there|exist)(?: {_SCOPE})?(?: now)?"
    rf"|(?:what|which) tables are in (?:the )?(?:db|database)(?: now)?"
    rf"|(?:list|show(?: me)?) (?:the )?(?:available |demo )?tables(?: {_SCOPE})?",
    re.IGNORECASE,
)
_TABLE_COUNT = re.compile(
    r"(?:how many|what(?:'s| is) the|give(?: me)? the) (?:demo )?tables"
    r"(?: are there)?(?: in (?:the )?(?:db|database))?(?: now)?"
    r"|(?:number|count) of (?:demo )?tables(?: in (?:the )?(?:db|database))?",
    re.IGNORECASE,
)
_ALL_SCHEMA = re.compile(
    r"(?:show(?: me)?|list|describe|what is|what's) (?:the )?"
    r"(?:schema|columns|structure) (?:of|for|in) (?:each (?:demo )?table|all (?:the )?(?:demo )?tables)"
    r"|(?:show(?: me)?|list|describe) (?:the )?(?:db|database) schema"
    r"|(?:what is|what's) (?:the )?(?:db|database) schema",
    re.IGNORECASE,
)
_ONE_SCHEMA = re.compile(
    r"(?:show(?: me)?|list|describe|what is|what's) (?:the )?"
    r"(?:schema|columns|structure) (?:of|for|in) (?:the )?(?:public\.)?(?P<table_a>[a-z_]+)(?: table)?"
    r"|(?:show(?: me)?|describe) (?:the )?(?:public\.)?(?P<table_b>[a-z_]+) (?:table )?(?:schema|columns|structure)"
    r"|describe (?:the )?(?:public\.)?(?P<table_d>[a-z_]+)(?: table)?"
    r"|(?:what|which) columns are in (?:the )?(?:public\.)?(?P<table_c>[a-z_]+)(?: table)?",
    re.IGNORECASE,
)


def metadata_request(question: str) -> tuple[str, str | None] | None:
    """Return a permitted metadata operation only for an entire clear request."""
    normalized = " ".join(question.strip().split()).rstrip("?.!")
    if _TABLE_COUNT.fullmatch(normalized):
        return "table_count", None
    if _TABLES.fullmatch(normalized):
        return "tables", None
    if _ALL_SCHEMA.fullmatch(normalized):
        return "schema", None
    match = _ONE_SCHEMA.fullmatch(normalized)
    if match:
        table = next(value for value in match.groupdict().values() if value).lower()
        if table in DEMO_TABLES:
            return "schema", table
    return None
