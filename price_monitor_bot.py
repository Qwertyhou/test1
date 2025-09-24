"""Telegram price monitoring bot for Aster Coin.

This module implements a configurable Telegram bot that periodically fetches
prices from the Aster and Dexscreener APIs and posts spread alerts to a
Telegram channel.  The bot is designed to be production ready with logging,
retry logic, and graceful shutdown support.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional, Tuple

import requests


LOGGER = logging.getLogger(__name__)


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class BotConfig:
    """Runtime configuration for :class:`PriceMonitorBot`.

    Attributes
    ----------
    telegram_bot_token:
        Token for the Telegram bot obtained via BotFather.
    telegram_channel_id:
        Identifier of the target Telegram chat/channel.
    price_threshold:
        Fractional percentage spread (0.05 == 5%) required to trigger alerts.
    check_interval:
        Number of seconds between successive price checks.
    aster_symbol:
        Market symbol used by the Aster API (e.g. ``"ADAUSDT"``).
    dexscreener_chain_id:
        Chain identifier for Dexscreener lookups.
    dexscreener_pair_id:
        Pair identifier for Dexscreener lookups.
    request_timeout:
        Timeout applied to HTTP requests in seconds.
    max_retries:
        Maximum retry attempts for failed HTTP requests.
    backoff_factor:
        Multiplier used between retry delays to implement exponential backoff.
    """

    telegram_bot_token: str
    telegram_channel_id: str
    price_threshold: float
    check_interval: float
    aster_symbol: str
    dexscreener_chain_id: str
    dexscreener_pair_id: str
    request_timeout: float = 10.0
    max_retries: int = 3
    backoff_factor: float = 2.0

    @staticmethod
    def from_env() -> "BotConfig":
        """Create a configuration object from environment variables."""

        def get_env(name: str, *, required: bool = False) -> Optional[str]:
            value = os.getenv(name)
            if required and not value:
                raise ConfigurationError(f"Environment variable '{name}' is required")
            return value

        telegram_bot_token = get_env("TELEGRAM_BOT_TOKEN", required=True)
        telegram_channel_id = get_env("TELEGRAM_CHANNEL_ID", required=True)

        def get_float(name: str, default: float) -> float:
            value = get_env(name)
            if value is None:
                return default
            try:
                return float(value)
            except ValueError as exc:
                raise ConfigurationError(
                    f"Environment variable '{name}' must be a float"
                ) from exc

        def get_int(name: str, default: int) -> int:
            value = get_env(name)
            if value is None:
                return default
            try:
                return int(value)
            except ValueError as exc:
                raise ConfigurationError(
                    f"Environment variable '{name}' must be an integer"
                ) from exc

        price_threshold = get_float("PRICE_THRESHOLD", 0.05)
        check_interval = get_float("CHECK_INTERVAL", 60.0)
        aster_symbol = get_env("ASTER_SYMBOL") or "ADAUSDT"
        dexscreener_chain_id = get_env("DEX_CHAIN_ID") or "bsc"
        dexscreener_pair_id = (
            get_env("DEX_PAIR_ID")
            or "0x3817fF61B34c5Ff5dc89709B2Db1f194299e3BA9"
        )

        request_timeout = get_float("REQUEST_TIMEOUT", 10.0)
        max_retries = get_int("MAX_RETRIES", 3)
        backoff_factor = get_float("BACKOFF_FACTOR", 2.0)

        if price_threshold < 0:
            raise ConfigurationError("PRICE_THRESHOLD must be non-negative")
        if check_interval <= 0:
            raise ConfigurationError("CHECK_INTERVAL must be greater than zero")
        if request_timeout <= 0:
            raise ConfigurationError("REQUEST_TIMEOUT must be greater than zero")
        if max_retries <= 0:
            raise ConfigurationError("MAX_RETRIES must be greater than zero")
        if backoff_factor <= 1:
            raise ConfigurationError("BACKOFF_FACTOR must be greater than one")

        return BotConfig(
            telegram_bot_token=telegram_bot_token,
            telegram_channel_id=telegram_channel_id,
            price_threshold=price_threshold,
            check_interval=check_interval,
            aster_symbol=aster_symbol,
            dexscreener_chain_id=dexscreener_chain_id,
            dexscreener_pair_id=dexscreener_pair_id,
            request_timeout=request_timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
        )


def setup_logging() -> None:
    """Configure root logging for the bot."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def retry_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: float,
    max_retries: int,
    backoff_factor: float,
    **request_kwargs,
) -> Optional[requests.Response]:
    """Execute an HTTP request with retry support.

    Returns the :class:`requests.Response` if successful, or ``None`` if all
    retry attempts fail.  Exceptions are logged and suppressed.
    """

    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            response = session.request(
                method,
                url,
                timeout=timeout,
                **request_kwargs,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            LOGGER.warning(
                "HTTP request failed (attempt %s/%s) for %s: %s",
                attempt,
                max_retries,
                url,
                exc,
            )
            if attempt >= max_retries:
                break
            time.sleep(delay)
            delay *= backoff_factor
    return None


def fetch_aster_price(
    session: requests.Session,
    config: BotConfig,
) -> Optional[float]:
    """Fetch the latest price from the Aster API."""

    url = "https://sapi.asterdex.com/api/v1/ticker/price"
    response = retry_request(
        session,
        "GET",
        url,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
        backoff_factor=config.backoff_factor,
    )
    if not response:
        return None
    try:
        payload = response.json()
    except json.JSONDecodeError:
        LOGGER.error("Invalid JSON received from Aster API: %s", response.text)
        return None
    # The Aster API returns either a single object when the ``symbol`` query
    # parameter is supplied or a list of ticker objects when omitted.  Some
    # deployments have been observed to return the list variant even when a
    # symbol is provided, so we defensively handle both formats here.
    if isinstance(payload, list):
        matching = next(
            (
                item
                for item in payload
                if isinstance(item, dict)
                and item.get("symbol") == config.aster_symbol
            ),
            None,
        )
        if matching is None:
            LOGGER.warning(
                "Aster symbol %s not found in response payload", config.aster_symbol
            )
            payload = {}
        else:
            payload = matching
    if not isinstance(payload, dict):
        LOGGER.error("Unexpected payload from Aster API: %s", payload)
        return None

    price = payload.get("price")
    try:
        return float(price)
    except (TypeError, ValueError):
        LOGGER.error("Unexpected price payload from Aster API: %s", payload)
        return None


def fetch_dexscreener_price(
    session: requests.Session,
    config: BotConfig,
) -> Optional[float]:
    """Fetch the latest price from the Dexscreener API."""

    url = (
        "https://api.dexscreener.com/latest/dex/pairs/"
        f"{config.dexscreener_chain_id}/{config.dexscreener_pair_id}"
    )
    response = retry_request(
        session,
        "GET",
        url,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
        backoff_factor=config.backoff_factor,
        params={"symbol": config.aster_symbol},
    )
    if not response:
        return None
    try:
        payload = response.json()
    except json.JSONDecodeError:
        LOGGER.error("Invalid JSON received from Dexscreener API: %s", response.text)
        return None

    pair_info = payload.get("pair")
    if pair_info is None and isinstance(payload.get("pairs"), list):
        # Some Dexscreener endpoints respond with a ``pairs`` list rather than
        # a single ``pair`` object.  Prefer an entry that matches the configured
        # chain/pair identifiers to keep the behaviour deterministic.
        for item in payload["pairs"]:
            if isinstance(item, dict) and item.get("pairAddress") == config.dexscreener_pair_id:
                pair_info = item
                break
        else:
            first = payload["pairs"][0] if payload["pairs"] else None
            pair_info = first if isinstance(first, dict) else None
    if not isinstance(pair_info, dict):
        LOGGER.error("Unexpected payload from Dexscreener API: %s", payload)
        return None
    price = pair_info.get("priceUsd")
    try:
        return float(price)
    except (TypeError, ValueError):
        LOGGER.error("Unexpected price data from Dexscreener API: %s", payload)
        return None


def calculate_spread(aster_price: float, dexscreener_price: float) -> Tuple[float, float]:
    """Return absolute and percentage spreads between the given prices."""

    absolute_diff = abs(aster_price - dexscreener_price)
    baseline = (aster_price + dexscreener_price) / 2.0
    if baseline == 0:
        return absolute_diff, 0.0
    percentage_diff = absolute_diff / baseline
    return absolute_diff, percentage_diff


def format_notification(
    aster_price: float,
    dexscreener_price: float,
    absolute_diff: float,
    percentage_diff: float,
) -> str:
    """Create a human-readable Telegram message for the price spread."""

    return (
        "📈 *Aster Coin Price Alert*\n"
        f"• Aster: `${aster_price:,.6f}`\n"
        f"• Dexscreener: `${dexscreener_price:,.6f}`\n"
        f"• Spread: `${absolute_diff:,.6f}` ({percentage_diff * 100:.2f}%)"
    )


def send_telegram_message(session: requests.Session, config: BotConfig, message: str) -> bool:
    """Send a Markdown-formatted message to the configured Telegram chat."""

    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": config.telegram_channel_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    response = retry_request(
        session,
        "POST",
        url,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
        backoff_factor=config.backoff_factor,
        data=payload,
    )
    if response is None:
        LOGGER.error("Failed to deliver Telegram message after retries")
        return False
    LOGGER.info("Notification sent to Telegram chat %s", config.telegram_channel_id)
    return True


