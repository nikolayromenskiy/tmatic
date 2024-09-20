import threading
import time
import tkinter as tk
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from random import randint
from typing import Tuple, Union

import display.bot_menu as bot_menu
import services as service
from api.api import WS, Markets
from api.variables import Variables
from botinit.variables import Variables as robo
from common.data import BotData, Bots
from common.variables import Variables as var
from display.functions import info_display
from display.messages import ErrorMessage, Message
from display.variables import AutoScrollbar, SubTreeviewTable, TreeTable, TreeviewTable
from display.variables import Variables as disp


class Function(WS, Variables):
    sql_lock = threading.Lock()

    def calculate(
        self: Markets,
        symbol: tuple,
        price: float,
        qty: float,
        rate: float,
        fund: int,
        execFee: float = None,
    ) -> dict:
        """
        Calculates trade or funding value and commission.

        Parameters
        ----------
        self: Markets
            Market instance.
        symbol: tuple
            Instrument symbol in (symbol, market name) format, e.g.
            ("BTCUSD", "Bybit").
        price: float
            Price of the instrument.
        qty: float
            Quantity of the instrument, negative if sell.
        rate: float
            Comission or funding rate.
        fund: int
            1 - trade, 0 - funding is being calculated.
        execFee: float (optional)
            Some exchanges send the commission and funding already calculated
            in the "execFee" field, so this value will be returned as
            "commission" or "funding" value.

        Returns
        -------
        dict
            "sumreal" - trade value.
            "commiss" - payed commission for trade, negative if maker rebate.
            "funding: - funding value, negative if in favor of the trader.
        """
        instrument = self.Instrument[symbol]
        coef = instrument.valueOfOneContract * instrument.myMultiplier
        if instrument.category in ["inverse", "future reversed"]:
            sumreal = qty / price * fund
            if execFee is not None:
                commiss = execFee
                funding = execFee
            else:
                commiss = abs(qty) / price * rate
                funding = qty / price * rate
        elif instrument.category in ["spot", "spot linear"]:
            sumreal = 0
            if execFee is not None:
                commiss = execFee
            else:
                commiss = abs(qty) * price * rate
            funding = 0
        else:
            sumreal = -qty * price * fund
            if execFee is not None:
                commiss = execFee
                funding = execFee
            else:
                commiss = abs(qty) * price * rate
                funding = qty * price * rate

        return {
            "sumreal": sumreal * coef,
            "commiss": commiss * coef,
            "funding": funding * coef,
        }

    def add_symbol(self: Markets, symbol: str, ticker: str, category: str) -> None:
        if (symbol, self.name) not in self.Instrument.get_keys():
            WS.get_instrument(self, ticker=ticker, category=category)

    def kline_data_filename(self: Markets, symbol: tuple, timefr: str) -> str:
        return "data/" + symbol[0] + "_" + self.name + "_" + str(timefr) + ".txt"

    def save_kline_data(self: Markets, row: dict, symbol: tuple, timefr: int) -> None:
        filename = Function.kline_data_filename(self, symbol=symbol, timefr=timefr)
        zero = (6 - len(str(row["time"]))) * "0"
        data = (
            str(row["date"])
            + ";"
            + str(zero)
            + str(row["time"])
            + ";"
            + str(row["bid"])
            + ";"
            + str(row["ask"])
            + ";"
            + str(row["hi"])
            + ";"
            + str(row["lo"])
            + ";"
        )
        with open(filename, "a") as f:
            f.write(data + "\n")

    def noll(self: Markets, val: str, length: int) -> str:
        r = ""
        for _ in range(length - len(val)):
            r = r + "0"

        return r + val

    def transaction(self: Markets, row: dict, info: str = "") -> None:
        """
        Trades and funding processing
        """

        def handle_trade_or_delivery(row, emi, refer, clientID):
            results = self.Result[row["settlCurrency"]]
            lastQty = row["lastQty"]
            leavesQty = row["leavesQty"]
            if row["side"] == "Sell":
                lastQty = -row["lastQty"]
                leavesQty = -row["leavesQty"]
            calc = Function.calculate(
                self,
                symbol=row["symbol"],
                price=row["lastPx"],
                qty=lastQty,
                rate=row["commission"],
                fund=1,
                execFee=row["execFee"],
            )
            if refer == "Delivery":
                calc["commiss"] = 0
            instrument = self.Instrument[row["symbol"]]
            instrument.volume += abs(lastQty)
            instrument.sumreal += calc["sumreal"]
            if emi in Bots.keys():
                if row["symbol"] not in Bots[emi].bot_positions:
                    service.fill_bot_position(
                        bot_name=emi,
                        symbol=row["symbol"],
                        instrument=instrument,
                        user_id=Markets[instrument.market].user_id,
                    )
                position = Bots[emi].bot_positions[row["symbol"]]
                if row["category"] != "spot":
                    position["position"] += lastQty
                    position["position"] = round(
                        position["position"],
                        instrument.precision,
                    )
                position["volume"] += abs(lastQty)
                position["commiss"] += calc["commiss"]
                position["sumreal"] += calc["sumreal"]
                position["ltime"] = row["transactTime"]
            results.commission += calc["commiss"]
            results.sumreal += calc["sumreal"]
            values = [
                row["execID"],
                emi,
                refer,
                row["settlCurrency"][0],
                row["symbol"][0],
                instrument.ticker,
                row["category"],
                self.name,
                row["side"],
                lastQty,
                leavesQty,
                row["price"],
                0,
                row["lastPx"],
                calc["sumreal"],
                calc["commiss"],
                clientID,
                row["transactTime"],
                self.user_id,
            ]
            service.insert_database(values=values, table="coins")
            message = {
                "SYMBOL": row["symbol"],
                "MARKET": row["market"],
                "TTIME": row["transactTime"],
                "SIDE": row["side"],
                "TRADE_PRICE": row["lastPx"],
                "QTY": abs(lastQty),
                "EMI": emi,
                "TICKER": instrument.ticker,
                "CATEGORY": instrument.category,
            }
            if not info:
                Function.trades_display(self, table=TreeTable.trades, val=message)
                if emi in Bots.keys():
                    Function.trades_display(
                        self, table=bot_menu.trade_treeTable[emi], val=message
                    )

        var.lock.acquire(True)
        try:
            Function.add_symbol(
                self,
                symbol=row["symbol"][0],
                ticker=row["ticker"],
                category=row["category"],
            )
            instrument = self.Instrument[row["symbol"]]
            if "clOrdID" in row:
                if "." not in row["clOrdID"]:
                    del row["clOrdID"]

            # Trade

            if row["execType"] == "Trade":
                if "clOrdID" in row:
                    dot = row["clOrdID"].find(".")
                    if (
                        dot == -1
                    ):  # The transaction was done from the exchange web interface,
                        # the clOrdID field is missing or clOrdID does not have EMI number
                        emi = row["symbol"][0]
                        refer = ""
                        if row["clOrdID"] == "":
                            clientID = 0
                        else:
                            try:
                                clientID = int(row["clOrdID"])
                            except Exception:
                                clientID = 0
                    else:
                        emi = row["clOrdID"][dot + 1 :]
                        clientID = row["clOrdID"][:dot]
                        refer = emi
                else:
                    emi = row["symbol"][0]
                    clientID = 0
                    refer = ""
                if emi not in Bots.keys():
                    emi = row["symbol"][0]
                data = service.select_database(  # read_database
                    "select EXECID from coins where EXECID='%s' and account=%s"
                    % (row["execID"], self.user_id),
                )
                if not data:
                    handle_trade_or_delivery(row, emi, refer, clientID)
                Function.orders_processing(self, row=row, info=info)

            # Delivery

            elif row["execType"] == "Delivery":
                results = self.Result[row["settlCurrency"]]
                pos = 0
                bot_list = []
                if row["side"] == "Sell":
                    lastQty = -row["lastQty"]
                else:
                    lastQty = row["lastQty"]
                for name in Bots.keys():
                    position = Bots[name].bot_positions
                    if (
                        row["symbol"] in position
                        and position[row["symbol"]]["position"] != 0
                    ):
                        qty = position[row["symbol"]]["position"]
                        if qty > 0:
                            row["side"] = "Sell"
                            pos += qty
                        else:
                            row["side"] = "Buy"
                            pos += qty
                        row["lastQty"] = abs(qty)
                        handle_trade_or_delivery(row, emi, "Delivery", 0)
                        bot_list.append(emi)
                diff = -(lastQty + pos)
                if diff != 0:
                    qwr = (
                        "select sum(QTY) as sum from coins where emi = '"
                        + row["symbol"][0]
                        + "' and MARKET = '"
                        + self.name
                        + "' and ACCOUNT = "
                        + str(self.user_id)
                        + " and side <> 'Fund'"
                        + ";"
                    )
                    data = service.select_database(query=qwr)[0]
                    if data["sum"] != diff:
                        message = ErrorMessage.IMPOSSIBLE_DATABASE_POSITION.format(
                            SYMBOL=row["symbol"][0],
                            DELIVERY=diff,
                            MARKET=self.name,
                            POSITION=data["sum"],
                        )
                        _put_message(market=self.name, message=message, warning="error")
                    else:
                        emi = row["symbol"][0]
                        if diff > 0:
                            row["side"] = "Sell"
                        else:
                            row["side"] = "Buy"
                        row["lastQty"] = abs(diff)
                        handle_trade_or_delivery(row, emi, "Delivery", 0)

            # Funding

            elif row["execType"] == "Funding":
                results = self.Result[row["settlCurrency"]]
                message = {
                    "SYMBOL": row["symbol"],
                    "TTIME": row["transactTime"],
                    "PRICE": row["price"],
                }
                position = 0
                instrument = self.Instrument[row["symbol"]]
                calc = Function.calculate(
                    self,
                    symbol=row["symbol"],
                    price=row["lastPx"],
                    qty=row["lastQty"],
                    rate=row["commission"],
                    fund=0,
                    execFee=row["execFee"],
                )
                emi = row["symbol"][0]
                message["CATEGORY"] = row["category"]
                message["MARKET"] = self.name
                message["EMI"] = emi
                message["QTY"] = row["lastQty"]
                message["COMMISS"] = calc["funding"]
                message["TICKER"] = instrument.ticker
                values = [
                    row["execID"],
                    emi,
                    "",
                    row["settlCurrency"][0],
                    row["symbol"][0],
                    instrument.ticker,
                    row["category"],
                    self.name,
                    "Fund",
                    row["lastQty"],
                    0,
                    row["lastPx"],
                    0,
                    row["price"],
                    calc["sumreal"],
                    calc["funding"],
                    0,
                    row["transactTime"],
                    self.user_id,
                ]
                service.insert_database(values=values, table="coins")
                results.funding += calc["funding"]
                if not info:
                    Function.funding_display(self, message)

            # New order

            elif row["execType"] == "New":
                if (
                    "clOrdID" not in row
                ):  # The order was placed from the exchange web interface
                    emi = ".".join(row["symbol"])
                    clOrdID = service.set_clOrdID(emi=emi)
                    service.fill_order(
                        emi=emi, clOrdID=clOrdID, category=row["category"], value=row
                    )
                    info = "Outside placement:"
                else:
                    info = ""
                Function.orders_processing(self, row=row, info=info)

            # Canceled order

            elif row["execType"] == "Canceled":
                Function.orders_processing(self, row=row)

            # Replaced order

            elif row["execType"] == "Replaced":
                Function.orders_processing(self, row=row)
        finally:
            var.lock.release()

    def orders_processing(self: Markets, row: dict, info: str = "") -> None:
        """
        Orders processing<--transaction()<--(trading_history() or get_exec())

        This function is called from transaction(), which in turn is called
        in two cases:

        1) A new row from the websocket with the corresponding execType is
        received:

        <New>       a new order has been successfully placed.
        <Trade>     the order has been executed, partially or completely.
        <Canceled>  the order has been cancelled.
        <Replaced>  the order has been moved to another price.

        2) A row from trading history (info parameter is "History") is
        received, and since trading history only informs about trades, there
        is only one possible execType:

        <Trade> the order has been executed, partially or completely.

        All orders in var.orders are assigned clOrdID, therefore the task is
        to find the required order and process it according to the execType.
        However, the current row does not necessarily contain clOrdID, so the
        possible scenarios are as follows:

        1) The order was sent via Tmatic.
            Websocket
        clOrdID is always present if the row was received from the websocket
        stream.
            Trading history
        clOrdID is always present for Bitmex and Bybit exchanges. Deribit
        exchange sends clOrdID only for trades for the last 5 days. In case
        of restoring the trading history for an earlier period, then the row
        without clOrdID.

        2) Order was not sent via Tmatic.
        clOrdID is missing. The application searches for order in var.orders
        by orderID. Failure does not always mean an error in case of
        restoring trading history.

        Parameters
        ----------
        self: Markets
            Market instance.
        row: dict
            Information received via web socket stream or downloaded trade
            history.
        info: str
            Possibly "History" in case of trading history or other additional
            information.
        """

        def order_not_found(clOrdID: str) -> None:
            message = (
                self.name
                + ": execType "
                + row["execType"]
                + " - order with clOrdID "
                + clOrdID
                + " not found."
            )
            _put_message(market=self.name, message=message, warning="warning")

        if "clOrdID" in row:
            if row["clOrdID"]:
                clOrdID = row["clOrdID"]
                dot = row["clOrdID"].find(".")
                if dot == -1:
                    emi = ".".join(row["symbol"])
                else:
                    emi = row["clOrdID"][dot + 1 :]
            else:
                """Possibly retrieved from Trading history"""
                clOrdID = "Empty"
                emi = "Not_found!"
        else:  # Retrieved from /execution or /execution/tradeHistory. The order was made
            # through the exchange web interface.
            for emi, values in var.orders.items():
                for clOrdID, value in values.items():
                    if value["orderID"] == row["orderID"]:
                        # emi and clOrdID were defined in var.orders
                        break
                else:
                    continue
                break
            else:
                """There is no order with this orderID in the var.orders. The
                order was not sent via Tmatic."""
                clOrdID = "Empty!"
                emi = "Not_found!"
        if "orderID" not in row:
            """Bitmex: orderID is missing when text='Closed to conform to lot
            size'. The last time this happened was on 31.05.2021."""
            row["orderID"] = row["text"]
        price = row["price"]
        info_q = ""
        info_p = ""
        if row["execType"] == "Canceled":
            info_p = price
            info_q = row["orderQty"] - row["cumQty"]
            if emi in var.orders and clOrdID in var.orders[emi]:
                var.queue_order.put(
                    {"action": "delete", "clOrdID": clOrdID, "market": self.name}
                )
                del var.orders[emi][clOrdID]
            else:
                order_not_found(clOrdID=clOrdID)
        else:
            if row["execType"] == "New":
                if "clOrdID" in row and row["clOrdID"]:
                    service.fill_order(
                        emi=emi, clOrdID=clOrdID, category=row["category"], value=row
                    )
                info_p = price
                info_q = row["orderQty"]
            elif row["execType"] == "Trade":
                info_p = row["lastPx"]
                info_q = row["lastQty"]
                if emi in var.orders and clOrdID in var.orders[emi]:
                    precision = self.Instrument[row["symbol"]].precision
                    var.orders[emi][clOrdID]["leavesQty"] -= row["lastQty"]
                    var.orders[emi][clOrdID]["leavesQty"] = round(
                        var.orders[emi][clOrdID]["leavesQty"], precision
                    )
                    if var.orders[emi][clOrdID]["leavesQty"] == 0:
                        del var.orders[emi][clOrdID]
                    var.queue_order.put(
                        {"action": "delete", "clOrdID": clOrdID, "market": self.name}
                    )
                else:
                    if info != "History":
                        order_not_found(clOrdID=clOrdID)
            elif row["execType"] == "Replaced":
                if emi in var.orders and clOrdID in var.orders[emi]:
                    var.orders[emi][clOrdID]["orderID"] = row["orderID"]
                    info_p = price
                    info_q = row["leavesQty"]
                    var.orders[emi][clOrdID]["leavesQty"] = row["leavesQty"]
                    var.queue_order.put(
                        {"action": "delete", "clOrdID": clOrdID, "market": self.name}
                    )
                else:
                    order_not_found(clOrdID=clOrdID)
            if emi in var.orders and clOrdID in var.orders[emi]:
                var.orders[emi][clOrdID]["price"] = price
                var.orders[emi][clOrdID]["transactTime"] = row["transactTime"]
        try:
            t = clOrdID.split(".")
            int(t[0])
            emi = ".".join(t[1:3])
        except ValueError:
            emi = clOrdID
        if info_q:
            info_q = Function.volume(self, qty=info_q, symbol=row["symbol"])
            info_p = Function.format_price(self, number=info_p, symbol=row["symbol"])
            if info != "History":
                message = (
                    row["execType"]
                    + " emi="
                    + emi
                    + ", side="
                    + row["side"]
                    + ", price="
                    + str(info_p)
                    + ", qty="
                    + info_q
                )
                var.queue_info.put(
                    {
                        "market": self.name,
                        "message": message,
                        "time": datetime.now(tz=timezone.utc),
                        "warning": None,
                        "emi": emi,
                    }
                )
            if info:
                info += " - "
            var.logger.info(
                self.name
                + " - "
                + info
                + row["execType"]
                + " - "
                + "side=%s, orderID=%s, clOrdID=%s, price=%s, qty=%s",
                row["side"],
                row["orderID"],
                clOrdID,
                str(info_p),
                info_q,
            )
        if emi in var.orders and clOrdID in var.orders[emi]:
            var.queue_order.put({"action": "put", "order": var.orders[emi][clOrdID]})
            var.orders[emi].move_to_end(clOrdID)
        disp.bot_orders_processing = True

    def trades_display(
        self: Markets, val: dict, table: TreeviewTable, init=False
    ) -> Union[None, list]:
        """
        Update trades widget
        """
        Function.add_symbol(
            self,
            symbol=val["SYMBOL"][0],
            ticker=val["TICKER"],
            category=val["CATEGORY"],
        )
        tm = str(val["TTIME"])[2:]
        tm = tm.replace("-", "")
        tm = tm.replace("T", " ")[:15]
        row = [
            tm,
            val["SYMBOL"][0],
            val["CATEGORY"],
            val["MARKET"],
            val["SIDE"],
            Function.format_price(
                self,
                number=float(val["TRADE_PRICE"]),
                symbol=val["SYMBOL"],
            ),
            Function.volume(self, qty=val["QTY"], symbol=val["SYMBOL"]),
            val["EMI"],
        ]
        if init:
            return row
        table.insert(values=row, market=self.name, configure=val["SIDE"])
        if "No trades" in table.children:
            table.delete(iid="No trades")

    def funding_display(self: Markets, val: dict, init=False) -> Union[None, list]:
        """
        Update funding widget
        """
        Function.add_symbol(
            self,
            symbol=val["SYMBOL"][0],
            ticker=val["TICKER"],
            category=val["CATEGORY"],
        )
        tm = str(val["TTIME"])[2:]
        tm = tm.replace("-", "")
        tm = tm.replace("T", " ")[:15]
        row = [
            tm,
            val["SYMBOL"][0],
            val["CATEGORY"],
            val["MARKET"],
            Function.format_price(
                self,
                number=float(val["PRICE"]),
                symbol=val["SYMBOL"],
            ),
            "{:.7f}".format(-val["COMMISS"]),
            Function.volume(self, qty=val["QTY"], symbol=val["SYMBOL"]),
            val["EMI"],
        ]
        if init:
            return row
        configure = "Buy" if val["COMMISS"] <= 0 else "Sell"
        TreeTable.funding.insert(values=row, market=self.name, configure=configure)

    def orders_display(self: Markets, val: dict) -> None:
        """
        Update Orders widget
        """
        emi = val["emi"]
        tm = str(val["transactTime"])[2:]
        tm = tm.replace("-", "")
        tm = tm.replace("T", " ")[:15]
        row = [
            tm,
            val["symbol"][0],
            val["category"],
            val["market"],
            val["side"],
            Function.format_price(
                self,
                number=val["price"],
                symbol=val["symbol"],
            ),
            Function.volume(self, qty=val["leavesQty"], symbol=val["symbol"]),
            emi,
        ]
        clOrdID = val["clOrdID"]
        if clOrdID in TreeTable.orders.children:
            TreeTable.orders.delete(iid=clOrdID)
        TreeTable.orders.insert(
            values=row, market=self.name, iid=val["clOrdID"], configure=val["side"]
        )

    def volume(self: Markets, qty: Union[int, float], symbol: tuple) -> str:
        if qty in ["", "None"]:
            return qty
        if qty == 0:
            qty = "0"
        else:
            instrument = self.Instrument[symbol]
            qty = "{:.{precision}f}".format(qty, precision=instrument.precision)

        return qty

    def format_price(self: Markets, number: Union[float, str], symbol: tuple) -> str:
        if not isinstance(number, str):
            precision = self.Instrument[symbol].price_precision
            number = "{:.{precision}f}".format(number, precision=precision)
            if precision:
                dot = number.find(".")
                if dot == -1:
                    number = number + "."
                n = len(number) - 1 - number.find(".")
                for _ in range(precision - n):
                    number = number + "0"

        return number

    def kline_update_market(self: Markets, utcnow: datetime) -> None:
        """
        Processing timeframes
        """
        for symbol, kline in self.klines.items():
            instrument = self.Instrument[symbol]
            for timefr, values in kline.items():
                timefr_minutes = var.timeframe_human_format[timefr]
                if utcnow > values["time"] + timedelta(minutes=timefr_minutes):
                    Function.save_kline_data(
                        self,
                        row=values["data"][-1],
                        symbol=symbol,
                        timefr=timefr,
                    )
                    next_minute = int(utcnow.minute / timefr_minutes) * timefr_minutes
                    dt_now = utcnow.replace(minute=next_minute, second=0, microsecond=0)
                    values["data"].append(
                        {
                            "date": (utcnow.year - 2000) * 10000
                            + utcnow.month * 100
                            + utcnow.day,
                            "time": utcnow.hour * 10000 + utcnow.minute * 100,
                            "bid": instrument.bids[0][0],
                            "ask": instrument.asks[0][0],
                            "hi": instrument.asks[0][0],
                            "lo": instrument.bids[0][0],
                            "funding": instrument.fundingRate,
                            "datetime": dt_now,
                        }
                    )
                    values["time"] = dt_now

    def refresh_on_screen(self: Markets, utc: datetime) -> None:
        """
        Refresh information on screen
        """
        # adaptive_screen(self)
        if utc.hour != var.refresh_hour:
            service.select_database("select count(*) cou from robots")
            var.refresh_hour = utc.hour
            var.logger.info("Emboldening SQLite")
        current_time = time.gmtime()
        if current_time.tm_sec != disp.last_gmtime_sec:
            # We are here once a second
            asctime = time.asctime(current_time)
            disp.label_time[
                "text"
            ] = f"CPU: {service.Variables.cpu_usage}% MEM: {service.Variables.memory_usage}MB  |  {asctime[0:len(asctime) - 4]}"
            disp.last_gmtime_sec = current_time.tm_sec
        Function.refresh_tables(self)

    def refresh_tables(self: Markets) -> None:
        current_notebook_tab = disp.notebook.tab(disp.notebook.select(), "text")

        # Refresh instrument table

        service.count_orders()

        tree = TreeTable.instrument

        # d tm = datetime.now()
        for num, symbol in enumerate(self.symbol_list):
            instrument = self.Instrument[symbol]
            compare = [
                symbol[0],
                instrument.category,
                instrument.currentQty,
                instrument.avgEntryPrice,
                instrument.unrealisedPnl,
                instrument.marginCallPrice,
                instrument.state,
                instrument.volume24h,
                instrument.expire,
                instrument.fundingRate,
            ]
            if compare != tree.cache[num]:
                tree.cache[num] = compare
                row = [
                    symbol[0],
                    instrument.category,
                    Function.volume(self, qty=instrument.currentQty, symbol=symbol),
                    Function.format_price(
                        self, number=instrument.avgEntryPrice, symbol=symbol
                    ),
                    format_number(number=instrument.unrealisedPnl),
                    instrument.marginCallPrice,
                    instrument.state,
                    Function.humanFormat(self, instrument.volume24h, symbol),
                    instrument.expire,
                    instrument.fundingRate,
                ]
                tree.update(row=num, values=row)
        # d print("___instrument", datetime.now() - tm)

        # Refresh orderbook table

        tree = TreeTable.orderbook

        # d tm = datetime.now()

        def display_order_book_values(
            val: list,
            start: int,
            end: int,
            direct: int,
            side: str,
        ) -> None:
            count = 0
            for number in range(start, end, direct):
                if len(val) > count:
                    qty = Function.find_order(self, val[count][0], symbol=var.symbol)
                    if side == "bids":
                        compare = [val[count][0], val[count][1], qty]
                        if compare != tree.cache[number]:
                            tree.cache[number] = compare
                            row = [
                                Function.volume(
                                    self, qty=val[count][1], symbol=var.symbol
                                ),
                                Function.format_price(
                                    self, number=val[count][0], symbol=var.symbol
                                ),
                                "",
                            ]
                            tree.update(row=number, values=row)
                            if qty:
                                TreeTable.orderbook.show_color_cell(
                                    text=Function.volume(
                                        self, qty=qty, symbol=var.symbol
                                    ),
                                    row=number,
                                    column=2,
                                    bg_color=disp.green_color,
                                    fg_color=disp.white_color,
                                )
                            else:
                                TreeTable.orderbook.hide_color_cell(
                                    row=number, column=2
                                )
                    else:
                        compare = [qty, val[count][0], val[count][1]]
                        if compare != tree.cache[number]:
                            tree.cache[number] = compare
                            row = [
                                "",
                                Function.format_price(
                                    self, number=val[count][0], symbol=var.symbol
                                ),
                                Function.volume(
                                    self, qty=val[count][1], symbol=var.symbol
                                ),
                            ]
                            tree.update(row=number, values=row)
                            if qty:
                                TreeTable.orderbook.show_color_cell(
                                    text=Function.volume(
                                        self, qty=qty, symbol=var.symbol
                                    ),
                                    row=number,
                                    column=0,
                                    bg_color=disp.red_color,
                                    fg_color=disp.white_color,
                                )
                            else:
                                TreeTable.orderbook.hide_color_cell(
                                    row=number, column=0
                                )
                else:
                    compare = ["", "", ""]
                    if compare != tree.cache[number]:
                        tree.cache[number] = compare
                        TreeTable.orderbook.hide_color_cell(row=number, column=0)
                        TreeTable.orderbook.hide_color_cell(row=number, column=2)
                        tree.update(row=number, values=compare)
                count += 1

        num = int(disp.num_book / 2)
        instrument = self.Instrument[var.symbol]
        display_order_book_values(
            val=instrument.bids,
            start=num,
            end=disp.num_book,
            direct=1,
            side="bids",
        )
        display_order_book_values(
            val=instrument.asks,
            start=num - 1,
            end=-1,
            direct=-1,
            side="asks",
        )
        # d print("___orderbook", datetime.now() - tm)

        # Refresh account table

        if current_notebook_tab == "Account":
            tree = TreeTable.account

            def form_result_line(compare):
                for num in range(1, 7):
                    compare[num] = format_number(compare[num])
                return compare

            # d tm = datetime.now()
            for market in var.market_list:
                ws = Markets[market]
                for settlCurrency in ws.Account.keys():
                    account = ws.Account[settlCurrency]
                    compare = [
                        settlCurrency[0],
                        account.walletBalance,
                        account.unrealisedPnl,
                        account.marginBalance,
                        account.orderMargin,
                        account.positionMagrin,
                        account.availableMargin,
                    ]
                    iid = market + settlCurrency[0]
                    if iid in tree.children_hierarchical[market]:
                        if iid not in tree.cache:
                            tree.cache[iid] = []
                        if compare != tree.cache[iid]:
                            tree.cache[iid] = compare.copy()
                            tree.update_hierarchical(
                                parent=market, iid=iid, values=form_result_line(compare)
                            )
                    else:
                        tree.insert_hierarchical(
                            parent=market, iid=iid, values=form_result_line(compare)
                        )
            # d print("___account", datetime.now() - tm)

        # Refresh result table

        elif current_notebook_tab == "Results":
            tree = TreeTable.results
            for market in var.market_list:
                ws = Markets[market]
                results = dict()
                for symbol in ws.symbol_list:
                    instrument = ws.Instrument[symbol]
                    if "spot" not in instrument.category:
                        if instrument.currentQty != 0:
                            value = Function.close_value(
                                ws, symbol=symbol, pos=instrument.currentQty
                            )
                            currency = instrument.settlCurrency
                            if currency in results:
                                results[currency] += value
                            else:
                                results[currency] = value
                for currency in ws.Result.keys():
                    result = ws.Result[currency]
                    result.result = 0
                    if currency in results:
                        result.result += results[currency]
                    compare = [
                        currency[0],
                        result.sumreal + result.result,
                        -result.commission,
                        -result.funding,
                    ]
                    iid = market + currency[0]
                    Function.update_result_line(
                        self,
                        iid=iid,
                        compare=compare,
                        market=market,
                        tree=tree,
                    )
            # d print("___result", datetime.now() - tm)

        # Refresh position table

        elif current_notebook_tab == "Positions":
            tree = TreeTable.position
            # d tm = datetime.now()
            pos_by_market = {market: [] for market in var.market_list}
            for name in Bots.keys():
                bot = Bots[name]
                for symbol in bot.bot_positions.keys():
                    pos_by_market[symbol[1]].append(bot.bot_positions[symbol])
            for market in pos_by_market.keys():
                rest = dict()
                rest_volume = dict()
                rest_sumreal = dict()
                pos = pos_by_market[market]
                notificate = True
                ws = Markets[market]
                for position in pos:
                    symbol = (position["symbol"], market)
                    if symbol not in rest:
                        rest[symbol] = 0
                        rest_volume[symbol] = 0
                        rest_sumreal[symbol] = 0
                    iid = position["emi"] + "!" + position["symbol"]
                    if position["position"] == 0:
                        if iid in tree.children_hierarchical[market]:
                            tree.delete_hierarchical(parent=market, iid=iid)
                    else:
                        notificate = False
                        pnl = Function.calculate_pnl(
                            ws,
                            symbol=symbol,
                            qty=position["position"],
                            sumreal=position["sumreal"],
                        )
                        rest[symbol] += position["position"]
                        rest_volume[symbol] += position["volume"]
                        rest_sumreal[symbol] += pnl
                        compare = [
                            position["emi"],
                            position["symbol"],
                            position["category"],
                            position["position"],
                            position["volume"],
                            pnl,
                        ]
                        Function.update_position_line(
                            self,
                            iid=iid,
                            compare=compare,
                            columns=[3, 4],
                            symbol=symbol,
                            market=market,
                            tree=tree,
                        )
                for symbol in ws.symbol_list:
                    instrument = ws.Instrument[symbol]
                    if "spot" not in instrument.category:
                        pnl = Function.calculate_pnl(
                            ws,
                            symbol=symbol,
                            qty=instrument.currentQty,
                            sumreal=instrument.sumreal,
                        )
                        if symbol in rest:
                            position = instrument.currentQty - rest[symbol]
                            volume = instrument.volume - rest_volume[symbol]
                            pnl = pnl - rest_sumreal[symbol]
                        else:
                            position = instrument.currentQty
                            volume = instrument.volume
                        iid = market + instrument.symbol
                        if position == 0:
                            if iid in tree.children_hierarchical[market]:
                                tree.delete_hierarchical(parent=market, iid=iid)
                        else:
                            notificate = False
                            compare = [
                                "----",
                                instrument.symbol,
                                instrument.category,
                                position,
                                volume,
                                pnl,
                            ]
                            Function.update_position_line(
                                self,
                                iid=iid,
                                compare=compare,
                                columns=[3, 4],
                                symbol=symbol,
                                market=market,
                                tree=tree,
                            )
                notification = market + "_notification"
                if notificate:
                    if notification not in tree.children_hierarchical[market]:
                        tree.insert_hierarchical(
                            parent=market, iid=notification, text="No positions"
                        )
                else:
                    if notification in tree.children_hierarchical[market]:
                        tree.delete_hierarchical(parent=market, iid=notification)
            # d print("___position", datetime.now() - tm)

        # Refresh bots table

        tree = TreeTable.bots

        # d tm = datetime.now()
        for name in Bots.keys():
            bot = Bots[name]
            compare = [
                name,
                bot.timefr,
                bot.state,
                service.bot_error(bot=bot),
                bot.updated,
            ]
            iid = name
            if iid in tree.children:
                if iid not in tree.cache:
                    tree.cache[iid] = []
                if compare != tree.cache[iid]:
                    tree.cache[iid] = compare.copy()
                    tree.update(row=iid, values=compare)
            else:
                tree.insert(iid=iid, values=compare, position="end")
        # d print("___bots", datetime.now() - tm)

        # Refresh market table

        tree = TreeTable.market

        # d tm = datetime.now()
        for num, name in enumerate(var.market_list):
            ws = Markets[name]
            status = str(ws.connect_count) + " " + "ONLINE"
            if not ws.api_is_active:
                status = "RELOADING..."
            compare = service.add_space([ws.name, ws.account_disp, status])
            if compare != tree.cache[name]:
                tree.cache[name] = compare
                tree.update(row=name, values=[compare], text=name)
                configure = "Market" if "ONLINE" in status else "Reload"
                TreeTable.market.paint(row=name, configure=configure)
        # d print("___market", datetime.now() - tm)

        # Refresh bot menu tables

        if disp.refresh_bot_info:
            current_bot_note_tab = disp.bot_note.tab(disp.bot_note.select(), "text")

            # Bot positions table

            if current_bot_note_tab == "Positions":
                # d tm = datetime.now()
                tree = TreeTable.bot_position
                pos_by_market = {market: False for market in var.market_list}
                if disp.bot_name:
                    bot = Bots[disp.bot_name]
                    for symbol, position in bot.bot_positions.items():
                        market = symbol[1]
                        if market not in tree.children:
                            tree.insert_parent(parent=market, configure="Gray")
                        iid = position["emi"] + "!" + position["symbol"]
                        if position["position"] == 0:
                            if iid in tree.children_hierarchical[market]:
                                tree.delete_hierarchical(parent=market, iid=iid)
                        else:
                            pos_by_market[market] = True
                            pnl = Function.calculate_pnl(
                                Markets[position["market"]],
                                symbol=symbol,
                                qty=position["position"],
                                sumreal=position["sumreal"],
                            )
                            compare = [
                                position["symbol"],
                                position["category"],
                                position["position"],
                                position["volume"],
                                pnl,
                            ]
                            Function.update_position_line(
                                self,
                                iid=iid,
                                compare=compare,
                                columns=[2, 3],
                                symbol=symbol,
                                market=market,
                                tree=tree,
                            )
                        for iid in list(tree.children_hierarchical[market]).copy():
                            lst = iid.split("!")
                            if len(lst) == 2:
                                if lst[0] != disp.bot_name:
                                    tree.delete_hierarchical(parent=market, iid=iid)
                    for market in list(tree.children).copy():
                        if market != "notification":
                            if not pos_by_market[market]:
                                tree.delete(iid=market)

                if not tree.children:
                    tree.insert_parent(parent="notification", text="No positions")
                else:
                    if len(tree.children) > 1 and "notification" in tree.children:
                        tree.delete(iid="notification")
                # d print("___bot position", datetime.now() - tm)

            # Bot orders table

            elif current_bot_note_tab == "Orders":
                if disp.bot_orders_processing:
                    bot_menu.refresh_bot_orders()
                    disp.bot_orders_processing = False

            # Bot results table

            elif current_bot_note_tab == "Results":
                tree = TreeTable.bot_results
                result_market = {market: False for market in var.market_list}
                if disp.bot_name:
                    bot = Bots[disp.bot_name]
                    for symbol, value in bot.bot_positions.items():
                        market = value["market"]
                        currency = value["currency"]
                        if market in var.market_list:
                            if not result_market[market]:
                                result_market[market] = dict()
                            pos_value = Function.close_value(
                                ws, symbol=symbol, pos=value["position"]
                            )
                            if currency in result_market[market]:
                                result_market[market][currency]["pnl"] += pos_value
                                result_market[market][currency]["commission"] += value[
                                    "commiss"
                                ]
                            else:
                                result_market[market][currency] = dict()
                                result_market[market][currency]["pnl"] = (
                                    value["sumreal"] + pos_value
                                )
                                result_market[market][currency]["commission"] = value[
                                    "commiss"
                                ]
                lines = set()
                for market, result in result_market.items():
                    if not result:
                        if market in tree.children:
                            tree.delete(iid=market)
                    else:
                        if market not in tree.children:
                            tree.insert_parent(parent=market, configure="Gray")
                        for currency, res in result.items():
                            compare = [
                                currency,
                                res["pnl"],
                                -res["commission"],
                            ]
                            iid = disp.bot_name + "!" + market + "!" + currency
                            lines.add(iid)
                            Function.update_result_line(
                                self,
                                iid=iid,
                                compare=compare,
                                market=market,
                                tree=tree,
                            )
                        for iid in list(tree.children_hierarchical[market]).copy():
                            if iid not in lines:
                                tree.delete_hierarchical(parent=market, iid=iid)
                if not tree.children:
                    tree.insert_parent(parent="notification", text="No results")
                else:
                    if len(tree.children) > 1 and "notification" in tree.children:
                        tree.delete(iid="notification")

    def update_result_line(
        self, iid: str, compare: list, market: str, tree: TreeviewTable
    ) -> None:
        def form_result_line(compare):
            for num in range(len(compare)):
                compare[num] = format_number(compare[num])

            return compare

        if iid in tree.children_hierarchical[market]:
            if compare != tree.cache[iid]:
                tree.cache[iid] = compare.copy()
                tree.update_hierarchical(
                    parent=market, iid=iid, values=form_result_line(compare)
                )
        else:
            tree.insert_hierarchical(
                parent=market, iid=iid, values=form_result_line(compare)
            )

    def update_position_line(
        self,
        iid: str,
        compare: list,
        columns: list,
        symbol: tuple,
        market: str,
        tree: TreeviewTable,
    ) -> None:
        def form_line(compare):
            for column in columns:
                compare[column] = Function.volume(
                    self,
                    qty=compare[column],
                    symbol=symbol,
                )
            num = columns[1] + 1
            compare[num] = format_number(number=compare[num])
            return compare

        if iid in tree.children_hierarchical[market]:
            if compare != tree.cache[iid]:
                tree.cache[iid] = compare.copy()
                tree.update_hierarchical(
                    parent=market, iid=iid, values=form_line(compare)
                )
        else:
            tree.insert_hierarchical(parent=market, iid=iid, values=form_line(compare))

    def close_value(self: Markets, symbol: tuple, pos: float) -> Union[float, None]:
        """
        Returns the value of the position if it is closed
        """
        instrument = self.Instrument[symbol]
        if pos > 0 and instrument.bids:
            close = instrument.bids[0][0]
        elif pos <= 0 and instrument.asks:
            close = instrument.asks[0][0]
        else:
            return
        calc = Function.calculate(
            self,
            symbol=symbol,
            price=close,
            qty=-pos,
            rate=0,
            fund=1,
        )

        return calc["sumreal"]

    def round_price(self: Markets, symbol: tuple, price: float, rside: int) -> float:
        """
        Round_price() returns rounded price: buy price goes down, sell price
        goes up according to 'tickSize'
        """
        instrument = self.Instrument[symbol]
        coeff = 1 / instrument.tickSize
        result = int(coeff * price) / coeff
        if rside < 0 and result < price:
            result += instrument.tickSize

        return result

    def post_order(
        self: Markets,
        name: str,
        symbol: tuple,
        emi: str,
        side: str,
        price: float,
        qty: int,
    ) -> str:
        """
        This function sends a new order
        """
        price_str = Function.format_price(self, number=price, symbol=symbol)
        clOrdID = ""
        if side == "Sell":
            qty = -qty
        if emi not in Bots.keys():
            emi = ".".join(symbol)
        clOrdID = service.set_clOrdID(emi=emi)
        var.logger.info(
            "Posting symbol="
            + str(symbol)
            + " clOrdID="
            + clOrdID
            + " side="
            + side
            + " price="
            + price_str
            + " qty="
            + str(qty)
        )
        WS.place_limit(
            self, quantity=qty, price=price_str, clOrdID=clOrdID, symbol=symbol
        )

        return clOrdID

    def put_order(
        self: Markets,
        emi: str,
        clOrdID: str,
        price: float,
        qty: int,
    ) -> str:
        """
        Replace orders
        """
        price_str = Function.format_price(
            self, number=price, symbol=var.orders[emi][clOrdID]["symbol"]
        )
        var.logger.info(
            "Putting orderID="
            + var.orders[emi][clOrdID]["orderID"]
            + " clOrdID="
            + clOrdID
            + " price="
            + price_str
            + " qty="
            + str(qty)
        )
        if price != var.orders[emi][clOrdID]["price"]:  # the price alters
            WS.replace_limit(
                self,
                quantity=qty,
                price=price_str,
                orderID=var.orders[emi][clOrdID]["orderID"],
                symbol=var.orders[emi][clOrdID]["symbol"],
            )

        return clOrdID

    def del_order(self: Markets, order: dict, clOrdID: str) -> int:
        """
        Del_order() function cancels orders
        """
        message = "Deleting orderID=" + order["orderID"] + " clOrdID=" + clOrdID
        var.logger.info(message)
        WS.remove_order(self, order=order)

        return self.logNumFatal

    def market_status(self: Markets, status: str, message: str, error=False) -> None:
        row = self.name  # var.market_list.index(self.name)
        if status == "ONLINE":
            line = [
                self.name,
                self.account_disp,
                str(self.connect_count) + " " + status,
            ]
        else:
            line = [self.name, self.account_disp, status]
        values = service.add_space(line)
        TreeTable.market.update(row=row, values=[values])
        if message:
            info_display(market=self.name, message=message)
        if error:
            TreeTable.market.paint(row=row, configure="Reload")
        else:
            TreeTable.market.paint(row=row, configure="Market")
        TreeTable.market.tree.update()

    def humanFormat(self: Markets, volNow: int, symbol: tuple) -> str:
        if volNow > 1000000000:
            volNow = "{:.2f}".format(round(volNow / 1000000000, 2)) + "B"
        elif volNow > 1000000:
            volNow = "{:.2f}".format(round(volNow / 1000000, 2)) + "M"
        elif volNow > 1000:
            volNow = "{:.2f}".format(round(volNow / 1000, 2)) + "K"
        else:
            volNow = Function.volume(self, qty=volNow, symbol=symbol)

        return volNow

    def find_order(self: Markets, price: float, symbol: str) -> Union[float, str]:
        qty = 0
        for values in var.orders.values():
            for value in values.values():
                if value["price"] == price and value["symbol"] == symbol:
                    qty += value["leavesQty"]
        if not qty:
            qty = ""

        return qty

    def calculate_pnl(
        self: Markets, symbol: tuple, qty: float, sumreal: float
    ) -> Union[float, str]:
        """
        Calculates current position pnl.

        Parameters
        ----------
        symbol: tuple
            Instrument symbol in (symbol, market name) format, e.g.
            ("BTCUSD", "Bybit").
        qty: float
            The quantity of the instrument in this position, negative if sell.
        sumreal:
            Position value.

        Returns
        -------
        float
            PNL value.
        """
        if qty == 0:
            return sumreal

        if symbol in self.symbol_list:
            if qty > 0:
                price = self.Instrument[symbol].bids[0][0]
            else:
                price = self.Instrument[symbol].asks[0][0]
            res = Function.calculate(
                self,
                symbol=symbol,
                price=price,
                qty=-qty,
                rate=0,
                fund=1,
            )
            pnl = sumreal + res["sumreal"]
        else:
            # symbol is not signed, so there is no order book and therefore
            # no position closing price. PNL cannot be calculated.
            pnl = "-"

        return pnl


