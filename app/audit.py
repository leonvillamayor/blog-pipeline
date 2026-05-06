"""Audit log de acciones realizadas desde la GUI.

Formato: JSONL en /opt/blog-pipeline/data/audit.log (writable bajo
ReadWritePaths del systemd unit). Cada línea es un evento. Idempotente:
solo append.

Cf-Access-Authenticated-User-Email: header inyectado por Cloudflare
Access tras OTP. Lo capturamos para saber qué autor hizo qué.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def log_event(audit_path: Path, **fields: Any) -> None:
    """Append una entrada JSON al audit log. No lanza si falla escritura."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **fields,
    }
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        # No queremos fallar acciones por audit fallido. Logging stdout y seguir.
        import sys
        sys.stderr.write(f"AUDIT FAIL: {entry}\n")


def read_recent(audit_path: Path, limit: int = 20) -> list[dict]:
    """Devuelve los últimos `limit` eventos (más recientes primero)."""
    if not audit_path.exists():
        return []
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out
