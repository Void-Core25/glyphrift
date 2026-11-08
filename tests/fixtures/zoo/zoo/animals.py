import threading  # => imports ?threading unresolved
import zoo.base as b  # => imports zoo.base resolved
from typing import Generic, TypeVar  # => imports ?typing unresolved
from zoo.base import Animal, Walker  # => imports zoo.base resolved; imports zoo.base.Animal resolved; imports zoo.base.Walker resolved

T = TypeVar("T")


def make_base():
    return Animal


class Dog(Animal, Walker):  # => inherits zoo.base.Animal resolved; inherits zoo.base.Walker resolved
    def speak(self):
        return super().speak() + "woof"  # => calls zoo.base.Animal.speak inferred

    def fetch(self):
        self.walk()  # => calls zoo.base.Walker.walk inferred
        return self.describe()  # => calls zoo.base.Animal.describe inferred


class Puppy(Dog):  # => inherits zoo.animals.Dog resolved
    def play(self):
        self.fetch()  # => calls zoo.animals.Dog.fetch inferred
        self.speak()  # => calls zoo.animals.Dog.speak inferred
        self.nonexistent()  # => calls ?self.nonexistent unresolved


class Frog(b.Animal):  # => inherits zoo.base.Animal resolved
    pass


class Tagged(Animal, metaclass=type):  # => inherits zoo.base.Animal resolved
    pass


class Box(Generic[T]):  # => inherits ?Generic[T] unresolved
    pass


class Odd(make_base()):  # => inherits ?make_base() unresolved; calls zoo.animals.make_base resolved
    pass


class Worker(threading.Thread):  # => inherits ?threading.Thread unresolved
    def run(self):
        self.start()  # => calls ?self.start unresolved
        super().run()  # => calls ?super().run unresolved
