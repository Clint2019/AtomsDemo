from atoms_demo.ports.providers import InfrastructureExecutor
from atoms_demo.config import Settings
from atoms_demo.core.errors import FeatureNotImplemented


def create_adapter(settings: Settings) -> InfrastructureExecutor:
    """TODO: implement InfrastructureExecutor; no network or credential access at import time."""
    raise FeatureNotImplemented("infrastructure adapter not implemented")
