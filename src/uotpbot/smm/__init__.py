"""Social-boost shop: a second catalogue on the same customer wallet.

The upstream panel is behind :class:`PanelV2Client` so the provider can be
swapped without touching pricing, the wallet or the Telegram UI. Customer-facing
copy never names the supplier.
"""

from .catalog import SmmCatalog, SmmService, detect_platform
from .persist import SmmOrderRow
from .provider import (
    SmmAmbiguous,
    SmmAuthError,
    SmmError,
    SmmProviderError,
    SmmStatus,
    SmmUserError,
)
from .shop import SmmShop

__all__ = [
    "SmmAmbiguous",
    "SmmAuthError",
    "SmmCatalog",
    "SmmError",
    "SmmOrderRow",
    "SmmProviderError",
    "SmmService",
    "SmmShop",
    "SmmStatus",
    "SmmUserError",
    "detect_platform",
]
