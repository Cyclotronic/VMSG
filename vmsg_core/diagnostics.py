"""
Diagnostic event channel shared by every gateway emulator.

The traffic feed answers "what went over the wire". This answers "what does
that mean" -- the protocol misuse a client cannot see, the link lifecycle, and
the conditions real hardware would have logged in an error queue. Keeping the
two separate matters: a developer scanning raw traffic for a corrupted read
should not have to pick interpretation out of the same list.

Levels:
    INFO   normal lifecycle -- a client connected, a link was created
    WARN   the client did something real hardware would record as an error
    ERROR  the emulator itself could not honour a request
"""
import time
from typing import Any, Callable, Dict, List, Optional

INFO, WARN, ERROR = "INFO", "WARN", "ERROR"


def timestamp() -> str:
    return time.strftime("%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}"


class DiagnosticEmitter:
    """
    Mixin giving a server a diagnostic channel.

    Subclasses call `self.diagnose(...)`. Listeners are invoked on the emitting
    thread, so a GUI listener must hand off to its own queue rather than touch
    widgets directly.
    """

    def _diagnostic_sinks(self) -> List[Callable[[Dict[str, Any]], None]]:
        if not hasattr(self, "_diag_callbacks"):
            self._diag_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        return self._diag_callbacks

    def add_diagnostic_callback(self, callback: Callable[[Dict[str, Any]], None]):
        sinks = self._diagnostic_sinks()
        if callback not in sinks:
            sinks.append(callback)

    def diagnose(self, level: str, event: str, detail: str = "",
                 address: Optional[int] = None, source: str = "",
                 code: Optional[int] = None, device: str = "",
                 extra: Optional[Dict[str, Any]] = None):
        record = {
            "timestamp": timestamp(),
            "level": level,
            "source": source or getattr(self, "DIAGNOSTIC_SOURCE", "gateway"),
            "address": address,
            "event": event,
            "detail": detail,
            "code": code,
            "device": device,
        }
        # Merged before dispatch: a listener must see the finished record.
        if extra:
            record.update(extra)
        for callback in self._diagnostic_sinks():
            try:
                callback(record)
            except Exception:
                # A failing listener must never take the emulator down with it.
                pass
        return record


#: Plain-language gloss for the SCPI errors the emulators raise. The codes and
#: text are standard; this column is what actually saves a developer time,
#: because the standard wording does not say what to do about it.
ERROR_MEANINGS = {
    -420: "Read issued when the instrument had nothing queued. The client read "
          "before its query produced a reply, or read twice for one query.",
    -410: "New query sent while an earlier reply was still unread. The earlier "
          "reply was discarded, as it is on real hardware.",
    -113: "Query for a node this instrument does not have. Real hardware "
          "answers nothing and logs this.",
}


def format_record(record: Dict[str, Any]) -> str:
    """One diagnostic as a line of text, for the exported log."""
    address = "" if record.get("address") is None else "A:%s" % record["address"]
    parts = [record["timestamp"], "%-5s" % record["level"],
             "%-9s" % record["source"], "%-5s" % address, record["event"]]
    if record.get("detail"):
        parts.append("-- %s" % record["detail"])
    return " ".join(p for p in parts if p is not None)
