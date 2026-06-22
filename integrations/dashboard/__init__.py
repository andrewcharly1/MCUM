"""Local read-only operational dashboard for MCUM."""

from .data_service import DashboardDataService, normalize_status, redact_secrets

__all__ = ["DashboardDataService", "create_server", "normalize_status", "redact_secrets"]


def create_server(*args, **kwargs):
    """Create the local server without importing it during module startup."""
    from .server import create_server as build_server

    return build_server(*args, **kwargs)
