"""Service module: calls functions from utils.py (cross-file call)."""

from tests.fixtures.multi_file_python.utils import format_greeting


def greet_user(user_name: str) -> str:
    """Greet a user by calling format_greeting from utils."""
    result = format_greeting(user_name)
    return result


def process_request(data: dict) -> str:
    """Process a request by greeting the user in the data."""
    name = data.get("name", "World")
    greeting = greet_user(name)
    return greeting
