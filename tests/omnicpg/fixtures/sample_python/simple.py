"""Sample Python file for testing the OmniCPG analysis pipeline."""


def greet(name: str) -> str:
    """Return a greeting message."""
    message = "Hello, " + name
    return message


def classify(value: int) -> str:
    """Classify a value as positive, negative, or zero."""
    result = "positive" if value > 0 else "negative or zero"
    return result


class Calculator:
    """A simple calculator class."""

    def __init__(self, initial: int = 0) -> None:
        """Initialise with an optional starting value."""
        self.value = initial

    def add(self, amount: int) -> int:
        """Add *amount* to the stored value and return it."""
        self.value = self.value + amount
        return self.value


def loop_example() -> int:
    """Demonstrate a simple loop."""
    total = 0
    for i in range(10):
        total = total + i
    return total