def handler_order(event) -> None:
    tree = event.widget
    items = tree.selection()
    if items:
        tree.update()
        clOrdID = items[0]
        indx = TreeTable.orders.title.index("MARKET")
        ws = Markets[TreeTable.orders.tree.item(clOrdID)["values"][indx]]
        indx = TreeTable.orders.title.index("EMI")
        emi = TreeTable.orders.tree.item(clOrdID)["values"][indx]

        def on_closing() -> None:
            disp.order_window_trigger = "off"
            order_window.destroy()
            tree.selection_remove(items[0])

        def delete(order: dict, clOrdID: str) -> None:
            try:
                var.orders[emi][clOrdID]
            except KeyError:
                message = "Order " + clOrdID + " does not exist!"
                info_display(market=ws.name, message=message, warning="warning")
                var.logger.info(message)
                return
            if not ws.logNumFatal:
                Function.del_order(ws, order=order, clOrdID=clOrdID)
            else:
                info_display(
                    market=ws.name,
                    message="The operation failed. Websocket closed!",
                    warning="warning",
                )
            on_closing()

        def replace(clOrdID) -> None:
            try:
                var.orders[emi][clOrdID]
            except KeyError:
                message = "Order " + clOrdID + " does not exist!"
                info_display(ws.name, message)
                var.logger.info(message)
                return
            try:
                float(price_replace.get())
            except ValueError:
                info_display(
                    market=ws.name, message="Price must be numeric!", warning="warning"
                )
                return
            if not ws.logNumFatal:
                roundSide = var.orders[emi][clOrdID]["leavesQty"]
                if var.orders[emi][clOrdID]["side"] == "Sell":
                    roundSide = -roundSide
                price = Function.round_price(
                    ws,
                    symbol=var.orders[emi][clOrdID]["symbol"],
                    price=float(price_replace.get()),
                    rside=roundSide,
                )
                if price == var.orders[emi][clOrdID]["price"]:
                    info_display(
                        market=ws.name,
                        message="Price is the same but must be different!",
                        warning="warning",
                    )
                    return
                clOrdID = Function.put_order(
                    ws,
                    emi=emi,
                    clOrdID=clOrdID,
                    price=price,
                    qty=var.orders[emi][clOrdID]["leavesQty"],
                )
            else:
                info_display(
                    market=ws.name,
                    message="The operation failed. Websocket closed!",
                    warning="warning",
                )
            on_closing()

        if disp.order_window_trigger == "off":
            order = var.orders[emi][clOrdID]
            disp.order_window_trigger = "on"
            order_window = tk.Toplevel(disp.root, pady=10, padx=10)
            cx = disp.root.winfo_pointerx()
            cy = disp.root.winfo_pointery()
            order_window.geometry("+{}+{}".format(cx - 200, cy - 50))
            order_window.title("Cancel / Modify order ")
            order_window.protocol("WM_DELETE_WINDOW", on_closing)
            order_window.attributes("-topmost", 1)
            frame_up = tk.Frame(order_window)
            frame_dn = tk.Frame(order_window)
            label1 = tk.Label(frame_up, justify="left")
            order_price = Function.format_price(
                ws,
                number=var.orders[emi][clOrdID]["price"],
                symbol=var.orders[emi][clOrdID]["symbol"],
            )
            label1["text"] = (
                "market\t"
                + var.orders[emi][clOrdID]["symbol"][1]
                + "\nsymbol\t"
                + var.orders[emi][clOrdID]["symbol"][0]
                + "\nside\t"
                + var.orders[emi][clOrdID]["side"]
                + "\nclOrdID\t"
                + clOrdID
                + "\nprice\t"
                + order_price
                + "\nquantity\t"
                + Function.volume(
                    ws,
                    qty=var.orders[emi][clOrdID]["leavesQty"],
                    symbol=var.orders[emi][clOrdID]["symbol"],
                )
            )
            label_price = tk.Label(frame_dn)
            label_price["text"] = "Price "
            label1.pack(side="left")
            button = tk.Button(
                frame_dn,
                text="Delete order",
                command=lambda id=clOrdID: delete(clOrdID=id, order=order),
            )
            price_replace = tk.StringVar(frame_dn, order_price)
            entry_price = tk.Entry(
                frame_dn, width=10, bg=disp.bg_color, textvariable=price_replace
            )
            button_replace = tk.Button(
                frame_dn, text="Replace", command=lambda id=clOrdID: replace(id)
            )
            button.pack(side="right")
            label_price.pack(side="left")
            entry_price.pack(side="left")
            button_replace.pack(side="left")
            frame_up.pack(side="top", fill="x")
            frame_dn.pack(side="top", fill="x")
            change_color(color=disp.title_color, container=order_window)


