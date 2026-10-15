"""Settings for the Links runtime itself. Each link also loads its source's settings."""

from __future__ import annotations

from pydantic import Field

from arango_connector.config import ConnectorSettings


class LinkSettings(ConnectorSettings):
    integration_service_address: str = Field(
        default="127.0.0.1:9201",
        description="gRPC address of the integration sidecar. Injected by the platform.",
    )
    link_idle_wait_seconds: float = Field(
        default=5,
        gt=0,
        description="How long to wait before asking for a job again when none was waiting.",
    )
    link_cancel_check_seconds: float = Field(
        default=5,
        gt=0,
        description="How often a running job re-reads its state to notice cancellation.",
    )
    log_level: str = Field(default="INFO", description="Python log level.")
