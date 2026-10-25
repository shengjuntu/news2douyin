from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger


class _InterceptHandler(logging.Handler):
    """Redirect stdlib logging records to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except Exception:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        # walk back to find caller outside logging module
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logger(
    log_dir: str | Path,
    level: str = "INFO",
    *,
    intercept_std_logging: bool = True,
) -> Path:
    """Configure loguru sinks (console + rotating file). Return log file path."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"pipeline_{datetime.now():%Y%m%d_%H%M%S}.log"

    logger.remove()

    logger.add(
        sys.stdout,
        level=level,
        colorize=True,
        backtrace=False,
        diagnose=False,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level:<8}</level> | "
            "<cyan>{module}:{function}:{line}</cyan> | "
            "<level>{message}</level>"
            " <blue>{extra}</blue>"
        ),
    )

    logger.add(
        log_file,
        level=level,
        rotation="20 MB",
        retention="14 days",
        compression="zip",
        enqueue=True,
        backtrace=True,
        diagnose=False,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
            "{process}:{thread} | {module}:{function}:{line} | "
            "{message} | extra={extra}"
        ),
    )

    if intercept_std_logging:
        logging.basicConfig(handlers=[_InterceptHandler()], level=logging.getLevelName(level))
        for name in ("urllib3", "httpx", "requests", "openai"):
            logging.getLogger(name).handlers = [ _InterceptHandler() ]
            logging.getLogger(name).propagate = False

    logger.info("Logger initialized")
    logger.info(f"Log file: {log_file}")
    return log_file
