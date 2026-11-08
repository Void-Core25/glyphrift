class A:
    def save(self): ...


class B:
    def save(self): ...


class C:
    def save(self): ...


class D:
    def save(self): ...


class E:
    def save(self): ...


class Stack:
    def append(self, item): ...


def persist(obj):
    obj.save()  # => calls ?obj.save unresolved


def collect(items):
    items.append(1)