class PriceMonitorBot:
    """Telegram bot that monitors price spreads between Aster and Dexscreener."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._stop_event = threading.Event()
        def build_session() -> requests.Session:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(max_retries=0)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            return session

        self._aster_session = build_session()
        self._dex_session = build_session()
        self._telegram_session = build_session()

        self._executor = ThreadPoolExecutor(max_workers=2)

    def run(self) -> None:
        """Start the monitoring loop until the bot is stopped."""

        LOGGER.info("Starting price monitor for %s", self.config.aster_symbol)
        self._install_signal_handlers()
        try:
            while not self._stop_event.is_set():
                start = time.monotonic()
                self._run_iteration()
                elapsed = time.monotonic() - start
                delay = max(0.0, self.config.check_interval - elapsed)
                if delay:
                    self._stop_event.wait(delay)
        finally:
            self._shutdown_resources()
            LOGGER.info("Price monitor stopped")

    def stop(self) -> None:
        """Signal the monitoring loop to exit."""

        self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._handle_signal)

    def _handle_signal(self, signum: int, _frame) -> None:  # pragma: no cover - signal handler
        LOGGER.info("Received signal %s, shutting down...", signum)
        self.stop()

    def _run_iteration(self) -> None:
        LOGGER.debug("Executing price check iteration")
        futures = {
            self._executor.submit(fetch_aster_price, self._aster_session, self.config): "Aster",
            self._executor.submit(
                fetch_dexscreener_price, self._dex_session, self.config
            ): "Dexscreener",
        }

        prices = {}
        for future, label in futures.items():
            try:
                prices[label] = future.result()
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.exception("Unhandled error while fetching %s price: %s", label, exc)
                prices[label] = None

        aster_price = prices.get("Aster")
        dexscreener_price = prices.get("Dexscreener")

        if aster_price is None and dexscreener_price is None:
            LOGGER.error("Failed to retrieve prices from both sources")
            return
        if aster_price is None or dexscreener_price is None:
            LOGGER.warning(
                "Incomplete price data (Aster: %s, Dexscreener: %s)",
                aster_price,
                dexscreener_price,
            )
            return

        absolute_diff, percentage_diff = calculate_spread(aster_price, dexscreener_price)
        LOGGER.info(
            "Aster: %s, Dexscreener: %s, Spread: %s (%.2f%%)",
            aster_price,
            dexscreener_price,
            absolute_diff,
            percentage_diff * 100,
        )
        if percentage_diff < self.config.price_threshold:
            LOGGER.debug(
                "Spread %.4f below threshold %.4f; skipping notification",
                percentage_diff,
                self.config.price_threshold,
            )
            return

        message = format_notification(aster_price, dexscreener_price, absolute_diff, percentage_diff)
        send_telegram_message(self._telegram_session, self.config, message)

    def _shutdown_resources(self) -> None:
        self._executor.shutdown(wait=True)
        self._aster_session.close()
        self._dex_session.close()
        self._telegram_session.close()


def main() -> None:
    """Entry point for running the bot from the command line."""

    setup_logging()
    try:
        config = BotConfig.from_env()
    except ConfigurationError as exc:
        LOGGER.error("Configuration error: %s", exc)
        raise SystemExit(1) from exc

    bot = PriceMonitorBot(config)
    bot.run()


if __name__ == "__main__":
    main()
