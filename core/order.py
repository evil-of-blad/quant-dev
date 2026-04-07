"""
订单模型
"""
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from typing import Optional
import uuid


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    symbol: str
    side: OrderSide
    order_type: OrderType
    amount: float                    # 买卖数量（基础货币）
    price: float = 0.0               # 限价单价格，市价单填 0
    filled_price: float = 0.0        # 成交均价
    filled_amount: float = 0.0       # 实际成交量
    fee: float = 0.0                 # 手续费(USDT)
    status: OrderStatus = OrderStatus.PENDING
    order_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: datetime = field(default_factory=datetime.utcnow)
    filled_at: Optional[datetime] = None
    note: str = ""

    @property
    def cost(self) -> float:
        """订单名义价值"""
        p = self.filled_price if self.filled_price else self.price
        return p * self.amount

    def fill(self, price: float, amount: float, fee: float):
        self.filled_price = price
        self.filled_amount = amount
        self.fee = fee
        self.status = OrderStatus.FILLED
        self.filled_at = datetime.utcnow()
