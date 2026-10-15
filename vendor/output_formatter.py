"""Output formatters for JSON, CSV, and table formats."""

import csv
import json
import sys
from typing import Any, List, Dict, Union


def flatten_object(obj: Any, prefix: str = "") -> Dict[str, Any]:
    """Recursively flatten nested objects for CSV/table display."""
    result = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            new_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, (dict, list)):
                result.update(flatten_object(value, new_key))
            else:
                result[new_key] = value
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            new_key = f"{prefix}[{i}]"
            if isinstance(item, (dict, list)):
                result.update(flatten_object(item, new_key))
            else:
                result[new_key] = item
    else:
        result[prefix] = obj
    return result


def format_output(data: Union[dict, list], output_format: str = "json") -> str:
    """Format data as JSON, CSV, or table. Returns formatted string."""
    if output_format == "json":
        return json.dumps(data, indent=2)

    # Ensure data is a list for CSV/table
    rows = data if isinstance(data, list) else [data]
    if not rows:
        return "(no results)" if output_format == "table" else ""

    # Flatten nested objects
    flat_rows = [flatten_object(row) if isinstance(row, dict) else {"value": row} for row in rows]
    columns = sorted(set(col for row in flat_rows for col in row.keys()))

    if output_format == "csv":
        return _format_csv(flat_rows, columns)
    elif output_format == "table":
        return _format_table(flat_rows, columns)
    else:
        raise ValueError(f"Unknown format: {output_format}")


def _format_csv(rows: List[Dict[str, Any]], columns: List[str]) -> str:
    """Format rows as CSV string."""
    lines = []

    # Header
    lines.append(",".join(_csv_escape(col) for col in columns))

    # Rows
    for row in rows:
        values = [str(row.get(col, "")) for col in columns]
        lines.append(",".join(_csv_escape(val) for val in values))

    return "\n".join(lines)


def _format_table(rows: List[Dict[str, Any]], columns: List[str]) -> str:
    """Format rows as ASCII table string."""
    if not rows or not columns:
        return "(no results)"

    # Convert all values to strings and calculate widths
    str_rows = []
    widths = {col: min(len(col), 40) for col in columns}

    for row in rows:
        str_row = {}
        for col in columns:
            val = str(row.get(col, ""))
            str_row[col] = val
            widths[col] = min(max(widths[col], len(val)), 40)
        str_rows.append(str_row)

    lines = []

    # Header
    header = "  ".join(col.ljust(widths[col]) for col in columns)
    lines.append(header)
    lines.append("  ".join("─" * widths[col] for col in columns))

    # Rows
    for row in str_rows:
        cells = []
        for col in columns:
            val = row[col]
            if len(val) > widths[col]:
                val = val[:widths[col] - 1] + "…"
            cells.append(val.ljust(widths[col]))
        lines.append("  ".join(cells))

    return "\n".join(lines)


def _csv_escape(value: str) -> str:
    """Escape CSV value: wrap in quotes if contains comma, newline, or quote."""
    value = str(value)
    if "," in value or "\n" in value or '"' in value:
        escaped = value.replace('"', '""')
        return f'"{escaped}"'
    return value


def print_output(data: Union[dict, list], output_format: str = "json", file=None) -> None:
    """Print formatted output to stdout or specified file."""
    if file is None:
        file = sys.stdout
    formatted = format_output(data, output_format)
    print(formatted, file=file)
