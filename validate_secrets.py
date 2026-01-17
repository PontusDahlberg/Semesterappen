"""Validate Streamlit secrets TOML without printing secret values.

Usage:
  python validate_secrets.py .streamlit/secrets.toml

Exit codes:
  0: OK
  2: File missing
  3: TOML parse error
"""

from __future__ import annotations

import sys
from pathlib import Path

# Prefer stdlib tomllib (py3.11+), else fall back to the 'toml' package
_parse = None
_decode_error = Exception

try:  # py3.11+
    import tomllib as _tomllib  # type: ignore

    _parse = lambda s: _tomllib.loads(s)  # noqa: E731
    _decode_error = _tomllib.TOMLDecodeError
except Exception:  # pragma: no cover
    import toml as _toml  # streamlit already depends on this

    _parse = lambda s: _toml.loads(s)  # noqa: E731
    _decode_error = _toml.TomlDecodeError


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".streamlit/secrets.toml")
    if not path.exists():
        print(f"Missing file: {path}")
        return 2

    raw = path.read_bytes()

    try:
        data = _parse(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        print(f"UTF-8 decode error: {e}")
        return 3
    except _decode_error as e:
        # Important: don't print the line content, only location + message.
        line = getattr(e, "lineno", "?")
        col = getattr(e, "colno", "?")
        msg = getattr(e, "msg", "TOML parse error")
        print(f"TOML parse error at {path}:{line}:{col}: {msg}")
        return 3

    top_keys = sorted(list(data.keys()))
    print("TOML OK")
    print("Top-level keys:")
    for k in top_keys:
        if k == "gcp_service_account":
            v = data.get(k)
            subkeys = sorted(list(v.keys())) if isinstance(v, dict) else []
            print(f"- {k} (subkeys: {', '.join(subkeys)})")
        else:
            print(f"- {k}")

    # Lightweight checks
    missing = []
    if "drive_folder_id" not in data:
        missing.append("drive_folder_id")
    if "gcp_service_account" not in data:
        missing.append("gcp_service_account")
    if missing:
        print("Warning: missing expected keys: " + ", ".join(missing))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