def handler_orderbook(event) -> None:
    tree = event.widget
    items = tree.selection()
    if items:
        tree.update()
        tree.selection_remove(items[0])
    ws = Markets[var.current_market]
    if ws.name == "Bitmex" and var.symbol[1] == "spot":
        warning_window("Tmatic does not support spot trading on Bitmex.")
        return
    if not ws.api_is_active:
        if ws.name != "Fake":
            info_display(
                market=ws.name,
                message=ws.name + ": You cannot add new orders during a reboot.\n",
                warning="warning",
            )
        return
    disp.handler_orderbook_symbol = var.symbol

    def first_price(prices: list) -> float:
        if prices:
            return prices[0][0]
        else:
            return "None"

    def refresh() -> None:
        while disp.refresh_handler_orderbook:
            try:
                book_window.title(var.symbol)
            except Exception:
                pass
            if disp.handler_orderbook_symbol != var.symbol:
                ws = Markets[var.current_market]
                entry_price_ask.delete(0, "end")
                entry_price_ask.insert(
                    0,
                    Function.format_price(
                        ws,
                        number=first_price(ws.Instrument[var.symbol].asks),
                        symbol=var.symbol,
                    ),
                )
                entry_price_bid.delete(0, "end")
                entry_price_bid.insert(
                    0,
                    Function.format_price(
                        ws,
                        number=first_price(ws.Instrument[var.symbol].bids),
                        symbol=var.symbol,
                    ),
                )
                entry_quantity.delete(0, "end")
                entry_quantity.insert(
                    0,
                    Function.volume(
                        ws, qty=ws.Instrument[var.symbol].minOrderQty, symbol=var.symbol
                    ),
                )
                option_robots["menu"].delete(0, "end")
                options = list()
                for name in Bots.keys():
                    bot = Bots[name]
                    for symbol, pos in bot.bot_positions.items():
                        if symbol == var.symbol and pos["position"] != 0:
                            options.append(name)
                options.append(var.symbol[0])
                for option in options:
                    option_robots["menu"].add_command(
                        label=option,
                        command=lambda v=emi_number, optn=option: v.set(optn),
                    )
                emi_number.set("")
                disp.handler_orderbook_symbol = var.symbol
            time.sleep(0.1)

    def on_closing() -> None:
        disp.book_window_trigger = "off"
        disp.refresh_handler_orderbook = False
        book_window.destroy()

    def minimum_qty(qnt):
        minOrderQty = ws.Instrument[var.symbol].minOrderQty
        if qnt < minOrderQty:
            message = (
                "The "
                + str(var.symbol)
                + " quantity must be greater than or equal to "
                + Function.volume(ws, qty=minOrderQty, symbol=var.symbol)
            )
            warning_window(message)
            return "error"
        qnt_d = Decimal(str(qnt))
        qtyStep = Decimal(str(ws.Instrument[var.symbol].qtyStep))
        if qnt_d % qtyStep != 0:
            message = (
                "The "
                + str(var.symbol)
                + " quantity must be multiple to "
                + Function.volume(ws, qty=qtyStep, symbol=var.symbol)
            )
            warning_window(message)
            return "error"

    def callback_sell_limit() -> None:
        if quantity.get() and price_ask.get() and emi_number.get():
            try:
                qnt = abs(float(quantity.get()))
                price = float(price_ask.get())
                res = "yes"
            except Exception:
                warning_window("Fields must be numbers!")
                res = "no"
            if res == "yes" and qnt != 0:
                price = Function.round_price(
                    ws, symbol=var.symbol, price=price, rside=-qnt
                )
                if price <= 0:
                    warning_window("The price must be above zero.")
                    return
                if minimum_qty(qnt):
                    return
                Function.post_order(
                    ws,
                    name=ws.name,
                    symbol=var.symbol,
                    emi=emi_number.get(),
                    side="Sell",
                    price=price,
                    qty=qnt,
                )
        else:
            warning_window("Some of the fields are empty!")

    def callback_buy_limit() -> None:
        if quantity.get() and price_bid.get() and emi_number.get():
            try:
                qnt = abs(float(quantity.get()))
                price = float(price_bid.get())
                res = "yes"
            except Exception:
                warning_window("Fields must be numbers!")
                res = "no"
            if res == "yes" and qnt != 0:
                price = Function.round_price(
                    ws, symbol=var.symbol, price=price, rside=qnt
                )
                if price <= 0:
                    warning_window("The price must be above zero.")
                    return
                if minimum_qty(qnt):
                    return
                Function.post_order(
                    ws,
                    name=ws.name,
                    symbol=var.symbol,
                    emi=emi_number.get(),
                    side="Buy",
                    price=price,
                    qty=qnt,
                )
        else:
            warning_window("Some of the fields are empty!")

    if disp.book_window_trigger == "off":
        disp.book_window_trigger = "on"
        book_window = tk.Toplevel(disp.root, padx=10, pady=20)
        cx = disp.root.winfo_pointerx()
        cy = disp.root.winfo_pointery()
        book_window.geometry("+{}+{}".format(cx + 200, cy))
        book_window.title(var.symbol)
        book_window.protocol("WM_DELETE_WINDOW", on_closing)
        book_window.attributes("-topmost", 1)
        frame_quantity = tk.Frame(book_window)
        frame_market_ask = tk.Frame(book_window)
        frame_market_bid = tk.Frame(book_window)
        frame_robots = tk.Frame(book_window)
        sell_limit = tk.Button(
            book_window, text="Sell Limit", command=callback_sell_limit
        )
        buy_limit = tk.Button(book_window, text="Buy Limit", command=callback_buy_limit)
        quantity = tk.StringVar()
        price_ask = tk.StringVar()
        price_bid = tk.StringVar()
        entry_price_ask = tk.Entry(
            frame_market_ask, width=10, bg=disp.bg_color, textvariable=price_ask
        )
        entry_price_bid = tk.Entry(
            frame_market_bid, width=10, bg=disp.bg_color, textvariable=price_bid
        )
        instrument = ws.Instrument[var.symbol]
        entry_price_ask.insert(
            0,
            Function.format_price(
                ws,
                number=first_price(instrument.asks),
                symbol=var.symbol,
            ),
        )
        entry_price_bid.insert(
            0,
            Function.format_price(
                ws,
                number=first_price(instrument.bids),
                symbol=var.symbol,
            ),
        )
        entry_quantity = tk.Entry(
            frame_quantity, width=9, bg=disp.bg_color, textvariable=quantity
        )
        entry_quantity.insert(
            0,
            Function.volume(ws, qty=instrument.minOrderQty, symbol=var.symbol),
        )
        label_ask = tk.Label(frame_market_ask, text="Price:")
        label_bid = tk.Label(frame_market_bid, text="Price:")
        label_quantity = tk.Label(frame_quantity, text="Quantity:")
        label_robots = tk.Label(frame_robots, text="EMI:")
        emi_number = tk.StringVar()
        options = list()
        for name in Bots.keys():
            options.append(name)
        options.append(var.symbol[0])
        option_robots = tk.OptionMenu(frame_robots, emi_number, *options)
        frame_robots.grid(row=1, column=0, sticky="NSWE", columnspan=2, padx=10, pady=0)
        label_robots.pack(side="left")
        option_robots.pack()
        frame_quantity.grid(
            row=2,
            column=0,
            sticky="NSWE",
            columnspan=2,
            padx=10,
            pady=10,
        )
        label_quantity.pack(side="left")
        entry_quantity.pack()
        frame_market_ask.grid(row=3, column=0, sticky="NSWE")
        frame_market_bid.grid(row=3, column=1, sticky="NSWE")
        label_ask.pack(side="left")
        entry_price_ask.pack()
        label_bid.pack(side="left")
        entry_price_bid.pack()
        sell_limit.grid(row=4, column=0, sticky="NSWE", pady=10)
        buy_limit.grid(row=4, column=1, sticky="NSWE", pady=10)
        change_color(color=disp.title_color, container=book_window)
        disp.refresh_handler_orderbook = True
        t = threading.Thread(target=refresh)
        t.start()


