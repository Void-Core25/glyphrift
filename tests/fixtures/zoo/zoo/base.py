class Animal:
    def speak(self):
        return "..."

    def describe(self):
        return self.speak()  # => calls zoo.base.Animal.speak inferred


class Walker:
    def walk(self):
        return "walking"
