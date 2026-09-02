"""The ``/createbot`` white-label conversation.

A small state machine that walks an owner through: paste a bot token, pick a
mode, (optionally) paste a provider key, read the fee disclosure, confirm.

The fee terms are shown **before** the bot is created and stored with it, so
the terms an owner agreed to are the terms charged. Nothing in this flow can
create a bot without the owner having been shown what it costs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .whitelabel import (
    API_SIGNUP_URL,
    DEFAULT_PLATFORM_FEE,
    PlatformFee,
    SubBot,
    SubBotMode,
    SubBotRegistry,
    validate_bot_token,
)

__all__ = ["CreateBotFlow", "PendingCreation", "CreateBotResult"]

#: How long an owner has to answer each step before it is dropped.
STEP_TIMEOUT_HINT = "Send /cancel to abort at any point."

CB_PLATFORM = "cb:createbot:platform"
CB_OWN_API = "cb:createbot:own_api"
CB_CONFIRM = "cb:createbot:confirm"
CB_ABORT = "cb:createbot:abort"


class Step:
    AWAIT_TOKEN = "await_token"
    AWAIT_MODE = "await_mode"
    AWAIT_KEY = "await_key"
    AWAIT_CONFIRM = "await_confirm"


@dataclass
class PendingCreation:
    """Where an owner is in the creation flow."""

    owner_id: str
    step: str = Step.AWAIT_TOKEN
    bot_token: str = ""
    mode: Optional[SubBotMode] = None
    provider_key: str = ""
    provider_url: str = API_SIGNUP_URL
    fee: PlatformFee = field(default_factory=lambda: DEFAULT_PLATFORM_FEE)
    bot: Optional[SubBot] = None

    def reset(self) -> None:
        self.step = Step.AWAIT_TOKEN
        self.bot_token = ""
        self.mode = None
        self.provider_key = ""
        self.bot = None


@dataclass
class CreateBotResult:
    """What a step produced."""

    reply: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    created: Optional[SubBot] = None
    finished: bool = False


class CreateBotFlow:
    """Drives one owner's ``/createbot`` session to completion."""

    def __init__(
        self, registry: SubBotRegistry, fee: Optional[PlatformFee] = None
    ) -> None:
        self._registry = registry
        self._fee = fee or DEFAULT_PLATFORM_FEE
        self._pending: dict[str, PendingCreation] = {}

    # -- lifecycle -------------------------------------------------------
    def start(self, owner_id: str) -> CreateBotResult:
        if not self._registry.for_owner(owner_id) and self._registry.count() == 0:
            pass  # first bot overall; nothing special to say
        p = self._pending.setdefault(
            owner_id, PendingCreation(owner_id=owner_id, fee=self._fee)
        )
        p.reset()
        p.fee = self._fee
        return CreateBotResult(
            "Paste the **bot token** for the bot you want to white-label.\n"
            "Get one from @BotFather: `/newbot` -> name -> username -> token.\n\n"
            "The token looks like `123456789:AAF...`. Keep it private; anyone "
            "holding it controls that bot.\n\n" + STEP_TIMEOUT_HINT,
        )

    def cancel(self, owner_id: str) -> None:
        self._pending.pop(owner_id, None)

    def pending(self, owner_id: str) -> Optional[PendingCreation]:
        return self._pending.get(owner_id)

    # -- text steps ------------------------------------------------------
    def on_text(self, owner_id: str, text: str) -> CreateBotResult:
        p = self._pending.get(owner_id)
        if p is None:
            return CreateBotResult("Send /createbot to start.", finished=True)
        text = text.strip()
        if p.step is Step.AWAIT_TOKEN:
            return self._take_token(p, text)
        if p.step is Step.AWAIT_KEY:
            return self._take_key(p, text)
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
            "Tap one of the buttons below to choose how your bot sources numbers.",
            buttons=[("Platform numbers (no % fee)", CB_PLATFORM),
                     ("My own API (5% fee)", CB_OWN_API)],
        )

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
        p.step = Step.AWAIT_MODE
        return CreateBotResult(
            "Token received.\n\n**How should your bot get its numbers?**\n\n"
            "**1. Platform numbers** - buys from this bot's wallet at wholesale "
            "price. No percentage fee; you keep everything above what you paid.\n\n"
            "**2. Your own API** - you bring a provider key and pay the provider "
            "directly. The platform takes "
            f"{p.fee.describe()}, itemised on every sale.",
            buttons=[("Platform numbers (no % fee)", CB_PLATFORM),
                     ("My own API (5% fee)", CB_OWN_API)],
        )

    def _take_key(self, p: PendingCreation, key: str) -> CreateBotResult:
        if len(key) < 8 or " " in key:
            return CreateBotResult(
                "That does not look like an API key. Paste the full key from "
                f"{API_SIGNUP_URL}, or send /cancel."
            )
        p.provider_key = key
        p.mode = SubBotMode.OWN_API
        p.bot = SubBot(
            owner_id=p.owner_id, bot_token=p.bot_token, mode=p.mode,
            fee=p.fee, provider_key=key, provider_url=p.provider_url,
        )
        p.step = Step.AWAIT_CONFIRM
        return CreateBotResult(
            "**Read this before you confirm.**\n\n" + p.bot.fee_disclosure(),
            buttons=[("I accept, create my bot", CB_CONFIRM), ("Cancel", CB_ABORT)],
        )

    # -- buttons ---------------------------------------------------------
    def on_button(self, owner_id: str, data: str) -> CreateBotResult:
        p = self._pending.get(owner_id)
        if p is None:
            return CreateBotResult("Send /createbot to start.", finished=True)
        if data == CB_PLATFORM:
            p.mode = SubBotMode.PLATFORM_API
            p.provider_key = ""
            p.bot = SubBot(
                owner_id=owner_id, bot_token=p.bot_token, mode=p.mode, fee=p.fee
            )
            p.step = Step.AWAIT_CONFIRM
            return CreateBotResult(
                "**Read this before you confirm.**\n\n" + p.bot.fee_disclosure(),
                buttons=[("I accept, create my bot", CB_CONFIRM), ("Cancel", CB_ABORT)],
            )
        if data == CB_OWN_API:
            p.step = Step.AWAIT_KEY
            return CreateBotResult(
                "You will need your own provider API key.\n\n"
                f"**Get one here:** {API_SIGNUP_URL}\n"
                "(Register, top up your own balance, copy your API key.)\n\n"
                f"The platform fee is **{p.fee.describe()}** on every sale your "
                "bot makes.\n\nPaste your API key when you have it, or send "
                "/cancel."
            )
        if data == CB_CONFIRM:
            return self._confirm(p)
        if data == CB_ABORT:
            self.cancel(owner_id)
            return CreateBotResult("Cancelled. Nothing was created.", finished=True)
        return CreateBotResult("Unknown action; send /cancel and try again.")

    # -- finishing -------------------------------------------------------
    def _confirm(self, p: PendingCreation) -> CreateBotResult:
        if p.bot is None:
            return CreateBotResult("Nothing to confirm; send /createbot to start.")
        try:
            self._registry.add(p.bot)
        except Exception as exc:  # noqa: BLE001 - surface anything, never half-create
            return CreateBotResult(
                f"Could not create your bot: {exc}. Nothing was charged.",
                finished=True,
            )
        bot = p.bot
        self._pending.pop(p.owner_id, None)
        fee_line = (
            "No percentage fee."
            if bot.mode is SubBotMode.PLATFORM_API
            else f"Platform fee: {bot.fee.describe()} on every sale."
        )
        return CreateBotResult(
            f"**Your bot `{bot.id}` is created.**\n\n"
            f"Terms on record (agreed {bot.disclosed_at}):\n{fee_line}\n\n"
            "It runs the same commands as this bot. Send it /start.\n"
            "Manage it with /mybots and /stopbot.",
            created=bot,
            finished=True,
        )

    @staticmethod
    def _terms_summary(p: PendingCreation) -> str:
        return p.bot.fee_disclosure() if p.bot else ""