def format_number(number: Union[float, str]) -> str:
    """
    Rounding a value from 2 to 8 decimal places.
    """
    if not isinstance(number, str):
        after_dot = max(2, 9 - max(1, len(str(int(number)))))
        number = "{:.{num}f}".format(number, num=after_dot)
        number = number.rstrip("0")
        number = number.rstrip(".")

    return number


def handler_instrument(event) -> None:
    tree = event.widget
    items = tree.selection()
    if items:
        ws = Markets[var.current_market]
        item = items[0]
        children = tree.get_children()
        row_position = children.index(item)
        if var.symbol != ws.symbol_list[row_position]:
            var.symbol = ws.symbol_list[row_position]
            TreeTable.orderbook.clear_color_cell()


def handler_account(event) -> None:
    tree = event.widget
    items = tree.selection()
    if items:
        tree.update()
        time.sleep(0.05)
        tree.selection_remove(items[0])


def handler_market(event) -> None:
    tree = event.widget
    items = tree.selection()
    if items:
        item = items[0]
        children = tree.get_children()
        row_position = children.index(item)
        shift = var.market_list[row_position]
        if shift != var.current_market:
            var.current_market = shift
            if Markets[var.current_market].api_is_active:
                TreeTable.market.paint(row=shift, configure="Market")
            else:
                TreeTable.market.paint(row=shift, configure="Reload")
            ws = Markets[var.current_market]
            var.symbol = ws.symbol_list[0]
            clear_tables()


