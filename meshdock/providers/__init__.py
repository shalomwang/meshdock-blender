from .base import ProviderAdapter, ProviderCapabilities, ProviderStatus
from .compare import CompareAdapter
from .hunyuan import TokenHubAdapter
from .hunyuan_direct import HunyuanDirectAdapter
from .mock import MockProvider
from .tripo import TripoAdapter

# Source compatibility for 0.7.x integrations.  The old class always spoke
# TokenHub despite its name; new code should import TokenHubAdapter explicitly.
HunyuanAdapter = TokenHubAdapter

__all__ = [
    "HunyuanDirectAdapter",
    "TokenHubAdapter",
    "HunyuanAdapter",
    "CompareAdapter",
    "MockProvider",
    "ProviderAdapter",
    "ProviderCapabilities",
    "ProviderStatus",
    "TripoAdapter",
]
