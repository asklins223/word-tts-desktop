#!/usr/bin/env python3
"""Local append-only receiver for the Chrome UI-operation recorder.

The browser recorder posts only sanitized operation metadata and page-visible
field values to 127.0.0.1.  The receiver never forwards data elsewhere.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = Path(
    os.environ.get(
        "PLATFORM_INPUT_MONITOR_LOG",
        str(ROOT / ".runtime" / "platform_input_ui_operations_persistent.jsonl"),
    )
).expanduser()
HOST = os.environ.get("PLATFORM_INPUT_MONITOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("PLATFORM_INPUT_MONITOR_PORT", "8765"))
MAX_BODY_BYTES = 4 * 1024 * 1024
WRITE_LOCK = threading.Lock()
SEEN_EVENT_IDS: set[str] = set()


def load_seen_event_ids() -> None:
    """Load client event IDs already persisted so retries stay idempotent."""
    try:
        with LOG_PATH.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    stored = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(stored, dict):
                    continue
                event_id = stored.get("eventId")
                if not event_id and isinstance(stored.get("payload"), dict):
                    event_id = stored["payload"].get("eventId")
                if isinstance(event_id, str) and event_id:
                    SEEN_EVENT_IDS.add(event_id)
    except FileNotFoundError:
        return
    except OSError:
        return


def append_record(record: dict, event_id: str | None = None) -> bool:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with WRITE_LOCK:
        if event_id and event_id in SEEN_EVENT_IDS:
            return False
        with LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        if event_id:
            SEEN_EVENT_IDS.add(event_id)
    return True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class MonitorHandler(BaseHTTPRequestHandler):
    server_version = "PlatformInputMonitor/1.0"

    def _headers(self, status: int, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._headers(204)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self._headers(404)
            self.wfile.write(b'{"ok":false,"error":"not_found"}')
            return
        payload = {"ok": True, "logPath": str(LOG_PATH), "pid": os.getpid()}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(200)
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/event":
            self._headers(404)
            self.wfile.write(b'{"ok":false,"error":"not_found"}')
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self._headers(413)
            self.wfile.write(b'{"ok":false,"error":"invalid_body_size"}')
            return

        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            record_type = payload.get("recordType") or (
                "ui_event" if payload.get("kind") == "ui" else "browser_event"
            )
            event_id = payload.get("eventId")
            if not isinstance(event_id, str) or not event_id:
                event_id = None
            append_record(
                {
                    "recordedAt": now_iso(),
                    "recordType": record_type,
                    "eventId": event_id,
                    "source": "chrome_page",
                    "payload": payload,
                },
                event_id=event_id,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, OSError) as exc:
            self._headers(400)
            body = json.dumps(
                {"ok": False, "error": type(exc).__name__}, ensure_ascii=False
            ).encode("utf-8")
            self.wfile.write(body)
            return

        self._headers(204)

    def log_message(self, format: str, *args: object) -> None:
        # Keep the terminal quiet; the JSONL file is the durable audit trail.
        return


def main() -> None:
    load_seen_event_ids()
    append_record(
        {
            "recordedAt": now_iso(),
            "recordType": "monitor_started",
            "source": "local_receiver",
            "host": HOST,
            "port": PORT,
            "logPath": str(LOG_PATH),
            "persistence": "append_only_jsonl_fsync_per_event",
        }
    )
    server = ThreadingHTTPServer((HOST, PORT), MonitorHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
