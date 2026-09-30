from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Any
import xml.etree.ElementTree as ET

import aiohttp

from .resell import ResellClient


class PricingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Quote:
    supplier_cost_usd: Decimal
    usd_rub_rate: Decimal
    markup_percent: Decimal
    price_rub: int
    source: str


class PricingService:
    """Creates a repeatable quote which is stored with the order before payment."""

    cbr_url = "https://www.cbr.ru/scripts/XML_daily.asp"

    def __init__(self, resell: ResellClient, markup_percent: float, rounding: int):
        self.resell = resell
        self.markup = Decimal(str(markup_percent))
        self.rounding = Decimal(rounding)
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def usd_rub(self) -> Decimal:
        if not self.session:
            raise RuntimeError("PricingService is not started")
        try:
            async with self.session.get(self.cbr_url) as response:
                response.raise_for_status()
                raw = await response.read()
            root = ET.fromstring(raw)
            for node in root.findall("Valute"):
                if node.findtext("CharCode") == "USD":
                    value = Decimal(node.findtext("Value", "").replace(",", "."))
                    nominal = Decimal(node.findtext("Nominal", "1"))
                    return value / nominal
        except (aiohttp.ClientError, ET.ParseError, ArithmeticError) as exc:
            raise PricingError("Не удалось получить официальный курс USD/RUB") from exc
        raise PricingError("Курс USD в ответе Банка России не найден")

    def retail_quote(self, supplier_cost_usd: Decimal, rate: Decimal, source: str) -> Quote:
        if supplier_cost_usd <= 0 or rate <= 0:
            raise PricingError("Поставщик вернул некорректную цену")
        raw_rub = supplier_cost_usd * rate * (Decimal("1") + self.markup / Decimal("100"))
        rounded = int((raw_rub / self.rounding).to_integral_value(rounding=ROUND_CEILING) * self.rounding)
        return Quote(supplier_cost_usd, rate, self.markup, rounded, source)

    async def steam_quote(self, amount: Decimal, currency: str) -> Quote:
        rates = await self.resell.steam_rates()
        try:
            nominal_usd = amount / rates[currency]
        except KeyError as exc:
            raise PricingError("Валюта Steam сейчас недоступна") from exc
        return self.retail_quote(nominal_usd, await self.usd_rub(), "steam_face_value")

    async def stars_quote(self, quantity: int) -> Quote:
        return self.retail_quote(await self.resell.telegram_stars_price(quantity), await self.usd_rub(), "resell_telegram_stars")

    async def premium_quote(self, months: int) -> Quote:
        return self.retail_quote(await self.resell.telegram_premium_price(months), await self.usd_rub(), "resell_telegram_premium")
