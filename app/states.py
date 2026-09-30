from aiogram.fsm.state import State, StatesGroup


class SteamOrder(StatesGroup):
    login = State()
    currency = State()
    amount = State()


class StarsOrder(StatesGroup):
    username = State()
    quantity = State()


class PremiumOrder(StatesGroup):
    username = State()
    months = State()


class SupportDialog(StatesGroup):
    customer_message = State()
    admin_reply = State()
