"""Resolve the source once so routing and execution obey the same precedence."""

import re

from utils.demo_metadata import metadata_request
from utils.etl_tools import requests_artifact_transform, referenced_demo_tables, requests_database_etl
from utils.safe_fetch import source_url


def select_source(message: str, source_mode: str = "auto") -> tuple[str, str]:
    if source_url(message):
        return "url", "explicit_url"
    if requests_artifact_transform(message):
        return "artifact", "explicit_file_reference"
    if metadata_request(message) or re.search(r"\b(?:db|database|neon|neondb)\b"
                                            r"|\b(?:users?|vehicles?|rides?|payments?|ratings?)\s+table\b", message, re.I):
        return "database", "explicit_database_reference"
    if source_mode == "artifact":
        return "artifact", "explicit_ui_selection"
    if referenced_demo_tables(message) or requests_database_etl(message):
        return "database", "demo_table_reference"
    return "unspecified", "no_explicit_source"
