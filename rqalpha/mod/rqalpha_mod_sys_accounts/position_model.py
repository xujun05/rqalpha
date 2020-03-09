# -*- coding: utf-8 -*-
# 版权所有 2019 深圳米筐科技有限公司（下称“米筐科技”）
#
# 除非遵守当前许可，否则不得使用本软件。
#
#     * 非商业用途（非商业用途指个人出于非商业目的使用本软件，或者高校、研究所等非营利机构出于教育、科研等目的使用本软件）：
#         遵守 Apache License 2.0（下称“Apache 2.0 许可”），
#         您可以在以下位置获得 Apache 2.0 许可的副本：http://www.apache.org/licenses/LICENSE-2.0。
#         除非法律有要求或以书面形式达成协议，否则本软件分发时需保持当前许可“原样”不变，且不得附加任何条件。
#
#     * 商业用途（商业用途指个人出于任何商业目的使用本软件，或者法人或其他组织出于任何目的使用本软件）：
#         未经米筐科技授权，任何个人不得出于任何商业目的使用本软件（包括但不限于向第三方提供、销售、出租、出借、转让本软件、
#         本软件的衍生产品、引用或借鉴了本软件功能或源代码的产品或服务），任何法人或其他组织不得出于任何目的使用本软件，
#         否则米筐科技有权追究相应的知识产权侵权责任。
#         在此前提下，对本软件的使用同样需要遵守 Apache 2.0 许可，Apache 2.0 许可与本许可冲突之处，以本许可为准。
#         详细的授权流程，请联系 public@ricequant.com 获取。

from typing import Tuple, Optional
from datetime import date
from functools import lru_cache

from rqalpha.model.trade import Trade
from rqalpha.const import POSITION_DIRECTION, SIDE, POSITION_EFFECT, DEFAULT_ACCOUNT_TYPE
from rqalpha.environment import Environment
from rqalpha.portfolio.base_position import BasePosition, PositionProxy
from rqalpha.data.data_proxy import DataProxy
from rqalpha.utils.logger import user_system_log
from rqalpha.utils.class_helper import deprecated_property
from rqalpha.utils.i18n import gettext as _


def _int_to_date(d):
    r, d = divmod(d, 100)
    y, m = divmod(r, 100)
    return date(year=y, month=m, day=d)


