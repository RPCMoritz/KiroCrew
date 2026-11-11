"""STUB (red commit): a registry that binds nothing and a switch that is always off.

The next commit replaces this with the real registry. It exists so the new tests
import and then fail on their assertions.
"""

from __future__ import annotations


def is_bound_key_shape(key: str) -> bool:
    return False


def room_enabled() -> bool:
    return False


class RoomSessionBindings:
    def is_bound(self, key: str) -> bool:
        return False

    def bind(self, key: str) -> None:
        return None

    def unbind(self, key: str) -> None:
        return None


def bindings() -> RoomSessionBindings:
    return RoomSessionBindings()
