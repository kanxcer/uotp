"""The ``/createbot`` white-label conversation.

Walks an owner through: paste a bot token, pick their extra % on OUR selling
price (suggested 38%), read the terms, confirm.

Clone bots always use platform numbers and the platform FamGateway. There is
no "own UOTP API" option — that path leaked provider access and a second
payment rail. The extra % is stored with the bot so the terms they agreed
are the terms charged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .reseller import DEFAULT_RESELLER_RATE, clone_price, parse_percent
from .whitelabel import (
    DEFAULT_PLATFORM_FEE,
    PlatformFee,
    SubBot,
    SubBotMode,
    SubBotRegistry,
    validate_bot_token,
    verify_bot_token,
)

__all__ = [
    "CreateBotFlow",
    "PendingCreation",
    "CreateBotResult",
    "CB_HUB_MINE",
    "CB_HUB_ADD",
    "clone_about",
    "clone_description",
    "apply_clone_profile",
    "platform_username_from_token",
]

STEP_TIMEOUT_HINT = "Send /cancel to abort at any point."

CB_CONFIRM = "cb:createbot:confirm"
CB_ABORT = "cb:createbot:abort"
CB_MARGIN_SUGGESTED = "cb:createbot:margin:38"
# Landing hub (My bots / Create) — reachable without a pending session.
CB_HUB_MINE = "cb:hub:mine"
CB_HUB_ADD = "cb:hub:add"
# Legacy callbacks from the retired own-API / mode picker: treat as abort.
CB_PLATFORM = "cb:createbot:platform"
CB_OWN_API = "cb:createbot:own_api"

#: Telegram Bot API limits for setMyShortDescription / setMyDescription.
ABOUT_MAX = 120
DESCRIPTION_MAX = 512


class Step:
    AWAIT_TOKEN = "await_token"
    AWAIT_MARGIN = "await_margin"
    AWAIT_CONFIRM = "await_confirm"
    # Kept so older in-flight sessions / tests naming them don't AttributeError.
    AWAIT_MODE = "await_mode"
    AWAIT_KEY = "await_key"


def clone_handle(main_username: str) -> str:
    """``@username`` or empty when we don't know the main bot yet."""
    name = (main_username or "").strip().lstrip("@")
    if not name or name == "?":
        return ""
    return "@" + name


def clone_about(main_username: str = "") -> str:
    """Telegram About (setMyShortDescription). Plain text, ≤120 chars."""
    handle = clone_handle(main_username)
    powered = f" Powered by {handle}." if handle else ""
    text = f"Real 🇮🇳 SIMs · 1,000+ apps · OTP in seconds.{powered}".strip()
    return text[:ABOUT_MAX]


def clone_description(main_username: str = "") -> str:
    """Telegram Description (setMyDescription). Plain text, ≤512 chars."""
    handle = clone_handle(main_username)
    powered = f"Powered by {handle}. " if handle else ""
    text = (
        "Get a real 🇮🇳 Indian number. OTP in seconds.\n\n"
        "1,000+ apps — Telegram, WhatsApp, Uber, Blinkit, Zomato, every "
        "service you use. Real Indian SIMs.\n\n"
        "Tap. See the price. Pay. Done. No hidden fees, no sign-up, no papers.\n\n"
        "Number stays live 20 minutes — as many OTPs as you need until it "
        "expires. No OTP? Instant auto-refund.\n\n"
        "Recharge once, buy anytime. Refunds land in your wallet immediately.\n\n"
        f"{powered}Send /start. Need help? Tap Support."
    )
    return text[:DESCRIPTION_MAX]


