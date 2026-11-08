from mylib.core import compute  # => imports mylib.core resolved; imports mylib.core.compute resolved


def test_compute():
    assert compute() == 42  # => calls mylib.core.compute resolved
