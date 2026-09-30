from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    resell_api_key: str
    database_path: str
    payment_enabled: bool
    markup_percent: float
    poll_seconds: int
    operator_name: str
    policy_revision: str
    data_retention_days: int
    topup_banner_path: str
    cabinet_banner_path: str
    rub_price_rounding: int
    outbound_proxy: str | None


def load_settings() -> Settings:
    load_dotenv()
    missing = [key for key in ("BOT_TOKEN", "RESELL_API_KEY") if not os.getenv(key)]
    if missing:
        raise RuntimeError("Не заданы переменные: " + ", ".join(missing))
    proxy = os.getenv("OUTBOUND_PROXY", "").strip() or None
    if proxy:
        parsed = urlparse(proxy)
        try:
            port = parsed.port
        except ValueError:
            port = None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not port:
            raise RuntimeError("OUTBOUND_PROXY должен иметь вид http://host:port или http://login:password@host:port")
    return Settings(
        bot_token=os.environ["BOT_TOKEN"],
        resell_api_key=os.environ["RESELL_API_KEY"],
        database_path=os.getenv("DATABASE_PATH", "data/bot.sqlite3"),
        payment_enabled=os.getenv("PAYMENT_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        markup_percent=float(os.getenv("PRICE_MARKUP_PERCENT", "10")),
        poll_seconds=max(10, int(os.getenv("ORDER_POLL_SECONDS", "20"))),
        operator_name=os.getenv("OPERATOR_NAME", "Владелец сервиса NovaTop"),
        policy_revision=os.getenv("POLICY_REVISION", "30.06.2026"),
        data_retention_days=max(1, int(os.getenv("DATA_RETENTION_DAYS", "365"))),
        topup_banner_path=os.getenv("TOPUP_BANNER_PATH", "/app/assets/topup-banner.png"),
        cabinet_banner_path=os.getenv("CABINET_BANNER_PATH", "/app/assets/cabinet-banner.png"),
        rub_price_rounding=max(1, int(os.getenv("RUB_PRICE_ROUNDING", "10"))),
        outbound_proxy=proxy,
    )
