from shop import pricing  # => imports shop resolved; imports shop.pricing resolved
from shop.models import *  # => imports shop.models resolved


class Cart:
    def __init__(self):
        self.items = []

    def add_item(self, name, price):
        self.items.append(Product(name, price))  # => calls shop.models.Product inferred

    def total(self):
        return pricing.total(self)  # => calls shop.pricing.total resolved
