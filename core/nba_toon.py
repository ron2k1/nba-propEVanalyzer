"""TOON (Token-Oriented Object Notation) serializer for LLM prompt payloads.

Compact format that cuts token usage 30-60% on tabular/structured data
while remaining LLM-parseable. Two output shapes:

  Object:  key: value (one per line)
  Table:   header | header | ... then data | data | ...
"""


def to_toon_object(d: dict) -> str:
    """Convert a flat dict to TOON object format (key: value, one per line).

    Nested dicts/lists are serialized inline as compact strings.
    None values are rendered as empty string.
    """
    if not d:
        return ""
    lines = []
    for k, v in d.items():
        if v is None:
            v_str = ""
        elif isinstance(v, dict):
            v_str = " ".join(f"{sk}={sv}" for sk, sv in v.items())
        elif isinstance(v, (list, tuple)):
            v_str = ", ".join(str(item) for item in v)
        elif isinstance(v, float):
            v_str = f"{v:.4g}"
        elif isinstance(v, bool):
            v_str = "yes" if v else "no"
        else:
            v_str = str(v)
        lines.append(f"{k}: {v_str}")
    return "\n".join(lines)


def to_toon_table(rows: list[dict], columns: list[str] | None = None) -> str:
    """Convert a list of dicts to TOON table (header row + pipe-delimited data).

    Args:
        rows: List of dicts with consistent keys.
        columns: Optional column whitelist/order. If None, uses keys from first row.

    Returns:
        String with header row and data rows separated by ' | '.
        Empty string if rows is empty.
    """
    if not rows:
        return ""
    cols = columns or list(rows[0].keys())

    def _fmt(v) -> str:
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:.4g}"
        if isinstance(v, bool):
            return "yes" if v else "no"
        return str(v)

    header = " | ".join(cols)
    data_lines = []
    for row in rows:
        data_lines.append(" | ".join(_fmt(row.get(c)) for c in cols))
    return header + "\n" + "\n".join(data_lines)


def toon_print_section(title: str, body: str) -> None:
    """Print a titled TOON section to stdout."""
    print(f"\n=== {title} ===")
    print(body)
    print()
