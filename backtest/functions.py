import os
from datetime import datetime

import services as service
from api.api import WS, Markets
from common.data import BotData, Bots
from common.variables import Variables as var
from display.messages import ErrorMessage


def get_instrument(ws: Markets, symbol: tuple):
    """
    When running backtesting outside main.py there is no connection to any
    market and no information about instruments is received. Therefore, we
    have to get information via an http request. This request is made only
    once, since the received information is saved in the database in the
    `backtest` table to speed up the program.
    """
    qwr = (
        "select * from backtest where SYMBOL ='"
        + symbol[0]
        + "' and MARKET = '"
        + ws.name
        + "';"
    )
    data = service.select_database(qwr)
    if not data:
        symbols = ws.Instrument.get_keys()
        if symbols == None or symbol not in symbols:
            WS.get_active_instruments(ws)
        service.add_symbol_database(instrument=ws.Instrument[symbol], table="backtest")
    else:
        data = data[0]
        instrument = ws.Instrument.add(symbol)
        service.set_symbol(instrument=instrument, data=data)


def load_backtest_data(market: str, symb: str, timefr: str, bot_name: str):
    def fill(header, record, num, line):
        if header in ["dt", "tm"]:
            record[header] = int(line[num])
        else:
            record[header] = float(line[num])

    bot = Bots[bot_name]
    for symbol in var.backtest_symbols:
        bot.backtest_data[symbol] = list()
        b_data: list = bot.backtest_data[symbol]
        filename = os.getcwd() + f"/backtest/data/{market}/{symb}/{timefr}.csv"
        print("\nLoading backtest data from", filename, "\n")
        with open(filename, "r") as file:
            headers = next(file).strip("\n").split(";")
            for line in file:
                line = line.strip("\n").split(";")
                record = dict()
                for num, header in enumerate(headers):
                    fill(header, record, num, line)
                b_data.append(record)

    # Checking if the sizes of all backtesting data records are the same.

    if len(var.backtest_symbols) > 1:
        reference_size = len(bot.backtest_data[var.backtest_symbols[0]])
        reference_symbol = var.backtest_symbols[0]
        for symbol in var.backtest_symbols:
            if len(bot.backtest_data[symbol]) != reference_size:
                message = ErrorMessage.CHECK_BACKTEST_DATA_SIZE.format(
                    REFERENCE=reference_symbol,
                    REFERENCE_NUMBER=reference_size,
                    SYMBOL=symbol,
                    NUMBER=len(bot.backtest_data[symbol]),
                )
                print(message)
                exit(1)


def run(bot: BotData, strategy: callable):
    symbols = list(bot.backtest_data.keys())
    size = len(bot.backtest_data[symbols[0]]) - 1
    for bot.iter in range(size):
        print(bot.iter)
        strategy()