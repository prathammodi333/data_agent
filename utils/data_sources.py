"""Parse bounded public tabular data without evaluating source content."""

import csv
import io
import json

import pandas as pd

from utils.errors import ETLRejected

MAX_SOURCE_ROWS = 5000
MAX_COLUMNS = 100


def validate_frame(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) > MAX_SOURCE_ROWS or len(df.columns) > MAX_COLUMNS:
        raise ETLRejected("Source exceeds 5,000 rows or 100 columns. Use a smaller data endpoint.")
    if not len(df.columns) or df.columns.duplicated().any():
        raise ETLRejected("Source needs a nonempty set of unique column names.")
    if any(len(str(column)) > 120 for column in df.columns):
        raise ETLRejected("Source column names are too long.")
    df.columns = [str(column) for column in df.columns]
    return df


def parse_data(body: bytes, content_type: str, records_path: str | None = None) -> pd.DataFrame:
    try:
        text = body.decode("utf-8-sig").strip()
        if not text or text.startswith("<"):
            raise ETLRejected("The source must contain UTF-8 JSON, JSON Lines, CSV, or TSV data.")
        if text.startswith(("[", "{")):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = [json.loads(line) for line in text.splitlines() if line.strip()]
            if records_path:
                for key in records_path.split("."):
                    payload = payload[key]
            elif isinstance(payload, dict):
                candidates = []
                def find_arrays(value, path="", depth=0):
                    if depth > 5:
                        return
                    if isinstance(value, list):
                        candidates.append((path, value))
                    elif isinstance(value, dict):
                        for key, nested in value.items():
                            find_arrays(nested, f"{path}.{key}".lstrip("."), depth + 1)
                find_arrays(payload)
                if len(candidates) == 1:
                    payload = candidates[0][1]
                elif len(candidates) > 1:
                    paths = ", ".join(path for path, _ in candidates[:8])
                    raise ETLRejected(f"JSON has multiple record arrays ({paths}). Add 'records path: data.items' using the desired path.")
                else:
                    payload = [payload]
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                raise ETLRejected("JSON records must be an array of objects or one object.")
            if not payload:
                raise ETLRejected("The source array is empty and has no column schema.")
            if len(payload) > MAX_SOURCE_ROWS:
                raise ETLRejected("Source exceeds 5,000 rows. Use a smaller data endpoint.")
            df = pd.json_normalize(payload, max_level=4)
            # Preserve nested lists/objects as values; never fetch nested URLs.
            for column in df.columns:
                df[column] = df[column].map(lambda value: json.dumps(value) if isinstance(value, (dict, list)) else value)
            return validate_frame(df)
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;")
        header = next(csv.reader(io.StringIO(text), dialect=dialect))
        if len(set(header)) != len(header) or any(not name.strip() for name in header):
            raise ETLRejected("CSV column names must be nonempty and unique.")
        return validate_frame(pd.read_csv(io.StringIO(text), sep=dialect.delimiter, nrows=MAX_SOURCE_ROWS + 1))
    except ETLRejected:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, csv.Error, RecursionError):
        raise ETLRejected("Could not parse the source as UTF-8 tabular JSON, JSON Lines, CSV, or TSV.") from None