def _telegram_method(token: str, method: str, payload: dict) -> tuple[bool, dict]:
    """POST one Bot API method. Never raises."""
    import json
    import urllib.error
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            body = json.loads(resp.read().decode() or "{}")
            return bool(body.get("ok")), body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode() or "{}")
            return False, body if isinstance(body, dict) else {"description": str(exc)}
        except Exception:  # noqa: BLE001
            return False, {"description": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return False, {"description": f"{type(exc).__name__}: {exc}"}


def apply_clone_profile(
    bot_token: str, *, main_username: str = "", api=None,
) -> tuple[bool, str]:
    """Write About + Description on a freshly created clone. Never raises.

    Returns ``(True, "ok")`` when both Bot API calls succeed, else
    ``(False, reason)``. Creation of the clone must not depend on this.
    """
    if not bot_token:
        return False, "no token"
    call = api or _telegram_method
    about = clone_about(main_username)
    desc = clone_description(main_username)
    ok1, body1 = call(bot_token, "setMyShortDescription", {"short_description": about})
    ok2, body2 = call(bot_token, "setMyDescription", {"description": desc})
    if ok1 and ok2:
        return True, "ok"
    reasons = []
    if not ok1:
        reasons.append(str((body1 or {}).get("description") or "About failed"))
    if not ok2:
        reasons.append(str((body2 or {}).get("description") or "Description failed"))
    return False, "; ".join(reasons) or "profile update failed"


def platform_username_from_token(token: str) -> str:
    """Live ``getMe`` username for the platform bot, or empty."""
    if not token:
        return ""
    ok, info = verify_bot_token(token)
    if ok and info and info not in ("?",):
        return str(info).lstrip("@")
    return ""


@dataclass
class PendingCreation:
    """Where an owner is in the creation flow."""

    owner_id: str
    step: str = Step.AWAIT_TOKEN
    bot_token: str = ""
    mode: Optional[SubBotMode] = None
    provider_key: str = ""
    provider_url: str = ""
    fee: PlatformFee = field(default_factory=lambda: DEFAULT_PLATFORM_FEE)
    reseller_rate: Decimal = field(default_factory=lambda: DEFAULT_RESELLER_RATE)
    bot: Optional[SubBot] = None

    def reset(self) -> None:
        self.step = Step.AWAIT_TOKEN
        self.bot_token = ""
        self.mode = None
        self.provider_key = ""
        self.bot = None
        self.reseller_rate = DEFAULT_RESELLER_RATE


@dataclass
class CreateBotResult:
    """What a step produced."""

    reply: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    rows: list[tuple[tuple[str, str], ...]] = field(default_factory=list)
    created: Optional[SubBot] = None
    finished: bool = False


class CreateBotFlow:
    """Drives one owner's ``/createbot`` session to completion."""

    def __init__(
        self, registry: SubBotRegistry, fee: Optional[PlatformFee] = None,
        *,
        token_verifier=None,
        platform_username: str = "",
        profile_applier=None,
    ) -> None:
        self._registry = registry
        self._fee = fee or DEFAULT_PLATFORM_FEE
        self._pending: dict[str, PendingCreation] = {}
        self._verify_token = token_verifier or verify_bot_token
        self._platform_username = (platform_username or "").lstrip("@")
        self._apply_profile = profile_applier

    def hub(self) -> CreateBotResult:
        """Landing: My bots + Create/add. Does not start a pending session."""
        return CreateBotResult(
            "🤖 Run your own bot\n\n"
            "Clone this shop as your own Telegram bot. You pick an extra % "
            "on our selling price; customers pay through our UPI.\n\n"
            "📋 My bots — bots you already run.\n"
            "➕ Create / add bot — paste a @BotFather token and go live.",
            rows=[(("📋 My bots", CB_HUB_MINE), ("➕ Create / add bot", CB_HUB_ADD))],
        )

    def start(self, owner_id: str) -> CreateBotResult:
        p = self._pending.setdefault(
            owner_id, PendingCreation(owner_id=owner_id, fee=self._fee)
        )
        p.reset()
        p.fee = self._fee
        return CreateBotResult(
            "Paste the **bot token** for the bot you want to run.\n"
            "Get one from @BotFather: `/newbot` → name → username → token.\n\n"
            "The token looks like `123456789:AAF...`. Keep it private; anyone "
            "holding it controls that bot.\n\n"
            "Your bot will sell OUR numbers. Customers pay through our UPI. "
            "You pick an extra % on our selling price — we suggest **38%**.\n\n"
            + STEP_TIMEOUT_HINT,
        )

    def cancel(self, owner_id: str) -> None:
        self._pending.pop(owner_id, None)

    def pending(self, owner_id: str) -> Optional[PendingCreation]:
        return self._pending.get(owner_id)

    def on_text(self, owner_id: str, text: str) -> CreateBotResult:
        p = self._pending.get(owner_id)
        if p is None:
            return CreateBotResult("Send /createbot to start.", finished=True)
        text = text.strip()
        if p.step is Step.AWAIT_TOKEN:
            return self._take_token(p, text)
        if p.step is Step.AWAIT_MARGIN:
            return self._take_margin(p, text)
        if p.step is Step.AWAIT_CONFIRM:
            if text.lower() in {"yes", "y", "confirm", "agree"}:
                return self._confirm(p)
            if text.lower() in {"no", "n", "cancel"}:
                self.cancel(owner_id)
                return CreateBotResult("Cancelled. Nothing was created.", finished=True)
            return CreateBotResult(
                "Reply `yes` to accept these terms and create the bot, or `no` to "
                "cancel.\n\n" + self._terms_summary(p)
            )
        return CreateBotResult(
            "Send the extra % you want to charge (e.g. 38), or tap the button.",
            buttons=self._margin_buttons(),
        )

    @staticmethod
    def _margin_buttons() -> list[tuple[str, str]]:
        return [("Use 38% (suggested)", CB_MARGIN_SUGGESTED)]

    def _take_token(self, p: PendingCreation, token: str) -> CreateBotResult:
        if not validate_bot_token(token):
            return CreateBotResult(
                "That does not look like a Telegram bot token.\n"
                "It should be a numeric ID, a colon, then ~35 characters, "
                "like `123456789:AAF...`.\n\n"
                "Paste the full token again, or send /cancel."
            )
        if self._registry.find_by_token(token):
            return CreateBotResult(
                "That bot token is already registered. Use /mybots to see it, "
                "or /deletebot to remove it first."
            )
        p.bot_token = token
        p.step = Step.AWAIT_MARGIN
        from .money import INR
        sample = INR("14.50")
        example = clone_price(sample, DEFAULT_RESELLER_RATE)
        return CreateBotResult(
            "Token received.\n\n"
            "**What extra % do you want to charge on top of our selling price?**\n\n"
            "Suggested: **38%**\n"
            f"Example: if a service is {sample} here, at 38% your customers "
            f"pay {example}.\n\n"
            "You keep 95% of that extra; we keep 5% as the platform fee. "
            "Numbers always come from us — you cannot add your own UOTP API.\n\n"
            "Reply with a number (e.g. `38` or `38%`), or tap the button.",
            buttons=self._margin_buttons(),
        )

    def _take_margin(self, p: PendingCreation, text: str) -> CreateBotResult:
        rate = parse_percent(text)
        if rate is None:
            return CreateBotResult(
                "That doesn't look like a percentage. Send a number between "
                "1 and 200 (we suggest 38), e.g. `38` or `38%`.",
                buttons=self._margin_buttons(),
            )
        return self._accept_margin(p, rate)

    def _accept_margin(self, p: PendingCreation, rate: Decimal) -> CreateBotResult:
        p.reseller_rate = rate
        p.mode = SubBotMode.PLATFORM_API
        p.provider_key = ""
        p.bot = SubBot(
            owner_id=p.owner_id, bot_token=p.bot_token, mode=p.mode,
            fee=p.fee, reseller_rate=rate,
        )
        p.step = Step.AWAIT_CONFIRM
        return CreateBotResult(
            "**Read this before you confirm.**\n\n" + p.bot.fee_disclosure(),
            buttons=[("I accept, create my bot", CB_CONFIRM), ("Cancel", CB_ABORT)],
        )

    def on_button(self, owner_id: str, data: str) -> CreateBotResult:
        p = self._pending.get(owner_id)
        if p is None:
            return CreateBotResult("Send /createbot to start.", finished=True)
        if data == CB_MARGIN_SUGGESTED or data.startswith("cb:createbot:margin:"):
            if p.step not in (Step.AWAIT_MARGIN, Step.AWAIT_TOKEN):
                if p.step is Step.AWAIT_CONFIRM:
                    return CreateBotResult(
                        "Already have your extra %. Confirm or cancel below.",
                        buttons=[("I accept, create my bot", CB_CONFIRM),
                                 ("Cancel", CB_ABORT)],
                    )
            if p.step is Step.AWAIT_TOKEN or not p.bot_token:
                return CreateBotResult("Paste your bot token first.")
            suffix = data.rsplit(":", 1)[-1]
            rate = parse_percent(suffix) or DEFAULT_RESELLER_RATE
            return self._accept_margin(p, rate)
        if data == CB_CONFIRM:
            return self._confirm(p)
        if data in (CB_ABORT, CB_PLATFORM, CB_OWN_API):
            # Own-API / mode-picker is retired. Stale buttons cancel cleanly.
            self.cancel(owner_id)
            if data == CB_OWN_API:
                return CreateBotResult(
                    "Your own UOTP API is not available on clone bots. "
                    "Send /createbot to start again — you'll pick an extra % "
                    "on our selling price instead.",
                    finished=True,
                )
            return CreateBotResult("Cancelled. Nothing was created.", finished=True)
        return CreateBotResult("Unknown action; send /cancel and try again.")

    def _confirm(self, p: PendingCreation) -> CreateBotResult:
        if p.bot is None:
            return CreateBotResult("Nothing to confirm; send /createbot to start.")
        ok, info = self._verify_token(p.bot.bot_token)
        if not ok:
            return CreateBotResult(
                "⚠️ That bot token was rejected by Telegram — your bot would "
                "never reply.\n\n"
                f"Reason: {info}\n\n"
                "Create a fresh bot with @BotFather → /newbot, copy the token "
                "exactly (a numeric ID, a colon, then ~35 chars), and /createbot "
                "again. Nothing was created or charged.",
                finished=True,
            )
        try:
            self._registry.add(p.bot)
        except Exception as exc:  # noqa: BLE001
            return CreateBotResult(
                f"Could not create your bot: {exc}. Nothing was charged.",
                finished=True,
            )
        bot = p.bot
        self._pending.pop(p.owner_id, None)
        extra_pct = (bot.reseller_rate * 100).quantize(Decimal("1"))
        note = self._stamp_profile(bot.bot_token)
        return CreateBotResult(
            f"**Your bot `{bot.id}` is created.**\n\n"
            f"Extra on our prices: {extra_pct}%\n"
            f"You keep 95% of that extra; we keep 5%.\n"
            "Customers pay through our UPI. Withdraw from your admin panel.\n\n"
            "It runs the same shop as this bot. Send it /start.\n"
            "Manage it with /mybots and /deletebot."
            + note,
            created=bot,
            finished=True,
        )

    def _stamp_profile(self, bot_token: str) -> str:
        """Best-effort About + Description. Never blocks creation."""
        applier = self._apply_profile or apply_clone_profile
        try:
            ok, reason = applier(
                bot_token, main_username=self._platform_username,
            )
        except TypeError:
            # Test stubs that only take the token.
            try:
                ok, reason = applier(bot_token)
            except Exception as exc:  # noqa: BLE001
                ok, reason = False, f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"{type(exc).__name__}: {exc}"
        if ok:
            return ""
        return (
            "\n\nNote: could not set the bot's About/Description"
            + (f" ({reason})" if reason else "")
            + ". You can edit those in @BotFather."
        )

    @staticmethod
    def _terms_summary(p: PendingCreation) -> str:
        return p.bot.fee_disclosure() if p.bot else ""
