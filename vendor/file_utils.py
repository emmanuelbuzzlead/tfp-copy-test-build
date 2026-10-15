"""File utilities for loading lists from CSV/text files."""

import os
from typing import List, Set


def load_domains_from_file(filepath: str) -> List[str]:
    """Load domains from CSV or text file (one per line or comma-separated)."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    domains = set()

    # Try CSV format first (comma-separated values)
    for line in content.strip().split("\n"):
        # Each line might have multiple comma-separated values
        for value in line.split(","):
            val = value.strip().lower()
            if val and not val.startswith("#"):  # Skip empty and comments
                domains.add(val)

    return sorted(list(domains))


def load_list_from_file(filepath: str, lowercase: bool = True) -> List[str]:
    """Load a list of values from CSV or text file (one per line or comma-separated)."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    items = set()

    # Parse comma-separated or newline-separated values
    for line in content.strip().split("\n"):
        for value in line.split(","):
            val = value.strip()
            if lowercase:
                val = val.lower()
            if val and not val.startswith("#"):  # Skip empty and comments
                items.add(val)

    return sorted(list(items))