def handler_subscription(event) -> None:
    """
    Opens a websocket subscription for an instrument selected in the
    Instruments menu.

    Parameters
    ----------
    market: str
        Market names such as Bitmex, Bybit.
    symbol: str
        Instrument symbol.
    """
    market = TreeTable.market.active_row
    symb = TreeTable.i_list.active_row
    symbol = (symb, market)
    print("______", market, symb)
    print(var.env[market]["SYMBOLS"])

    # var.env[market]["SYMBOLS"].append(symbol)
    value = ", ".join(map(lambda x: x[0], var.env[market]["SYMBOLS"]))
    """service.set_dotenv(
        dotenv_path=var.subscriptions,
        key=service.define_symbol_key(market=market),
        value=value,
    )"""
    TreeTable.market.del_sub(TreeTable.market)
    ws = Markets[market]
    if ws.subscribe_symbol(symbol=symbol):
        message = Message.SUBSCRIPTION_ADDED.format(SYMBOL=symbol)
        _put_message(market=market, message=message)
    else:
        message = ErrorMessage.FAILED_SUBSCRIPTION.format(SYMBOL=symbol)
        _put_message(market=market, message=message, warning="error")


def handler_bot(event) -> None:
    """
    Handles the event when the bot table is clicked.
    """
    tree = event.widget
    iid = tree.selection()
    if iid:
        iid = tree.selection()[0]
        disp.on_bot_menu("None")
        bot_menu.bot_manager.show(iid)