class StockPosition(BasePosition):
    __repr_properties__ = [
        "order_book_id", "direction", "market_value"
    ]

    dividend_reinvestment = False
    cash_return_by_stock_delisted = True
    t_plus_enabled = True
    enable_position_validator = True

    def __init__(self, order_book_id, direction, init_quantity=0):
        super(StockPosition, self).__init__(order_book_id, direction, init_quantity)
        self._dividend_receivable = None
        self._pending_transform = None

    dividend_receivable = property(lambda self: self._dividend_receivable[1] if self._dividend_receivable else 0)
    receivable = property(lambda self: self.dividend_receivable)
    market_value =  property(lambda self: self.last_price * self.quantity)
    margin = property(lambda self: 0)
    equity = property(lambda self: self.market_value)

    @property
    def closable(self):
        order_quantity = sum(o for o in self._open_orders if o.position_effect in (
            POSITION_EFFECT.CLOSE, POSITION_EFFECT.CLOSE_TODAY, POSITION_EFFECT.EXERCISE
        ))
        if self.t_plus_enabled:
            return self.quantity - order_quantity - self._non_closable
        return self.quantity - order_quantity

    @property
    def position_validator_enabled(self):
        return self.enable_position_validator

    @property
    def trading_pnl(self):
        trade_quantity = self._today_quantity + (self._old_quantity - self._logical_old_quantity)
        return trade_quantity * self.last_price - self._trade_cost

    @property
    def position_pnl(self):
        return self._logical_old_quantity * (self.last_price - self.prev_close)

    def set_state(self, state):
        super(StockPosition, self).set_state(state)
        self._dividend_receivable = state.get("dividend_receivable")
        self._pending_transform = state.get("pending_transform")

    def get_state(self):
        state = super(StockPosition, self).get_state()
        state.update({
            "dividend_receivable": self._dividend_receivable,
            "pending_transform": self._pending_transform,
        })

    def before_trading(self, trading_date):
        # type: (date) -> float
        if self.quantity == 0:
            return 0
        if self.direction != POSITION_DIRECTION.LONG:
            raise RuntimeError("direction of stock position {} is not supposed to be short".format(self._order_book_id))
        data_proxy = Environment.get_instance().data_proxy
        self._handle_dividend_book_closure(trading_date, data_proxy)
        delta_cash = self._handle_dividend_payable(trading_date)
        self._handle_split(trading_date, data_proxy)
        return delta_cash

    def apply_trade(self, trade):
        # type: (Trade) -> float
        # 返回总资金的变化量
        self._transaction_cost += trade.transaction_cost
        if trade.position_effect == POSITION_EFFECT.OPEN:
            if self.quantity < 0:
                if trade.last_quantity <= -1 * self.quantity:
                    self._avg_price = 0
                else:
                    self._avg_price = trade.last_price
            else:
                self._avg_price = (self.quantity * self._avg_price + trade.last_quantity * trade.last_price) / (
                        self.quantity + trade.last_quantity
                )
            self._today_quantity += trade.last_quantity
            self._trade_cost += trade.last_price * trade.last_quantity

            if self._market_tplus >= 1:
                self._non_closable += trade.last_quantity
            return (-1 * trade.last_price * trade.last_quantity) - trade.transaction_cost
        elif trade.position_effect == POSITION_EFFECT.CLOSE:
            self._today_quantity -= max(trade.last_quantity - self._old_quantity, 0)
            self._old_quantity -= min(trade.last_quantity, self._old_quantity)
            return trade.last_price * trade.last_quantity - trade.transaction_cost
        else:
            raise RuntimeError("StockPosition dose not support position_effect: {}".format(trade.position_effect))

    def settlement(self, trading_date):
        # type: (date) -> Tuple[float, Optional[Trade]]
        self._old_quantity += self._today_quantity
        self._logical_old_quantity = self._old_quantity
        self._today_quantity = self._trade_cost = self._transaction_cost = self._non_closable = 0
        self._prev_close = self.last_price

        if self.quantity == 0:
            return 0, None
        if self.direction != POSITION_DIRECTION.LONG:
            raise RuntimeError("direction of stock position {} is not supposed to be short".format(self._order_book_id))
        data_proxy = Environment.get_instance().data_proxy
        next_date = data_proxy.get_next_trading_date(trading_date)
        instrument = data_proxy.instruments(self._order_book_id)
        delta_cash = 0
        if instrument.de_listed_at(next_date):
            try:
                transform_data = data_proxy.get_share_transformation(self._order_book_id)
            except NotImplementedError:
                pass
            else:
                if transform_data is not None:
                    successor, conversion_ratio = transform_data
                    virtual_trade = Trade.__from_create__(
                        order_id=None,
                        price=self.avg_price / conversion_ratio,
                        amount=self.quantity * conversion_ratio,
                        side=SIDE.BUY,
                        position_effect=POSITION_EFFECT.OPEN,
                        order_book_id=successor
                    )
                    return 0, virtual_trade
            if self.cash_return_by_stock_delisted:
                delta_cash = self.market_value

            self._today_quantity = self._old_quantity = 0
        return delta_cash, None

    def calc_close_today_amount(self, trade_amount):
        return 0

    @property
    def _market_tplus(self):
        if self._market_tplus_ is None:
            env = Environment.get_instance()
            self._market_tplus_ = env.data_proxy.instruments(self._order_book_id).market_tplus
        return self._market_tplus_

    def _handle_dividend_book_closure(self, trading_date, data_proxy):
        # type: (date, DataProxy) -> None
        last_date = data_proxy.get_previous_trading_date(trading_date)
        dividend = data_proxy.get_dividend_by_book_date(self._order_book_id, last_date)
        if dividend is None:
            return
        dividend_per_share = sum(dividend['dividend_cash_before_tax'] / dividend['round_lot'])
        if dividend_per_share != dividend_per_share:
            raise RuntimeError("Dividend per share of {} is not supposed to be nan.".format(self._order_book_id))
        self._avg_price -= dividend_per_share

        try:
            payable_date = _int_to_date(dividend["payable_date"][0])
        except ValueError:
            payable_date = _int_to_date(dividend["ex_dividend_date"][0])

        self._dividend_receivable = (payable_date, self.quantity * dividend_per_share)

    def _handle_dividend_payable(self, trading_date):
        # type: (date) -> float
        # 返回总资金的变化量
        if not self._dividend_receivable:
            return 0
        payable_date, dividend_value = self._dividend_receivable
        if payable_date != trading_date:
            return 0
        self._dividend_receivable = None
        if self.dividend_reinvestment:
            last_price = self.last_price
            self.apply_trade(Trade.__from_create__(
                None, last_price, dividend_value / last_price, SIDE.BUY, POSITION_EFFECT.OPEN, self._order_book_id
            ))
            return 0
        else:
            return dividend_value

    def _handle_split(self, trading_date, data_proxy):
        ratio = data_proxy.get_split_by_ex_date(self._order_book_id, trading_date)
        if ratio is None:
            return
        self._today_quantity *= ratio
        self._old_quantity *= ratio
        self._avg_price /= ratio


