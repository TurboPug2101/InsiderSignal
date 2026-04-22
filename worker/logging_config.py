"""Structured JSON logging using structlog."""

import logging
import sys
import structlog


def configure_logging(service_name: str = "worker", level: str = "INFO"):
    """
    Set up structlog to emit structured JSON log lines.
    Every log call produces a JSON object with:
      - timestamp, level, service, event, and any extra key/value pairs passed by the caller
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Configure stdlib logging to go through structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_logger_name,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )

    # Bind service name globally so every log line includes it
    structlog.contextvars.bind_contextvars(service=service_name)

    # Silence noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
