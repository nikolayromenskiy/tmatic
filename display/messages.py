from enum import Enum


class Message(str, Enum):
    SUBSCRIPTION_ADDED = "Added subscription to {SYMBOL}."

    def __str__(self) -> str:
        return self.value


class ErrorMessage(str, Enum):
    BOT_FOLDER_NOT_FOUND = (
        "``{BOT_NAME}`` bot is in the database but there is no subdirectory named "
        + "``{BOT_NAME}``. You should either restore the subdirectory in the ``algo`` "
        + "folder, then restart <f3> Tmatic, or delete this bot using ``Bot menu``.\n"
    )
    BOT_MARKET_ERROR = (
        "There was an error loading {MODULE}:\n\n{EXCEPTION}\n\n"
        + "You are probably trying to use an exchange that is not connected. "
        + "You should either add the exchange in the .env file or correct "
        + "the strategy file or delete ``{BOT_NAME}`` using the ``Bot Menu``.\n"
    )
    BOT_LOADING_ERROR = (
        "There was an error loading {MODULE}:\n\n{EXCEPTION}\n\n"
        + "You should either correct the strategy file or delete ``{BOT_NAME}`` "
        + "using the ``Bot Menu``.\n"
    )
    IMPOSSIBLE_SUBSCRIPTION = (
        "The {SYMBOL} instrument has a {STATE} status, but is normally Open. "
        + "The instrument has probably expired, but in the database there are "
        + " still positions that should not exist. Check your trading history."
    )

    def __str__(self) -> str:
        return self.value
