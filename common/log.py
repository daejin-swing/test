import os
import sys
import json
import time
import copy
import socket
import logging
import traceback
import threading
from logging.handlers import RotatingFileHandler
from collections import OrderedDict
from contextlib import contextmanager

from common.config import LOG_ROOT, CRASH_LOG_ROOT, ensure_directories, get_device_id
from common.params import Params

LOG_TIMESTAMPS = "LOG_TIMESTAMPS" in os.environ


def json_handler(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return repr(obj)


def json_robust_dumps(obj):
    return json.dumps(obj, default=json_handler)


class NiceOrderedDict(OrderedDict):
    def __str__(self):
        return json_robust_dumps(self)


class JsonLinesFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": record.created,
            "level": record.levelname,
            "module": record.filename,
            "lineno": record.lineno,
            "func": record.funcName,
            "message": record.getMessage(),
        }
        if record.exc_info:
            data["traceback"] = self.formatException(record.exc_info)
        if hasattr(record, "context"):
            data["context"] = record.context
        return json.dumps(data, default=json_handler)


class CloudLogger(logging.Logger):
    def __init__(self):
        super().__init__("cloudlog")
        self.setLevel(logging.DEBUG)

        self.global_ctx = {}
        self.log_local = threading.local()

        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] (%(filename)s:%(lineno)d): %(message)s")
        console_handler.setFormatter(console_formatter)
        self.addHandler(console_handler)

        # File handler (10MB x 3 rolling files)
        try:
            ensure_directories()
            app_log_path = os.path.join(LOG_ROOT, "app.log")
            file_handler = RotatingFileHandler(app_log_path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(JsonLinesFormatter())
            self.addHandler(file_handler)
        except Exception as e:
            sys.stderr.write(f"Failed to setup CloudLogger file handler: {e}\n")

    def local_ctx(self):
        if not hasattr(self.log_local, "ctx"):
            self.log_local.ctx = {}
        return self.log_local.ctx

    def get_ctx(self):
        return dict(self.local_ctx(), **self.global_ctx)

    @contextmanager
    def ctx(self, **kwargs):
        old_ctx = self.local_ctx()
        self.log_local.ctx = copy.copy(old_ctx) or {}
        self.log_local.ctx.update(kwargs)
        try:
            yield
        finally:
            self.log_local.ctx = old_ctx

    def bind(self, **kwargs):
        self.local_ctx().update(kwargs)

    def bind_global(self, **kwargs):
        self.global_ctx.update(kwargs)

    def event(self, event_name, *args, **kwargs):
        evt = NiceOrderedDict()
        evt['event'] = event_name
        if args:
            evt['args'] = args
        evt.update(kwargs)
        if 'error' in kwargs:
            self.error(json_robust_dumps(evt), extra={"context": self.get_ctx()})
        elif 'debug' in kwargs:
            self.debug(json_robust_dumps(evt), extra={"context": self.get_ctx()})
        else:
            self.info(json_robust_dumps(evt), extra={"context": self.get_ctx()})


cloudlog = log = CloudLogger()


def save_crash_dump(exc_type, exc_value, exc_traceback, thread_name: str = "main"):
    """Saves unhandled exception to a crash dump JSON file for immediate server upload."""
    try:
        ensure_directories()
        timestamp = time.time()
        pid = os.getpid()
        filename = f"crash_{int(timestamp)}_{pid}.json"
        filepath = os.path.join(CRASH_LOG_ROOT, filename)

        tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        params = Params()

        crash_data = {
            "device_id": get_device_id(),
            "log_type": "crash",
            "entries": [{
                "timestamp": timestamp,
                "level": "CRITICAL",
                "module": sys.argv[0] if sys.argv else "unknown",
                "message": f"Fatal Crash in thread [{thread_name}]: {exc_type.__name__}: {exc_value}",
                "traceback": tb_str,
                "context": {
                    "pid": pid,
                    "target_branch": params.get("UpdaterTargetBranch"),
                    "git_commit": params.get("GitCommit") or "unknown",
                }
            }]
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(crash_data, f, indent=2)

        cloudlog.critical(f"Crash dump saved to {filepath}\n{tb_str}")
    except Exception as e:
        sys.stderr.write(f"Failed to write crash dump: {e}\n")


def install_crash_handler():
    """Installs global exception hooks to capture all unhandled exceptions."""
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        save_crash_dump(exc_type, exc_value, exc_traceback, thread_name="main")
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    def handle_thread_exception(args):
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        save_crash_dump(args.exc_type, args.exc_value, args.exc_traceback, thread_name=args.thread.name)

    sys.excepthook = handle_exception
    if hasattr(threading, "excepthook"):
        threading.excepthook = handle_thread_exception


# Automatically install crash handler when log module is imported
install_crash_handler()