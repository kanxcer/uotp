"""uotpbot -- virtual-number OTP reseller with auditable unit economics.

Public surface::

    from uotpbot import INR, Money, Catalog, Pricer, FeeModel, BotEngine

The money type is exact integer paise; nothing in this package uses a float to
hold currency. See ``uotpbot.money`` for the rules and ``docs/RESEARCH.md``
for the provider pricing this is calibrated against.
"""

from __future__ import annotations

from .catalog import (
    PROVIDER_MIN_CHARGE,
    PROVIDER_REFUND_WINDOW_MINUTES,
    PROVIDER_VALIDITY_MINUTES,
    Catalog,
    CatalogError,
    ServiceCost,
    UOTP_PACKS,
    WalletPack,
    default_catalog,
    load_catalog,
)
from .economics import (
    DeliveryModel,
    EconomicsError,
    FeeModel,
    OrderEconomics,
    PricingAdvice,
)
from .engine import BotEngine, EngineConfig, FulfilResult
from .ledger import (
    CASH,
    COGS,
    GATEWAY,
    GST,
    OWNER,
    SALES,
    TOPUP_FEE,
    WALLET,
    Account,
    Ledger,
    LedgerError,
    PnL,
    Posting,
)
from .money import INR, Money, Rate, quantize_money, rate, split_amount
from .orders import Order, OrderState, RetryDecision, retry_policy
from .pricing import PRICE_LADDER, PriceLadder, Pricer, Strategy
from .provider.base import (
    AuthError,
    Balance,
    InsufficientBalance,
    NumberAllocation,
    NumberUnavailable,
    OtpResult,
    Provider,
    ProviderError,
    PurchaseTimedOut,
    ServiceUnavailable,
    SmsMessage,
)

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # money
    "INR", "Money", "Rate", "quantize_money", "rate", "split_amount",
    # catalog
    "Catalog", "CatalogError", "ServiceCost", "WalletPack", "UOTP_PACKS",
    "PROVIDER_MIN_CHARGE", "PROVIDER_VALIDITY_MINUTES",
    "PROVIDER_REFUND_WINDOW_MINUTES", "load_catalog", "default_catalog",
    # economics
    "DeliveryModel", "FeeModel", "OrderEconomics", "PricingAdvice", "EconomicsError",
    # pricing
    "Pricer", "PriceLadder", "PRICE_LADDER", "Strategy",
    # ledger
    "Ledger", "LedgerError", "Posting", "PnL", "Account",
    "CASH", "WALLET", "COGS", "GATEWAY", "TOPUP_FEE", "GST", "OWNER", "SALES",
    # orders / engine
    "Order", "OrderState", "RetryDecision", "retry_policy",
    "BotEngine", "EngineConfig", "FulfilResult",
    # provider
    "Provider", "ProviderError", "AuthError", "InsufficientBalance",
    "ServiceUnavailable", "NumberUnavailable", "PurchaseTimedOut",
    "Balance", "NumberAllocation", "SmsMessage", "OtpResult",
]
