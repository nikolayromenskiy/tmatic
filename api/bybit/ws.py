import json
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

import services as service
from api.bybit.erruni import Unify
from api.init import Setup
from api.variables import Variables
from common.data import MetaAccount, MetaInstrument, MetaResult
from common.variables import Variables as var
from display.messages import ErrorMessage, Message

from .error import ErrorStatus
from .pybit._websocket_stream import _V5WebSocketManager
from .pybit.unified_trading import HTTP, WebSocket


class Bybit(Variables):
    """
    The official Python3 API connector is used https://github.com/bybit-exchange/pybit
    Disabled _send_initial_ping, _on_pong in _websocket_stream.py
    """

    class Account(metaclass=MetaAccount):
        pass

    class Instrument(metaclass=MetaInstrument):
        pass

    class Result(metaclass=MetaResult):
        pass

    def __init__(self):
        self.object = Bybit
        self.name = "Bybit"
        Setup.variables(self)
        self.categories = ["spot", "inverse", "option", "linear"]
        self.settlCurrency_list = {
            "spot": [],
            "inverse": [],
            "option": [],
            "linear": [],
        }
        self.timefrs = OrderedDict(
            [
                (1, 1),
                (3, 3),
                (5, 5),
                (15, 15),
                (30, 30),
                (60, 60),
                (120, 120),
                (240, 240),
                (360, 360),
                (720, 720),
                (1440, "D"),
            ]
        )
        self.orderbook_depth = {
            "quote": {"spot": 1, "inverse": 1, "option": 25, "linear": 1},
            "orderBook": {"spot": 50, "inverse": 50, "option": 25, "linear": 50},
        }
        self.account_types = ["UNIFIED", "CONTRACT"]
        self.settleCoin_list = list()
        self.logger = var.logger
        self.klines = dict()
        self.setup_orders = list()
        self.account_disp = ""
        WebSocket._on_message = Bybit._on_message
        self.ticker = dict()
        self.instrument_index = OrderedDict()
        var.market_object[self.name] = self
        self.unsubscriptions = set()
        self.get_error = ErrorStatus

    def setup_session(self):
        self.session: HTTP = HTTP(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet,
        )

    def start_ws(self):
        """
        Not used in Bybit.
        """

    def setup_streams(self):
        for symbol in self.symbol_list:
            instrument = self.Instrument[symbol]
            if instrument.category == "linear":
                self.Result[(instrument.quoteCoin, self.name)]
            elif instrument.category == "inverse":
                self.Result[(instrument.baseCoin, self.name)]
            elif instrument.category == "spot":
                self.Result[(instrument.baseCoin, self.name)]
                self.Result[(instrument.quoteCoin, self.name)]

        self.__connect()

    def __connect(self) -> None:
        """
        Connecting to websocket.
        """
        self.ws = {
            "spot": WebSocket,
            "inverse": WebSocket,
            "option": WebSocket,
            "linear": WebSocket,
        }
        self.ws_private = WebSocket
        self.ws_wait = {
            "spot": "",
            "inverse": "",
            "option": "",
            "linear": "",
        }

        def subscribe_in_thread(category):
            lst = list()
            for symbol in self.symbol_list:
                if self.Instrument[symbol].category == category:
                    lst.append(symbol)
            threads = []
            for symbol in lst:
                t = threading.Thread(target=self.subscribe_symbol, args=(symbol, True))
                t.start()
                threads.append(t)
            [thread.join() for thread in threads]

        def private_in_thread():
            try:
                self.ws_private = WebSocket(
                    testnet=self.testnet,
                    channel_type="private",
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                )
            except Exception as exception:
                Unify.error_handler(
                    self,
                    exception=exception,
                    verb="WebSocket",
                    path="Private",
                )
            self.ws_private.pinging = "pong"

        threads = []
        for category in self.categories:
            t = threading.Thread(target=subscribe_in_thread, args=(category,))
            threads.append(t)
            t.start()
        t = threading.Thread(target=private_in_thread)
        threads.append(t)
        t.start()
        [thread.join() for thread in threads]
        try:
            self.ws_private.wallet_stream(callback=self.__update_account)
        except Exception as exception:
            Unify.error_handler(
                self,
                exception=exception,
                verb="WebSocket",
                path="Private wallet_stream",
            )
        try:
            self.ws_private.position_stream(callback=self.__update_position)
        except Exception as exception:
            Unify.error_handler(
                self,
                exception=exception,
                verb="WebSocket",
                path="Private position_stream",
            )
        try:
            self.ws_private.order_stream(callback=self.__handle_order)
        except Exception as exception:
            Unify.error_handler(
                self,
                exception=exception,
                verb="WebSocket",
                path="Private order_stream",
            )
        try:
            self.ws_private.execution_stream(callback=self.__handle_execution)
        except Exception as exception:
            Unify.error_handler(
                self,
                exception=exception,
                verb="WebSocket",
                path="Private execution_stream",
            )

    def __update_orderbook(self, values: dict, category: str) -> None:
        symbol = (self.ticker[(values["s"], category)], self.name)
        instrument = self.Instrument[symbol]
        asks = list(map(lambda x: [float(x[0]), float(x[1])], values["a"]))
        bids = list(map(lambda x: [float(x[0]), float(x[1])], values["b"]))
        asks.sort(key=lambda x: x[0])
        bids.sort(key=lambda x: x[0], reverse=True)
        instrument.asks = asks[:10]
        instrument.bids = bids[:10]
        if symbol in self.klines:
            service.kline_hi_lo_values(self, symbol=symbol, instrument=instrument)

    def __update_ticker(self, values: dict, category: str) -> None:
        symb = self.ticker[(values["symbol"], category)]
        instrument = self.Instrument[(symb, self.name)]
        if "volume24h" in values and values["volume24h"]:
            instrument.volume24h = float(values["volume24h"])
        if "fundingRate" in values:
            if values["fundingRate"]:
                instrument.fundingRate = float(values["fundingRate"]) * 100
        if category != "spot":
            instrument.openInterest = values["openInterest"]
            if "option" in instrument.category:
                instrument.delta = values["delta"]
                instrument.vega = values["vega"]
                instrument.theta = values["theta"]
                instrument.gamma = values["gamma"]
                instrument.bidIv = values["bidIv"]
                instrument.askIv = values["askIv"]
                instrument.bidPrice = values["bidPrice"]
                instrument.askPrice = values["askPrice"]
                instrument.bidSize = values["bidSize"]
                instrument.askSize = values["askSize"]
            else:
                instrument.bidPrice = values["bid1Price"]
                instrument.askPrice = values["ask1Price"]
                instrument.bidSize = values["bid1Size"]
                instrument.askSize = values["ask1Size"]
            instrument.markPrice = values["markPrice"]

        instrument.confirm_subscription.add("ticker")

    def __update_account(self, values: dict) -> None:
        for value in values["data"]:
            for coin in value["coin"]:
                currency = (coin["coin"] + "." + value["accountType"], self.name)
                account = self.Account[currency]
                total = 0
                check = 0
                if "locked" in coin:
                    if coin["locked"] != "":
                        total += float(coin["locked"])
                        check += 1
                if "totalOrderIM" in coin and coin["totalOrderIM"]:
                    total += float(coin["totalOrderIM"])
                    check += 1
                if check:
                    account.orderMargin = total
                if "totalPositionIM" in coin:
                    account.positionMagrin = float(coin["totalPositionIM"])
                if "availableToWithdraw" in coin and coin["availableToWithdraw"]:
                    account.availableMargin = float(coin["availableToWithdraw"])
                if "equity" in coin and coin["equity"]:
                    account.marginBalance = float(coin["equity"])
                if "walletBalance" in coin and coin["walletBalance"]:
                    account.walletBalance = float(coin["walletBalance"])
                if "unrealisedPnl" in coin and coin["unrealisedPnl"]:
                    account.unrealisedPnl = float(coin["unrealisedPnl"])

    def __update_position(self, values: dict) -> None:
        for value in values["data"]:
            symbol = self.ticker[(value["symbol"], value["category"])]
            symbol = (symbol, self.name)
            instrument = self.Instrument[symbol]
            if value["side"] == "Sell":
                instrument.currentQty = -float(value["size"])
            else:
                instrument.currentQty = float(value["size"])
            instrument.avgEntryPrice = service.set_number(
                instrument=instrument, number=value["entryPrice"]
            )
            if value["liqPrice"] == "":
                instrument.marginCallPrice = var.DASH
            else:
                instrument.marginCallPrice = value["liqPrice"]
            instrument.unrealisedPnl = service.set_number(
                instrument=instrument, number=value["unrealisedPnl"]
            )

    def __handle_order(self, values):
        """
        1.
        If the order is executed immediately, Bybit treats it as a market
        order and the message with orderStatus "New" does not appear in this
        callback, instead Bybit sends orderStatus "Filled" and only after
        the execution_stream has already received the corresponding trade.
        The order with the status "Filled" is ignored by Tmatic because the
        normal sequence of messages from the websocket is: 1) new order,
        2) execution. So in case of a market order, Tmatic will always issue
        a warning "order with clOrdID <> not found".
        2.
        Bybit never sends OrderStatus "Replaced". If the order is moved to
        another price, OrderStatus will be "New". Tmatic looks for
        orderLinkId in var.orders array, and if orderLinkId matches clOrdID
        of any order, OrderStatus "Replaced" is assigned.
        """
        for value in values["data"]:
            if value["orderStatus"] == "Cancelled":
                orderStatus = "Canceled"
            elif value["orderStatus"] == "New":
                if value["orderLinkId"]:
                    for orders in var.orders.values():
                        for clOrdID in orders:
                            if clOrdID == value["orderLinkId"]:
                                orderStatus = "Replaced"
                                break
                        else:
                            continue
                        break
                    else:
                        orderStatus = "New"
                else:
                    orderStatus = "New"
            elif value["orderStatus"] == "Rejected":
                self.logger.info(
                    "Rejected order "
                    + value["symbol"]
                    + " "
                    + value["category"]
                    + " orderId "
                    + value["orderId"]
                )
                return
            else:
                orderStatus = ""
            if orderStatus:
                symbol = (self.ticker[(value["symbol"], value["category"])], self.name)
                row = {
                    "ticker": value["symbol"],
                    "category": value["category"],
                    "leavesQty": float(value["leavesQty"]),
                    "price": float(value["price"]),
                    "symbol": symbol,
                    "transactTime": service.time_converter(
                        int(value["updatedTime"]) / 1000, usec=True
                    ),
                    "side": value["side"],
                    "orderID": value["orderId"],
                    "execType": orderStatus,
                    "settlCurrency": self.Instrument[symbol].settlCurrency,
                    "orderQty": float(value["qty"]),
                    "cumQty": float(value["cumExecQty"]),
                }
                if value["orderLinkId"]:
                    row["clOrdID"] = value["orderLinkId"]
                self.transaction(row=row)

    def __handle_execution(self, values):
        for row in values["data"]:
            row["ticker"] = row["symbol"]
            row["symbol"] = (self.ticker[(row["symbol"], row["category"])], self.name)
            instrument = self.Instrument[row["symbol"]]
            row["execID"] = row["execId"]
            row["orderID"] = row["orderId"]
            row["lastPx"] = float(row["execPrice"])
            row["leavesQty"] = float(row["leavesQty"])
            row["transactTime"] = service.time_converter(
                time=int(row["execTime"]) / 1000, usec=True
            )
            row["commission"] = float(row["feeRate"])
            if row["orderLinkId"]:
                row["clOrdID"] = row["orderLinkId"]
            row["price"] = float(row["orderPrice"])
            row["market"] = self.name
            row["lastQty"] = float(row["execQty"])
            if row["execType"] == "Funding":
                if row["side"] == "Sell":
                    row["lastQty"] = -row["lastQty"]
            row["execFee"] = float(row["execFee"])
            if row["category"] == "spot":
                if row["commission"] > 0:
                    if row["side"] == "Buy":
                        row["feeCurrency"] = instrument.baseCoin
                    elif row["side"] == "Sell":
                        row["feeCurrency"] = instrument.quoteCoin
                else:
                    if row["IsMaker"]:
                        if row["side"] == "Buy":
                            row["feeCurrency"] = instrument.quoteCoin
                        elif row["side"] == "Sell":
                            row["feeCurrency"] = instrument.baseCoin
                    elif not row["IsMaker"]:
                        if row["side"] == "Buy":
                            row["feeCurrency"] = instrument.baseCoin
                        elif row["side"] == "Sell":
                            row["feeCurrency"] = instrument.quoteCoin
                row["settlCurrency"] = (row["feeCurrency"], self.name)
            else:
                row["settlCurrency"] = instrument.settlCurrency
            self.transaction(row=row)

    def exit(self):
        """
        Closes websocket
        """
        for category in self.categories:
            try:
                self.ws[category].exit()
            except Exception:
                pass
        try:
            self.ws_private.exit()
        except Exception:
            pass
        self.api_is_active = False
        self.logger.info("Websocket closed.")

    def transaction(self, **kwargs):
        """
        This method is replaced by transaction() from functions.py after the
        application is launched.
        """
        pass

    def _on_message(self, message):
        """
        Parse incoming messages. This method replaces the original Pybit API
        method to intercept websocket pings via the pinging variable.
        """
        message = json.loads(message)
        if self._is_custom_pong(message):
            self.pinging = "pong"
            return
        else:
            self.callback(message)

    def ping_pong(self):
        for category in self.categories:
            if self.ws[category].__class__.__name__ == "WebSocket":
                if self.ws[category].pinging != "pong":
                    return False
                else:
                    self.ws[category].pinging = "ping"
                self.ws[category]._send_custom_ping()
        if self.ws_private.pinging != "pong":
            return False
        else:
            self.ws_private.pinging = "ping"
        self.ws_private._send_custom_ping()

        return True

    def _subscribe(self, symbol: tuple) -> str:
        instrument = self.Instrument[symbol]
        ticker = instrument.ticker
        category = instrument.category
        if ticker == "option!":
            lst = service.select_option_strikes(
                index=self.instrument_index, instrument=instrument
            )
            ticker = list()
            for option in lst:
                instrument = self.Instrument[(option, self.name)]
                instrument.confirm_subscription = set()
                ticker.append(option)
        else:
            instrument.confirm_subscription = set()

        # ws connection

        timeout = 5
        while self.ws_wait[category]:
            timeout -= 0.1
            if timeout < 0:
                return service.unexpected_error(self)

            time.sleep(0.1)

        if not self.ws[category].__class__.__name__ == "WebSocket":
            self.ws_wait[category] = "wait"
            try:
                self.ws[category] = WebSocket(
                    testnet=self.testnet, channel_type=category
                )
                self.ws_wait[category] = ""
            except Exception as exception:
                Unify.error_handler(
                    self,
                    exception=exception,
                    verb="WebSocket",
                    path=category,
                )
            self.ws[category].pinging = "pong"

        # Linear

        if category == "linear":
            message = Message.WEBSOCKET_SUBSCRIPTION.format(
                NAME="Orderbook", CHANNEL=category + " " + ticker
            )
            self._put_message(message)
            try:
                self.ws[category].orderbook_stream(
                    depth=self.orderbook_depth[var.order_book_depth][category],
                    symbol=ticker,
                    callback=lambda x: self.__update_orderbook(
                        values=x["data"], category="linear"
                    ),
                )
            except Exception as exception:
                Unify.error_handler(
                    self,
                    exception=exception,
                    verb="WebSocket",
                    path=f"orderbook_stream {symbol}",
                )
            message = Message.WEBSOCKET_SUBSCRIPTION.format(
                NAME="Ticker", CHANNEL=category + " " + ticker
            )
            self._put_message(message)
            try:
                self.ws[category].ticker_stream(
                    symbol=ticker,
                    callback=lambda x: self.__update_ticker(
                        values=x["data"], category="linear"
                    ),
                )
            except Exception as exception:
                Unify.error_handler(
                    self,
                    exception=exception,
                    verb="WebSocket",
                    path=f"ticker_stream {symbol}",
                )

        # Inverse

        elif category == "inverse":
            message = Message.WEBSOCKET_SUBSCRIPTION.format(
                NAME="Orderbook", CHANNEL=category + " " + ticker
            )
            self._put_message(message)
            try:
                self.ws[category].orderbook_stream(
                    depth=self.orderbook_depth[var.order_book_depth][category],
                    symbol=ticker,
                    callback=lambda x: self.__update_orderbook(
                        values=x["data"], category="inverse"
                    ),
                )
            except Exception as exception:
                Unify.error_handler(
                    self,
                    exception=exception,
                    verb="WebSocket",
                    path=f"orderbook_stream {symbol}",
                )
            message = Message.WEBSOCKET_SUBSCRIPTION.format(
                NAME="Ticker", CHANNEL=category + " " + ticker
            )
            self._put_message(message)
            try:
                self.ws[category].ticker_stream(
                    symbol=ticker,
                    callback=lambda x: self.__update_ticker(
                        values=x["data"], category="inverse"
                    ),
                )
            except Exception as exception:
                Unify.error_handler(
                    self,
                    exception=exception,
                    verb="WebSocket",
                    path=f"ticker_stream {symbol}",
                )

        # Spot

        elif category == "spot":
            message = Message.WEBSOCKET_SUBSCRIPTION.format(
                NAME="Orderbook", CHANNEL=category + " " + ticker
            )
            self._put_message(message)
            try:
                self.ws[category].orderbook_stream(
                    depth=self.orderbook_depth[var.order_book_depth][category],
                    symbol=ticker,
                    callback=lambda x: self.__update_orderbook(
                        values=x["data"], category="spot"
                    ),
                )
            except Exception as exception:
                Unify.error_handler(
                    self,
                    exception=exception,
                    verb="WebSocket",
                    path=f"orderbook_stream {symbol}",
                )
            message = Message.WEBSOCKET_SUBSCRIPTION.format(
                NAME="Ticker", CHANNEL=category + " " + ticker
            )
            self._put_message(message)
            try:
                self.ws[category].ticker_stream(
                    symbol=ticker,
                    callback=lambda x: self.__update_ticker(
                        values=x["data"], category="spot"
                    ),
                )
            except Exception as exception:
                Unify.error_handler(
                    self,
                    exception=exception,
                    verb="WebSocket",
                    path=f"ticker_stream {symbol}",
                )

        # Option

        elif category == "option":
            if var._series in symbol[0]:
                for item in self.symbol_list:
                    if item[0] in ticker:
                        ticker.remove(item[0])
                        message = Message.WEBSOCKET_SUBSCRIPTION_SKIPPED.format(
                            SKIPPED=item[0], SYMBOL=symbol[0]
                        )
                        self._put_message(message=message, warning="warning")
            message = Message.WEBSOCKET_SUBSCRIPTION.format(
                NAME="Orderbook", CHANNEL=category + " " + str(ticker)
            )
            self._put_message(message)
            try:
                self.ws[category].orderbook_stream(
                    depth=self.orderbook_depth[var.order_book_depth][category],
                    symbol=ticker,
                    callback=lambda x: self.__update_orderbook(
                        values=x["data"], category="option"
                    ),
                )
            except Exception as exception:
                Unify.error_handler(
                    self,
                    exception=exception,
                    verb="WebSocket",
                    path=f"orderbook_stream {symbol}",
                )
            message = Message.WEBSOCKET_SUBSCRIPTION.format(
                NAME="Ticker", CHANNEL=category + " " + str(ticker)
            )
            self._put_message(message)
            try:
                self.ws[category].ticker_stream(
                    symbol=ticker,
                    callback=lambda x: self.__update_ticker(
                        values=x["data"], category="option"
                    ),
                )
            except Exception as exception:
                Unify.error_handler(
                    self,
                    exception=exception,
                    verb="WebSocket",
                    path=f"ticker_stream {symbol}",
                )

        return self.logNumFatal

    def subscribe_symbol(self, symbol: tuple, answer=False) -> str:
        self._subscribe(symbol=symbol)

        # Confirmation

        instrument = self.Instrument[symbol]
        count = var.timeout
        slp = 0.1
        subscriptions = dict()

        if instrument.ticker == "option!":
            symbols = list()
            lst = service.select_option_strikes(
                index=self.instrument_index, instrument=instrument
            )
            for option in lst:
                subscriptions[(option, self.name)] = {"orderbook", "ticker"}
                symbols.append(option)
        else:
            subscriptions[symbol] = {"orderbook", "ticker"}
            symbols = symbol[0]
        s_copy = subscriptions.copy()
        while subscriptions:
            for symbol in s_copy:
                instrument = self.Instrument[symbol]
                if "ticker" in instrument.confirm_subscription:
                    if symbol in subscriptions:
                        del subscriptions[symbol]
            count -= slp
            if count < 0:
                message = ErrorMessage.FAILED_SUBSCRIPTION.format(SYMBOL=symbols)
                self._put_message(message=message)
                return "error"
            time.sleep(slp)
        if answer:
            message = Message.SUBSCRIPTION_ADDED.format(SYMBOL=symbols)
            self._put_message(message=message)

        return ""

    def _subscribe_args(self, args: list, symbol: tuple):
        instrument = self.Instrument[symbol]
        ticker = instrument.ticker
        arg_ticker = f"tickers.{ticker}"
        depth = self.orderbook_depth[var.order_book_depth][instrument.category]
        arg_orderbook = f"orderbook.{depth}.{ticker}"
        args.append(arg_ticker)
        args.append(arg_orderbook)

        return args

    def _subscribe_args_list(self, symbol: tuple) -> list:
        instrument = self.Instrument[symbol]
        ticker = instrument.ticker
        unsubscription_args = list()
        if ticker == "option!":
            lst = service.select_option_strikes(
                index=self.instrument_index, instrument=instrument
            )
            for option in lst:
                unsubscription_args = self._subscribe_args(
                    args=unsubscription_args, symbol=(option, self.name)
                )
        else:
            unsubscription_args = self._subscribe_args(
                args=unsubscription_args, symbol=symbol
            )

        return unsubscription_args

    def unsubscribe_symbol(self, symbol: tuple) -> str:
        unsubscription_args = self._subscribe_args_list(symbol=symbol)
        instrument = self.Instrument[symbol]
        category = instrument.category
        req_id = symbol[0]
        message = Message.WEBSOCKET_UNSUBSCRIBE.format(
            NAME="Orderbook, Ticker", CHANNEL=unsubscription_args
        )
        self.logger.info(message)
        if "option" in instrument.category:
            topic = str(sorted(unsubscription_args))
            self.unsubscriptions.add(topic)
        else:
            self.unsubscriptions.add(req_id)
            topic = req_id
        unsubscription_message = json.dumps(
            {"op": "unsubscribe", "req_id": req_id, "args": unsubscription_args}
        )
        self.ws[category].ws.send(unsubscription_message)

        # Confirmation

        count = 0
        slp = 0.1
        while topic in self.unsubscriptions:
            count += slp
            if count > var.timeout or not self.api_is_active:
                self.unsubscriptions.remove(topic)
                return "timeout"
            time.sleep(slp)
        for arg in unsubscription_args:
            if arg in self.ws[category].callback_directory:
                self.ws[category].callback_directory.pop(arg)

        return ""

    @staticmethod
    def _process_unsubscription_message(message):
        ws = var.market_object["Bybit"]
        if message["req_id"] in ws.unsubscriptions:
            ws.unsubscriptions.remove(message["req_id"])

    @staticmethod
    def _process_unsubscription_options(message):
        ws = var.market_object["Bybit"]
        if message["success"]:
            topic = str(sorted(message["data"]["successTopics"]))
            if topic in ws.unsubscriptions:
                ws.unsubscriptions.remove(topic)
                return True

        return False

    def _handle_incoming_message(self, message):
        if message.get("op") == "auth" or message.get("type") == "AUTH_RESP":
            self._process_auth_message(message)
        elif message.get("op") == "subscribe":
            self._process_subscription_message(message)
        elif message.get("type") == "COMMAND_RESP":
            if not Bybit._process_unsubscription_options(message):
                self._process_subscription_message(message)
        elif message.get("op") == "unsubscribe":
            Bybit._process_unsubscription_message(message)
        else:
            self._process_normal_message(message)

    def _put_message(self, message: str, warning=None) -> None:
        """
        Places an information message into the queue and the logger.
        """
        var.queue_info.put(
            {
                "market": self.name,
                "message": message,
                "time": datetime.now(tz=timezone.utc),
                "warning": warning,
            }
        )
        if not warning:
            self.logger.info(self.name + " - " + message)
        elif warning == "warning":
            self.logger.warning(self.name + " - " + message)
        else:
            self.logger.error(self.name + " - " + message)


_V5WebSocketManager._handle_incoming_message = Bybit._handle_incoming_message
