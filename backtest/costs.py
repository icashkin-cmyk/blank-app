"""臺股現股交易成本。

- 手續費：成交金額 × 0.1425% × 折扣，無條件捨去至整數元，最低 20 元。
- 證交稅：賣出成交金額 × 0.3%，無條件捨去至整數元（非當沖）。
捨去方式與最低手續費依券商而定，此處為常見做法，非官方逐字規定。
"""
from dataclasses import dataclass
import math

FEE_RATE = 0.001425
SELL_TAX_RATE = 0.003
MIN_FEE = 20


@dataclass(frozen=True)
class CostModel:
    fee_discount: float = 1.0
    min_fee: int = MIN_FEE
    sell_tax_rate: float = SELL_TAX_RATE

    def fee(self, amount: float) -> int:
        return max(self.min_fee, math.floor(amount * FEE_RATE * self.fee_discount))

    def tax(self, amount: float) -> int:
        return math.floor(amount * self.sell_tax_rate)

    def max_shares(self, cash: float, price: float) -> int:
        """以現金與價格計算可買最大股數（含手續費）。"""
        shares = math.floor(cash / (price * (1 + FEE_RATE * self.fee_discount)))
        while shares > 0 and shares * price + self.fee(shares * price) > cash:
            shares -= 1
        return max(shares, 0)