def warning_window(
    message: str, widget=None, item=None, width=400, height=150, title="Warning"
) -> None:
    def on_closing() -> None:
        warn_window.destroy()
        if widget:
            widget.selection_remove(item)

    warn_window = tk.Toplevel()
    warn_window.geometry(
        "{}x{}+{}+{}".format(
            width,
            height,
            disp.screen_width // 2 - width // 2 - randint(0, 7) * 15,
            disp.screen_height // 2 - height // 2,
        )
    )
    warn_window.title(title)
    warn_window.protocol("WM_DELETE_WINDOW", on_closing)
    warn_window.attributes("-topmost", 1)
    text = tk.Text(warn_window, wrap="word")
    scroll = AutoScrollbar(warn_window, orient="vertical")
    scroll.config(command=text.yview)
    text.config(yscrollcommand=scroll.set)
    text.insert("insert", message)
    text.grid(row=0, column=0, sticky="NSEW")
    scroll.grid(row=0, column=1, sticky="NS")
    warn_window.grid_columnconfigure(0, weight=1)
    warn_window.grid_rowconfigure(0, weight=1)


def change_color(color: str, container=None) -> None:
    line = container.__dict__.copy()
    if "children" in line:
        del line["children"]
    if "_last_child_ids" in line:
        del line["_last_child_ids"]
    line = str(line)
    if "notebook" not in line and "treeview" not in line:
        container.config(bg=color)
    for child in container.winfo_children():
        if child.winfo_children():
            change_color(color, child)
        elif type(child) is tk.Label:
            child.config(bg=color)
        elif type(child) is tk.Button:
            child.config(bg=color)


