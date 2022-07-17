# -*- coding: utf-8 -*-

import logging.config
from datetime import datetime
from logging import LogRecord
from pathlib import Path
from typing import Optional

import yaml
from colorlog import ColoredFormatter

_MSEC_FORMAT = "%s,%03d"

BASE_DIR = Path(__file__).resolve().parent.parent


def setup_logging():
    logging_config_file = BASE_DIR / "logging.yaml"
    if logging_config_file.is_file():
        with logging_config_file.open("rt", encoding="utf8") as f:
            logging.config.dictConfig(yaml.safe_load(f))


class HighlightingFormatter(ColoredFormatter):
    def formatTime(self, record: LogRecord, datefmt: Optional[str] = None) -> str:
        dt = datetime.fromtimestamp(record.created).astimezone()
        if datefmt:
            s = dt.strftime(datefmt)
        else:  # pragma: no cover
            t = dt.strftime(self.default_time_format)
            if self.default_msec_format:
                s = self.default_msec_format % (t, record.msecs)
            else:
                s = _MSEC_FORMAT % (t, record.msecs)
        return s
