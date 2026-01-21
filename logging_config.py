"""
Logging configuration for Vasili.

Provides structured logging with JSON format, configurable log levels,
and file output with rotation.

Usage:
    from logging_config import setup_logging, get_logger

    # Call once at application startup
    setup_logging()

    # Get a logger for your module
    logger = get_logger(__name__)
    logger.info('Application started', extra={'component': 'main'})

Environment variables:
    VASILI_LOG_LEVEL: Set log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    VASILI_LOG_FILE: Path to log file (default: /var/log/vasili/vasili.log)
    VASILI_LOG_FORMAT: 'json' for structured logging, 'text' for human-readable
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


class JsonFormatter(logging.Formatter):
    """
    Formats log records as JSON for structured logging.

    Output includes timestamp, level, logger name, message, and any extra fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }

        # Add location info
        if record.pathname:
            log_data['location'] = {
                'file': record.pathname,
                'line': record.lineno,
                'function': record.funcName,
            }

        # Add exception info if present
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)

        # Add any extra fields passed via the 'extra' parameter
        # Skip standard LogRecord attributes
        standard_attrs = {
            'name',
            'msg',
            'args',
            'created',
            'filename',
            'funcName',
            'levelname',
            'levelno',
            'lineno',
            'module',
            'msecs',
            'pathname',
            'process',
            'processName',
            'relativeCreated',
            'stack_info',
            'exc_info',
            'exc_text',
            'thread',
            'threadName',
            'taskName',
            'message',
        }
        extra_fields = {
            k: v
            for k, v in record.__dict__.items()
            if k not in standard_attrs and not k.startswith('_')
        }
        if extra_fields:
            log_data['extra'] = extra_fields

        return json.dumps(log_data)


class TextFormatter(logging.Formatter):
    """
    Human-readable formatter with consistent structure.

    Format: TIMESTAMP | LEVEL | LOGGER | MESSAGE [extra fields]
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        # Build base message
        base = f'{timestamp} | {record.levelname:8} | {record.name:20} | {record.getMessage()}'

        # Add extra fields if present
        standard_attrs = {
            'name',
            'msg',
            'args',
            'created',
            'filename',
            'funcName',
            'levelname',
            'levelno',
            'lineno',
            'module',
            'msecs',
            'pathname',
            'process',
            'processName',
            'relativeCreated',
            'stack_info',
            'exc_info',
            'exc_text',
            'thread',
            'threadName',
            'taskName',
            'message',
        }
        extra_fields = {
            k: v
            for k, v in record.__dict__.items()
            if k not in standard_attrs and not k.startswith('_')
        }
        if extra_fields:
            extra_str = ' '.join(f'{k}={v}' for k, v in extra_fields.items())
            base = f'{base} [{extra_str}]'

        # Add exception info if present
        if record.exc_info:
            base = f'{base}\n{self.formatException(record.exc_info)}'

        return base


def get_log_level(default: str = 'INFO') -> int:
    """
    Get log level from environment variable.

    Args:
        default: Default level if VASILI_LOG_LEVEL is not set

    Returns:
        Logging level constant (e.g., logging.INFO)
    """
    level_name = os.environ.get('VASILI_LOG_LEVEL', default).upper()
    level_map = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'WARN': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL,
    }
    return level_map.get(level_name, logging.INFO)


def get_formatter() -> logging.Formatter:
    """
    Get formatter based on VASILI_LOG_FORMAT environment variable.

    Returns:
        JsonFormatter if format is 'json', TextFormatter otherwise
    """
    log_format = os.environ.get('VASILI_LOG_FORMAT', 'text').lower()
    if log_format == 'json':
        return JsonFormatter()
    return TextFormatter()


def setup_logging(
    level: Optional[int] = None,
    log_file: Optional[str] = None,
    log_format: Optional[str] = None,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> None:
    """
    Configure logging for the application.

    Sets up both console and file handlers with appropriate formatters.
    Should be called once at application startup.

    Args:
        level: Log level (default: from VASILI_LOG_LEVEL env var or INFO)
        log_file: Path to log file (default: from VASILI_LOG_FILE env var)
        log_format: 'json' or 'text' (default: from VASILI_LOG_FORMAT env var)
        max_bytes: Maximum size of each log file before rotation
        backup_count: Number of backup files to keep
    """
    # Determine settings from args or environment
    if level is None:
        level = get_log_level()

    if log_format:
        os.environ['VASILI_LOG_FORMAT'] = log_format
    formatter = get_formatter()

    if log_file is None:
        log_file = os.environ.get('VASILI_LOG_FILE')

    # Get root logger for vasili
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # Console handler - always enabled
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler - if log file is specified
    if log_file:
        try:
            # Ensure log directory exists
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except (OSError, PermissionError) as e:
            # Log to console if file handler fails
            root_logger.warning(
                f'Could not set up file logging to {log_file}: {e}',
                extra={'component': 'logging_config'},
            )


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for the given name.

    This is a convenience function that ensures consistent logger naming.

    Args:
        name: Logger name (typically __name__ of the calling module)

    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)
