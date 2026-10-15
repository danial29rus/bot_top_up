from __future__ import annotations

from decimal import Decimal
from typing import Any

import aiohttp


class ResellError(RuntimeError):
    pass


class ResellClient:
    base_url = "https://resell.codes/api/v1"

    def __init__(self, api_key: str, proxy: str | None = None):
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.proxy = proxy
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=25), proxy=self.proxy)

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.session:
            raise RuntimeError("ResellClient is not started")
        async with self.session.request(method, self.base_url + path, json=payload) as response:
            try:
                data = await response.json(content_type=None)
            except aiohttp.ContentTypeError:
                data = {}
            if response.status >= 400:
                message = data.get("error", {}).get("message", response.reason)
                raise ResellError(f"Resell API {response.status}: {message}")
            return data

    async def check_steam_login(self, login: str) -> bool:
        data = await self._request("POST", "/steam-topup/check-login", {"steam_login": login})
        return bool(data.get("can_refill"))

    async def steam_rates(self) -> dict[str, Decimal]:
        data = await self._request("GET", "/steam-topup/rates")
        return {key: Decimal(str(value)) for key, value in data["rates"].items()}

    async def create_order(self, product: str, payload: dict[str, Any]) -> dict[str, Any]:
        endpoints = {
            "steam": "/steam-topup/order",
            "stars": "/telegram/stars/buy",
            "premium": "/telegram/premium/buy",
        }
        return await self._request("POST", endpoints[product], payload)

    async def get_order(self, supplier_order_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/orders/{supplier_order_id}")

    @staticmethod
    def _price_from_catalogue(data: Any, selector: str, value: int) -> Decimal:
        """Accept the current Resell Telegram catalogue formats without guessing prices."""
        if isinstance(data, dict):
            selector_value = data.get(selector)
            if selector_value is None and selector == "quantity":
                selector_value = data.get("stars")
            if selector_value == value and data.get("price_usd") is not None:
                return Decimal(str(data["price_usd"]))
            for nested in data.values():
                try:
                    return ResellClient._price_from_catalogue(nested, selector, value)
                except LookupError:
                    continue
        elif isinstance(data, list):
            for nested in data:
                try:
                    return ResellClient._price_from_catalogue(nested, selector, value)
                except LookupError:
                    continue
        raise LookupError

    async def telegram_premium_price(self, months: int) -> Decimal:
        data = await self._request("GET", "/telegram/premium")
        try:
            return self._price_from_catalogue(data, "months", months)
        except (LookupError, ArithmeticError) as exc:
            raise ResellError("Текущая цена выбранного срока Premium не найдена") from exc
