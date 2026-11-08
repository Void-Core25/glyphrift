import util  # => imports util resolved
from util import helper  # => imports util resolved; imports util.helper resolved
from util import other as renamed  # => imports util resolved; imports util.other resolved
import requests  # => imports ?requests unresolved


class Counter:
    def __init__(self):
        self.n = 0

    def bump(self):
        self.n += 1
        self.log()  # => calls app.Counter.log inferred

    def log(self):
        print(self.n)

    @staticmethod
    def make():
        return Counter()  # => calls app.Counter resolved


def main(callback):
    helper()  # => calls util.helper resolved
    renamed()  # => calls util.other resolved
    util.other()  # => calls util.other resolved
    counter = Counter()  # => calls app.Counter resolved
    counter.bump()  # => calls app.Counter.bump heuristic
    Counter.make()  # => calls app.Counter.make resolved
    callback()  # => calls ?callback unresolved
    requests.get("x")  # => calls ?requests.get unresolved
    len([])

    def inner():
        return helper()  # => calls util.helper resolved

    inner()  # => calls app.main.inner resolved
    missing()  # => calls ?missing unresolved
    return counter


def shadowed(helper):
    helper()  # => calls ?helper unresolved
