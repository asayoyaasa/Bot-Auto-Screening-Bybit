import logging
import logging.handlers
import os
import re

try:
    from pythonjsonlogger import jsonlogger
except Exception:  # pragma: no cover - optional dependency fallback
    jsonlogger = None


LOG_DIR = 'logs'
ROOT_MARKER = '_bot_root_logging_configured'
LOGGER_MARKER = '_bot_logger_configured'


class PIIMaskFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.secret_pattern = re.compile(r'(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*[^,\s]+')
        self.hash_pattern = re.compile(r'\b[a-zA-Z0-9]{32,}\b')

    def filter(self, record):
        try:
            if isinstance(record.msg, str):
                record.msg = self.secret_pattern.sub(r'\1=***REDACTED***', record.msg)
                record.msg = self.hash_pattern.sub('***REDACTED***', record.msg)
        except Exception:
            pass
        return True


class JsonFallbackFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            'timestamp': self.formatTime(record, self.datefmt),
            'level': record.levelname,
            'name': record.name,
            'message': record.getMessage(),
        }
        return str(payload)


def ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def setup_root_logging(level=logging.INFO):
    root = logging.getLogger()
    if getattr(root, ROOT_MARKER, False):
        return root

    ensure_log_dir()
    root.setLevel(level)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    setattr(root, ROOT_MARKER, True)
    return root


def _make_file_formatter(json_format=False):
    if json_format and jsonlogger is not None:
        return jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s', rename_fields={'asctime': 'timestamp'})
    if json_format:
        return JsonFallbackFormatter()
    return logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')


def build_component_logger(name, log_file, *, level=logging.INFO, json_format=False, pii_mask=False):
    setup_root_logging(level=level)
    logger = logging.getLogger(name)
    if getattr(logger, LOGGER_MARKER, False):
        return logger

    logger.setLevel(level)
    logger.propagate = True

    if log_file:
        ensure_log_dir()
        handler = logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, log_file), maxBytes=2_000_000, backupCount=5)
        handler.setFormatter(_make_file_formatter(json_format=json_format))
        if pii_mask:
            handler.addFilter(PIIMaskFilter())
        logger.addHandler(handler)

    setattr(logger, LOGGER_MARKER, True)
    return logger
