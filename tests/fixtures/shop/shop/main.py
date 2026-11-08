import shop.util.money as m  # => imports shop.util.money resolved
from shop.cart import Cart  # => imports shop.cart resolved; imports shop.cart.Cart resolved
from shop import Product, money  # => imports shop resolved; imports shop.models.Product resolved; imports shop.util.money resolved


def run():
    cart = Cart()  # => calls shop.cart.Cart resolved
    cart.add_item("tea", 3)  # => calls shop.cart.Cart.add_item heuristic
    Product("x", 1)  # => calls shop.models.Product resolved
    money.format_price(3)  # => calls shop.util.money.format_price resolved
    m.format_price(4)  # => calls shop.util.money.format_price resolved
