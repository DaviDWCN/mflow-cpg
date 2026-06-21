"""Utility module: string helpers used by service.py."""


def format_greeting(name: str) -> str:
    """Return a formatted greeting string."""
    message = "Hello, " + name + "!"
    return message


def format_farewell(name: str) -> str:
    """Return a formatted farewell string."""
    message = "Goodbye, " + name + "!"
    return message
