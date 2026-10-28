from __future__ import annotations

import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {
            "t": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "data", None)
        if extra:
            d["data"] = extra
        return json.dumps(d)


def setup_logging(level: str = "INFO", json_logs: bool = False, logfile: str | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    h = logging.StreamHandler(sys.stdout)
    if json_logs:
        h.setFormatter(JsonFormatter())
    else:
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(h)
    if logfile:
        fh = logging.FileHandler(logfile)
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
