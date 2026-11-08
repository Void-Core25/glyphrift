from __future__ import annotations

from typing import TYPE_CHECKING  # => imports ?typing unresolved

from shop.util import format_price  # => imports shop.util resolved; imports shop.util.money.format_price resolved

if TYPE_CHECKING:
    from shop.cart import Cart  # => imports shop.cart resolved; imports shop.cart.Cart resolved


def total(cart: Cart):
    return sum(item.price for item in cart.items)


def label(value):
    return format_price(value)  # => calls shop.util.money.format_price resolved
