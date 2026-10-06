"""Entrypoint: load config, build the pipeline from the registry, run until signalled."""

from __future__ import annotations

import logging
import signal
import sys
from types import FrameType

from arango_connector.config import ConfigError, CoreSettings, load_settings
from arango_connector.pipeline import Pipeline
from arango_connector.registry import build_sink, build_source, build_state_store

logger = logging.getLogger("arango_connector")

LOG_FORMAT = "ts=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"

# Third-party loggers that can be noisy (or verbose about requests) at DEBUG.
_QUIET_LOGGERS = ("slack_bolt", "slack_sdk", "urllib3", "arango")


class _ThirdPartyDebugFilter(logging.Filter):
    """Drop DEBUG records from third-party libraries.

    Filtering at the handler is needed because slack_bolt copies the root logger's
    level onto its own loggers, overriding any level set on them directly.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.INFO:
            return True
        return not any(
            record.name == name or record.name.startswith(name + ".") for name in _QUIET_LOGGERS
        )


def configure_logging(level: str) -> None:
    """Configure stdlib logging with a key=value style format."""
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stdout, force=True)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_ThirdPartyDebugFilter())


def main() -> int:
    """Run the connector. Returns a process exit code."""
    try:
        settings = load_settings(CoreSettings)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    configure_logging(settings.log_level)

    try:
        state = build_state_store(settings)
        pipeline = Pipeline(
            source=build_source(settings, state),
            sink=build_sink(settings),
            dead_letter_path=settings.dead_letter_path,
            retention_days=settings.retention_days,
            state=state,
        )
    except ConfigError as exc:
        # Source-specific settings are only loaded once the source is known.
        print(exc, file=sys.stderr)
        return 2
    except Exception:
        logger.exception("pipeline_build_failed")
        return 1

    def _on_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("signal_received signal=%s", signal.Signals(signum).name)
        pipeline.shutdown()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        pipeline.run()
    except Exception:
        logger.exception("pipeline_failed")
        pipeline.shutdown()
        return 1

    pipeline.shutdown()
    logger.info("connector_exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