def init_bot_treetable_trades():
    for bot_name in Bots.keys():
        bot_menu.init_bot_trades(bot_name)


def clear_tables():
    var.lock_market_switch.acquire(True)
    ws = Markets[var.current_market]
    TreeTable.instrument.init(size=len(ws.symbol_list))
    TreeTable.orderbook.init(size=disp.num_book)
    TreeTable.instrument.set_selection()
    var.lock_market_switch.release()


def target_time(timeframe_sec):
    now = datetime.now(tz=timezone.utc).timestamp()
    target_tm = now + (timeframe_sec - now % timeframe_sec)

    return target_tm


def bot_in_thread(bot_name: str, target_tm: float, bot: BotData):
    """
    Bot entry point
    """
    while var.bot_thread_active[bot_name]:
        tm = time.time()
        if tm > target_tm:
            target_tm = target_time(bot.timefr_sec)
            bot.timefr_current = bot.timefr
            if disp.f9 == "ON":
                if bot.state == "Active":
                    if not bot.error_message:
                        if callable(robo.run[bot_name]):
                            # Calls strategy function in the strategy.py file
                            robo.run[bot_name]()
        time.sleep(1 - time.time() % 1)


def activate_bot_thread(bot_name: str) -> None:
    var.bot_thread_active[bot_name] = True
    bot = Bots[bot_name]
    target_tm = target_time(bot.timefr_sec)
    t = threading.Thread(
        target=bot_in_thread,
        args=(
            bot_name,
            target_tm,
            bot,
        ),
    )
    t.start()


def download_kline_data(
    self: Markets, start_time: datetime, target: datetime, symbol: tuple, timeframe: int
) -> Tuple[Union[list, None], Union[datetime, None]]:
    res = list()
    while target > start_time:
        data = WS.trade_bucketed(
            self, symbol=symbol, time=start_time, timeframe=timeframe
        )
        if isinstance(data, list):
            last = start_time
            res += data
            start_time = data[-1]["timestamp"] + timedelta(minutes=timeframe)
            if last == start_time or target <= data[-1]["timestamp"]:
                return res

        else:
            message = "When downloading trade/bucketed data, list was recieved. Reboot."
            var.logger.error(message)
            return service.unexpected_error(self)

    return res


def kline_update():
    while var.kline_update_active:
        utcnow = datetime.now(tz=timezone.utc)
        var.lock_kline_update.acquire(True)
        for market in var.market_list:
            ws = Markets[market]
            if ws.api_is_active:
                Function.kline_update_market(ws, utcnow=utcnow)
        var.lock_kline_update.release()
        rest = 1 - time.time() % 1
        time.sleep(rest)


def load_klines(
    self: Markets,
    symbol: tuple,
    timefr: str,
    klines: dict,
) -> Union[dict, None]:
    """
    Loading kline data from the exchange server. Data is recorded
    in files for each timeframe. Every time you reboot the files are
    overwritten.
    """
    filename = Function.kline_data_filename(self, symbol=symbol, timefr=timefr)
    with open(filename, "w") as f:
        f.write("date;time;open bid;open ask;hi;lo;" + "\n")
    target = datetime.now(tz=timezone.utc)
    target = target.replace(second=0, microsecond=0)
    timefr_minutes = var.timeframe_human_format[timefr]
    start_time = target - timedelta(
        minutes=robo.CANDLESTICK_NUMBER * timefr_minutes - timefr_minutes
    )
    delta = timedelta(
        minutes=target.minute % timefr_minutes + (target.hour * 60) % timefr_minutes
    )
    target -= delta

    # Loading timeframe data

    res = download_kline_data(
        self,
        start_time=start_time,
        target=target,
        symbol=symbol,
        timeframe=timefr_minutes,
    )

    if not res:
        message = str(symbol) + " " + str(timefr) + " kline data was not loaded!"
        var.logger.error(message)
        return None

    # Bitmex bug fix. Bitmex can send data with the next period's
    # timestamp typically for 5m and 60m.
    if target < res[-1]["timestamp"]:
        delta = timedelta(minutes=timefr_minutes)
        for r in res:
            r["timestamp"] -= delta

    # The 'klines' array is filled with timeframe data.

    if res[0]["timestamp"] > res[-1]["timestamp"]:
        res.reverse()
    klines[symbol][timefr]["data"] = []
    for num, row in enumerate(res):
        tm = row["timestamp"] - timedelta(minutes=timefr_minutes)
        klines[symbol][timefr]["data"].append(
            {
                "date": (tm.year - 2000) * 10000 + tm.month * 100 + tm.day,
                "time": tm.hour * 10000 + tm.minute * 100,
                "bid": float(row["open"]),
                "ask": float(row["open"]),
                "hi": float(row["high"]),
                "lo": float(row["low"]),
                "datetime": tm,
            }
        )
        if num < len(res) - 1:
            Function.save_kline_data(
                self,
                row=klines[symbol][timefr]["data"][-1],
                symbol=symbol,
                timefr=timefr,
            )
    klines[symbol][timefr]["time"] = tm

    return klines