class FuturePosition(BasePosition):
    enable_position_validator = True

    old_quantity = property(lambda self: self._old_quantity)
    today_quantity = property(lambda self: self._today_quantity)
    market_value = property(lambda self: self.last_price * self.quantity * self.contract_multiplier)
    margin = property(lambda self: self.market_value * self.margin_rate)

    @property
    def position_validator_enabled(self):
        return self.enable_position_validator

    @property
    @lru_cache()
    def contract_multiplier(self):
        return self._instrument.contract_multiplier

    @property
    @lru_cache()
    def margin_rate(self):
        return self._instrument.margin_rate * Environment.get_instance().config.base.margin_multiplier

    @property
    def equity(self):
        # type: () -> float
        return self.quantity * (self.last_price - self._avg_price) * self.contract_multiplier * self._direction_factor

    @property
    def trading_pnl(self):
        trade_quantity = self._today_quantity + (self._old_quantity - self._logical_old_quantity)
        return self.contract_multiplier * (trade_quantity * self.last_price - self._trade_cost) * self._direction_factor

    @property
    def position_pnl(self):
        quantity = self._logical_old_quantity
        if quantity == 0:
            return 0
        return quantity * self.contract_multiplier * (self.last_price - self.prev_close) * self._direction_factor

    def calc_close_today_amount(self, trade_amount):
        close_today_amount = trade_amount - self.old_quantity
        return max(close_today_amount, 0)

    def before_trading(self, trading_date):
        # type: (date) -> float
        return 0

    def apply_trade(self, trade):
        self._transaction_cost += trade.transaction_cost
        if trade.position_effect == POSITION_EFFECT.OPEN:
            if self.quantity < 0:
                if trade.last_quantity <= -1 * self.quantity:
                    self._avg_price = 0
                else:
                    self._avg_price = trade.last_price
            else:
                self._avg_price = (self.quantity * self._avg_price + trade.last_quantity * trade.last_price) / (
                        self.quantity + trade.last_quantity
                )
            self._today_quantity += trade.last_quantity
            self._trade_cost += trade.last_price * trade.last_quantity
            return -1 * trade.transaction_cost
        else:
            if trade.position_effect == POSITION_EFFECT.CLOSE_TODAY:
                self._today_quantity -= trade.last_quantity
            elif trade.position_effect == POSITION_EFFECT.CLOSE:
                self._today_quantity -= max(trade.last_quantity - self._old_quantity, 0)
                self._old_quantity -= min(trade.last_quantity, self._old_quantity)
            else:
                raise RuntimeError("FuturePosition dose not support position_effect: {}".format(trade.position_effect))
            self._trade_cost -= trade.last_price * trade.last_quantity
            return -1 * trade.transaction_cost + (
                trade.last_price - self._avg_price
            ) * trade.last_quantity * self.contract_multiplier * self._direction_factor

    def settlement(self, trading_date):
        # type: (date) -> Tuple[float, Optional[Trade]]
        self._old_quantity += self._today_quantity
        self._logical_old_quantity = self._old_quantity
        self._today_quantity = self._trade_cost = self._transaction_cost = self._non_closable = 0
        if self.quantity == 0:
            return 0, None
        data_proxy = Environment.get_instance().data_proxy
        instrument = data_proxy.instruments(self._order_book_id)
        next_date = data_proxy.get_next_trading_date(trading_date)
        delta_cash = self.equity
        if instrument.de_listed_at(next_date):
            user_system_log.warn(_(u"{order_book_id} is expired, close all positions by system").format(
                order_book_id=self._order_book_id
            ))
            self._today_quantity = self._old_quantity = 0
        self._avg_price = self._prev_close = self.last_price
        return delta_cash, None


class StockPositionProxy(PositionProxy):

    __abandon_properties__ = PositionProxy.__abandon_properties__ + [
        "margin"
    ]

    @property
    def type(self):
        return "STOCK"

    @property
    def quantity(self):
        return self._long.quantity

    @property
    def sellable(self):
        """
        [int] 该仓位可卖出股数。T＋1 的市场中sellable = 所有持仓 - 今日买入的仓位 - 已冻结
        """
        return self._long.closable

    @property
    def avg_price(self):
        """
        [float] 平均开仓价格
        """
        return self._long.avg_price

    @property
    def value_percent(self):
        """
        [float] 获得该持仓的实时市场价值在股票投资组合价值中所占比例，取值范围[0, 1]
        """
        accounts = Environment.get_instance().portfolio.accounts
        if DEFAULT_ACCOUNT_TYPE.STOCK not in accounts:
            return 0
        total_value = accounts[DEFAULT_ACCOUNT_TYPE.STOCK].total_value
        return 0 if total_value == 0 else self.market_value / total_value


