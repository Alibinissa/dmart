import json
import logging
import logging.config
from utils.settings import settings


class CustomFormatter(logging.Formatter):
    def format(self, record):
        data = {
            'time': self.formatTime(record),
            'level': record.levelname,
            'message': record.getMessage(),
            'props': getattr(record, "props", ""),
            "thread": record.threadName,
            "process": record.process,
            "pathname": record.pathname,
            "lineno": record.lineno,
            "funcName": record.funcName,
        }
        return json.dumps(data)


logging_schema = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {
            '()': CustomFormatter
        }
    },
    'handlers': {
       'console': { 
            'class': 'logging.StreamHandler',
            'level': 'INFO',
            'formatter': 'json',
            'stream': 'ext://sys.stdout',  # Default is stderr
        },
        'file': {
            'class': 'concurrent_log_handler.ConcurrentRotatingFileHandler',
            'filename': settings.log_file,
            'backupCount': 5,
            'maxBytes': 1048576,
            'use_gzip': True,
            'formatter': 'json'
        }
    },
    'loggers': {
        'fastapi': {
            'handlers': settings.log_handlers,
            'level': logging.INFO,
            'propagate': True
        }
    }
}

def changeLogFile(log_file: str | None = None) -> None:
    global logging_schema
    if log_file:
        logging_schema["handlers"]["file"]["filename"] = log_file