def add_new_kline(self: Markets, symbol: tuple, bot_name: str, timefr: int) -> None:
    """
    Adds a new kline to the dictionary klines for the given exchange. If the
    given timefr already exists in the dictionary klines[symbol], then only
    adds bot_name to the set "robots", otherwise first creates a new timefr
    element. If the given symbol does not exist in the dictionary klines,
    then first adds the symbol to klines, then adds timefr to klines[symbol],
    and finally adds bot_name to the set "robots" in klines[symbol][timefr].
    """
    time = datetime.now(tz=timezone.utc)

    def append_new():
        self.klines[symbol][timefr] = {
            "time": time,
            "robots": set(),
            "open": 0,
            "data": [],
        }
        self.klines[symbol][timefr]["robots"].add(bot_name)

    try:
        self.klines[symbol][timefr]["robots"].add(bot_name)
    except KeyError:
        try:
            append_new()
        except KeyError:
            self.klines[symbol] = dict()
            append_new()


def init_market_klines(
    self: Markets,
) -> Union[dict, None]:
    """
    Downloads kline data from the endpoint of the specific exchange.
    """
    success = []

    def get_in_thread(symbol: tuple, timefr: str, klines: dict, number: int):
        nonlocal success
        res = load_klines(
            self,
            symbol=symbol,
            timefr=timefr,
            klines=klines,
        )
        if not res:
            return

        success[number] = "success"

    threads = []
    for symbol, timeframes in self.klines.items():
        for timefr in timeframes.keys():
            success.append(None)
            t = threading.Thread(
                target=get_in_thread,
                args=(symbol, timefr, self.klines, len(success) - 1),
            )

            threads.append(t)
            t.start()

    [thread.join() for thread in threads]
    for s in success:
        if not s:
            return

    return "success"


def init_bot_klines(bot_name: str) -> None:
    """
    Downloads kline data from exchange endpoints for a given bot. This
    happens when a specific bot's strategy.py file is updated.
    """
    success = []

    def get_in_thread(
        ws: Markets, symbol: tuple, timefr: str, klines: dict, number: int
    ):
        nonlocal success
        res = load_klines(
            ws,
            symbol=symbol,
            timefr=timefr,
            klines=klines,
        )
        if not res:
            return
        success[number] = "success"

    kline_to_download = list()
    for market in var.market_list:
        ws = Markets[market]
        for symbol, timeframes in ws.klines.items():
            for timefr, value in timeframes.items():
                if bot_name in value["robots"]:
                    if not value["data"]:
                        itm = {
                            "symbol": symbol,
                            "bot_name": bot_name,
                            "timefr": timefr,
                            "market": market,
                        }
                        kline_to_download.append(itm)
        """for item in ws.klin_set:
            if item[1] == bot_name:
                symbol = (item[0], market)
                if not ws.klines[symbol][item[2]]["data"]:
                    itm = {
                        "symbol": item[0],
                        "bot_name": item[1],
                        "timefr": item[2],
                        "market": market,
                    }
                    kline_to_download.append(itm)"""
    while kline_to_download:
        success = []
        threads = []
        for num, kline in enumerate(kline_to_download):
            success.append(None)
            ws = Markets[kline["market"]]
            t = threading.Thread(
                target=get_in_thread,
                args=(ws, kline["symbol"], kline["timefr"], ws.klines, num),
            )
            threads.append(t)
            t.start()
        [thread.join() for thread in threads]
        for num in range(len(success) - 1, -1, -1):
            if success[num]:
                kline_to_download.pop(num)
            else:
                message = (
                    kline_to_download[num]["market"]
                    + " "
                    + kline_to_download[num]["symbol"]
                    + " "
                    + kline_to_download[num]["timefr"]
                    + " kline is not loaded."
                )
                var.logger.error(message)
                time.sleep(2)


def remove_bot_klines(bot_name: str) -> None:
    """
    Removes the bot's subscription to kline data when deleting the bot in the
    Bot menu.
    """
    for market in var.market_list:
        ws = Markets[market]
        ws.klines
        for symbol, timeframes in ws.klines.items():
            copy = timeframes.copy()
            for timefr, value in copy.items():
                if bot_name in value["robots"]:
                    ws.klines[symbol][timefr]["robots"].remove(bot_name)
                    if not ws.klines[symbol][timefr]["robots"]:
                        var.lock_kline_update.acquire(True)
                        del ws.klines[symbol][timefr]
                        if not ws.klines:
                            del ws.klines[symbol]
                        var.lock_kline_update.release()


def setup_klines():
    """
    Initializing kline data on boot or reboot <f3>
    """

    def get_klines(ws: Markets, success):
        if init_market_klines(ws):
            success[ws.name] = "success"

    market_list = var.market_list.copy()
    while market_list:
        threads = []
        success = {market: None for market in market_list}
        for market in market_list:
            ws = Markets[market]
            success[market] = None
            t = threading.Thread(
                target=get_klines,
                args=(ws, success),
            )
            threads.append(t)
            t.start()
        [thread.join() for thread in threads]
        for market, value in success.items():
            if not value:
                var.logger.error(market + ": Klines are not loaded.")
                time.sleep(2)
            else:
                indx = market_list.index(market)
                market_list.pop(indx)


def _put_message(market: str, message: str, warning=None) -> None:
    """
    Places an information message into the queue and the logger.
    """
    var.queue_info.put(
        {
            "market": market,
            "message": message,
            "time": datetime.now(tz=timezone.utc),
            "warning": warning,
        }
    )
    if not warning:
        var.logger.info(market + " - " + message)
    elif warning == "warning":
        var.logger.warning(market + " - " + message)
    else:
        var.logger.error(market + " - " + message)


def clear_klines():
    """
    Erase all kline data.
    """
    for market in var.market_list:
        Markets[market].klines = dict()


def init_tables() -> None:
    ws = Markets[var.current_market]
    TreeTable.orderbook = TreeviewTable(
        frame=disp.frame_orderbook,
        name="orderbook",
        title=var.name_book,
        size=disp.num_book,
        bind=handler_orderbook,
        multicolor=True,
        autoscroll=True,
    )
    TreeTable.instrument = TreeviewTable(
        frame=disp.frame_instrument,
        name="instrument",
        title=var.name_instrument,
        size=len(ws.symbol_list),
        bind=handler_instrument,
        hide=["9", "8", "2"],
    )
    TreeTable.account = TreeviewTable(
        frame=disp.frame_account,
        name="account",
        title=var.name_account,
        bind=handler_account,
        hierarchy=True,
        lines=var.market_list,
        hide=["3", "5", "6"],
    )
    TreeTable.market = TreeviewTable(
        frame=disp.frame_market,
        name="market",
        title=var.name_market,
        size=var.market_list,
        style="market.Treeview",
        bind=handler_market,
        autoscroll=True,
        # subtable=TreeTable.i_category,
    )
    TreeTable.results = TreeviewTable(
        frame=disp.frame_results,
        name="results",
        title=var.name_results,
        hierarchy=True,
        lines=var.market_list,
    )
    TreeTable.position = TreeviewTable(
        frame=disp.frame_positions,
        name="position",
        title=var.name_position,
        hierarchy=True,
        lines=var.market_list,
    )
    TreeTable.bots = TreeviewTable(
        frame=disp.frame_bots,
        name="bots",
        title=var.name_bots,
        bind=handler_bot,
        hierarchy=False,
    )
    TreeTable.bot_menu = TreeviewTable(
        frame=bot_menu.menu_frame,
        name="bot_menu",
        title=var.name_bot_menu,
        style="menu.Treeview",
        bind=bot_menu.handler_bot_menu,
        autoscroll=True,
        hierarchy=True,
        rollup=True,
    )
    TreeTable.bot_info = TreeviewTable(
        frame=disp.frame_bot_parameters,
        name="bot_info",
        title=var.name_bot,
        bind=bot_menu.handler_bot_info,
        size=1,
        autoscroll=True,
    )
    TreeTable.bot_position = TreeviewTable(
        frame=disp.bot_positions,
        name="bot_position",
        title=var.name_bot_position,
        autoscroll=True,
        hierarchy=True,
        lines=var.market_list,
    )
    TreeTable.bot_results = TreeviewTable(
        frame=disp.bot_results,
        name="bot_results",
        title=var.name_bot_results,
        autoscroll=True,
        hierarchy=True,
    )
    TreeTable.instrument.set_selection()
    TreeTable.market.set_selection(index=var.current_market)
    init_bot_treetable_trades()


TreeTable.orders = TreeviewTable(
    frame=disp.frame_orders,
    name="orders",
    size=0,
    title=var.name_order,
    bind=handler_order,
    hide=["8", "3", "5"],
)
TreeTable.trades = TreeviewTable(
    frame=disp.frame_trades,
    name="trades",
    size=0,
    title=var.name_trade,
    bind=handler_account,
    hide=["8", "3", "5"],
)
TreeTable.funding = TreeviewTable(
    frame=disp.frame_funding,
    name="funding",
    size=0,
    title=var.name_funding,
    bind=handler_account,
    hide=["8", "3", "5"],
)
TreeTable.bot_orders = TreeviewTable(
    frame=disp.bot_orders,
    name="bot_orders",
    size=0,
    title=var.name_bot_order,
    bind=handler_order,
    hide=["8", "3", "5"],
)
TreeTable.i_list = SubTreeviewTable(
    frame=disp.frame_i_list,
    name="list",
    size=0,
    style="menu.Treeview",
    title=["Instrument"],
    bind=handler_subscription,
)
TreeTable.i_currency = SubTreeviewTable(
    frame=disp.frame_i_currency,
    name="currency",
    size=0,
    style="menu.Treeview",
    title=["Currency"],
    subtable=TreeTable.i_list,
)
TreeTable.i_category = SubTreeviewTable(
    frame=disp.frame_i_category,
    name="category",
    size=0,
    style="menu.Treeview",
    title=["Category"],
    subtable=TreeTable.i_currency,
)


# change_color(color=disp.title_color, container=disp.root)