class FuturePositionProxy(PositionProxy):

    __abandon_properties__ = PositionProxy.__abandon_properties__ +[
        "holding_pnl",
        "buy_holding_pnl",
        "sell_holding_pnl",
        "realized_pnl",
        "buy_realized_pnl",
        "sell_realized_pnl",
        "buy_avg_holding_price",
        "sell_avg_holding_price"
    ]

    @property
    def type(self):
        return "FUTURE"

    @property
    def margin_rate(self):
        return self._long.margin_rate

    @property
    def contract_multiplier(self):
        return self._long.contract_multiplier

    @property
    def buy_market_value(self):
        """
        [float] 多方向市值
        """
        return self._long.market_value

    @property
    def sell_market_value(self):
        """
        [float] 空方向市值
        """
        return self._short.market_value

    @property
    def buy_position_pnl(self):
        """
        [float] 多方向昨仓盈亏
        """
        return self._long.position_pnl

    @property
    def sell_position_pnl(self):
        """
        [float] 空方向昨仓盈亏
        """
        return self._short.position_pnl

    @property
    def buy_trading_pnl(self):
        """
        [float] 多方向交易盈亏
        """
        return self._long.trading_pnl

    @property
    def sell_trading_pnl(self):
        """
        [float] 空方向交易盈亏
        """
        return self._short.trading_pnl

    @property
    def buy_daily_pnl(self):
        """
        [float] 多方向每日盈亏
        """
        return self.buy_position_pnl + self.buy_trading_pnl

    @property
    def sell_daily_pnl(self):
        """
        [float] 空方向每日盈亏
        """
        return self.sell_position_pnl + self.sell_trading_pnl

    @property
    def buy_pnl(self):
        """
        [float] 买方向累计盈亏
        """
        return self._long.pnl

    @property
    def sell_pnl(self):
        """
        [float] 空方向累计盈亏
        """
        return self._short.pnl

    @property
    def buy_old_quantity(self):
        """
        [int] 多方向昨仓
        """
        return self._long.old_quantity

    @property
    def sell_old_quantity(self):
        """
        [int] 空方向昨仓
        """
        return self._short.old_quantity

    @property
    def buy_today_quantity(self):
        """
        [int] 多方向今仓
        """
        return self._long.today_quantity

    @property
    def sell_today_quantity(self):
        """
        [int] 空方向今仓
        """
        return self._short.today_quantity

    @property
    def buy_quantity(self):
        """
        [int] 多方向持仓
        """
        return self.buy_old_quantity + self.buy_today_quantity

    @property
    def sell_quantity(self):
        """
        [int] 空方向持仓
        """
        return self.sell_old_quantity + self.sell_today_quantity

    @property
    def margin(self):
        """
        [float] 保证金

        保证金 = 持仓量 * 最新价 * 合约乘数 * 保证金率

        股票保证金 = 市值 = 持仓量 * 最新价

        """
        return self._long.margin + self._short.margin

    @property
    def buy_margin(self):
        """
        [float] 多方向持仓保证金
        """
        return self._long.margin

    @property
    def sell_margin(self):
        """
        [float] 空方向持仓保证金
        """
        return self._short.margin

    @property
    def buy_avg_open_price(self):
        """
        [float] 多方向平均开仓价格
        """
        return self._long.avg_price

    @property
    def sell_avg_open_price(self):
        """
        [float] 空方向平均开仓价格
        """
        return self._short.avg_price

    @property
    def buy_transaction_cost(self):
        """
        [float] 多方向交易费率
        """
        return self._long.transaction_cost

    @property
    def sell_transaction_cost(self):
        """
        [float] 空方向交易费率
        """
        return self._short.transaction_cost

    @property
    def closable_today_sell_quantity(self):
        return self._long.today_closable

    @property
    def closable_today_buy_quantity(self):
        return self._long.today_closable

    @property
    def closable_buy_quantity(self):
        """
        [float] 可平多方向持仓
        """
        return self._long.closable

    @property
    def closable_sell_quantity(self):
        """
        [float] 可平空方向持仓
        """
        return self._short.closable

    holding_pnl = deprecated_property("holding_pnl", "position_pnl")
    buy_holding_pnl = deprecated_property("buy_holding_pnl", "buy_position_pnl")
    sell_holding_pnl = deprecated_property("sell_holding_pnl", "sell_position_pnl")
    realized_pnl = deprecated_property("realized_pnl", "trading_pnl")
    buy_realized_pnl = deprecated_property("buy_realized_pnl", "buy_trading_pnl")
    sell_realized_pnl = deprecated_property("sell_realized_pnl", "sell_trading_pnl")
    buy_avg_holding_price = deprecated_property("buy_avg_holding_price", "buy_avg_open_price")
    sell_avg_holding_price = deprecated_property("sell_avg_holding_price", "sell_avg_open_price")