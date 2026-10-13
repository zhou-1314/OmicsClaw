"""Link a chat session to its durable Run and telemetry identifiers."""

import logging
import sqlite3
from pathlib import Path
from typing import Any

from omicsclaw.runs.projections import ensure_session_run
from omicsclaw.runs.store import RunStore

_log = logging.getLogger(__name__)


def project_session_run(app: Any, session_id: str) -> str:
    if not session_id:
        return ""
    store = None
    try:
        path = Path(app.config.workspace) / ".omicsclaw" / "runs.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        store = RunStore(path, on_event=app.telemetry.run_event_sink())
        return ensure_session_run(store, session_id, plans=app.plans).run_id
    except (sqlite3.Error, OSError):
        _log.warning(
            "session Run projection unavailable; chat remains active", exc_info=True
        )
        return ""
    finally:
        if store is not None:
            store.db.close()
