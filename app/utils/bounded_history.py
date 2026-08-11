"""Tiny deque-backed replacement for langchain.memory.ConversationBufferMemory.

We used langchain's ConversationBufferMemory in orchestrator_service and
opentarget_service to keep a rolling chat history per WebSocket. Two issues:

1. The underlying list never trimmed — every long-lived session leaked RAM
   linearly in the number of turns.
2. We only ever needed `.chat_memory.add_user_message`,
   `.chat_memory.add_ai_message`, and `.chat_memory.messages[i].content` —
   a microscopic subset of the langchain API. Pulling in the whole
   `langchain==0.1.20` dependency for this was overkill.

This module exposes a drop-in replacement that keeps the same surface and
caps the buffer at `maxlen` messages via collections.deque.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import List


@dataclass
class _Message:
    """Mirrors the .content attribute callers read off
    langchain's HumanMessage / AIMessage objects."""
    content: str


class _BoundedHistory:
    def __init__(self, maxlen: int) -> None:
        self._buf: "deque[_Message]" = deque(maxlen=maxlen)

    def add_user_message(self, content: str) -> None:
        self._buf.append(_Message(str(content)))

    def add_ai_message(self, content: str) -> None:
        self._buf.append(_Message(str(content)))

    @property
    def messages(self) -> List[_Message]:
        # Return a list snapshot so callers can index/slice without seeing
        # the deque mutate underneath them mid-iteration.
        return list(self._buf)


class BoundedConversationMemory:
    """Drop-in replacement for langchain.memory.ConversationBufferMemory
    that bounds the underlying message list. Default maxlen=10 keeps the
    last 5 Q/A pairs — matches the consumer logic in both services."""

    def __init__(self, maxlen: int = 10) -> None:
        if maxlen < 2:
            raise ValueError("maxlen must be >= 2 to hold at least one Q/A pair")
        self.chat_memory = _BoundedHistory(maxlen=maxlen)
