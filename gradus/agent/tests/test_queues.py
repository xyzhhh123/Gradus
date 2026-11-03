"""PendingMessageQueue 的离线测试。"""

from __future__ import annotations

import unittest

from gradus.agent.queues import PendingMessageQueue
from gradus.ai.types import UserMessage


class PendingMessageQueueTest(unittest.TestCase):
    def test_all_drains_everything(self):
        queue = PendingMessageQueue("all")
        queue.push(UserMessage("a"))
        queue.push(UserMessage("b"))

        self.assertEqual(len(queue), 2)
        self.assertEqual(
            [message.content for message in queue.drain()],
            ["a", "b"],
        )
        self.assertEqual(len(queue), 0)
        self.assertEqual(queue.drain(), [])

    def test_one_at_a_time_preserves_order(self):
        queue = PendingMessageQueue("one-at-a-time")
        queue.push(UserMessage("a"))
        queue.push(UserMessage("b"))

        self.assertEqual(
            [message.content for message in queue.drain()],
            ["a"],
        )
        self.assertEqual(
            [message.content for message in queue.drain()],
            ["b"],
        )
        self.assertEqual(queue.drain(), [])

    def test_clear_empties_queue(self):
        queue = PendingMessageQueue()
        queue.push(UserMessage("a"))

        queue.clear()

        self.assertEqual(len(queue), 0)
        self.assertEqual(queue.drain(), [])

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            PendingMessageQueue("invalid")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
