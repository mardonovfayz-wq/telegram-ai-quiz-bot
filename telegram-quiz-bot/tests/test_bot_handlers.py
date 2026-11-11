"""Phase 0 tests: the bot only reacts to private administrator messages.

These need python-telegram-bot (installed in GitHub Actions). They build
Telegram update objects locally; nothing is sent to Telegram or OpenAI.
"""

import asyncio
import importlib.util
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

TELEGRAM_AVAILABLE = (
    importlib.util.find_spec("telegram") is not None
    and importlib.util.find_spec("openai") is not None
)

ADMIN = 424242


@unittest.skipUnless(TELEGRAM_AVAILABLE, "python-telegram-bot and openai are required")
class PrivateChatOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Placeholder values only; nothing connects to Telegram or OpenAI.
        cls._saved_environment = {
            key: os.environ.get(key)
            for key in ("TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "ADMIN_ID", "CHANNEL_ID")
        }
        os.environ["TELEGRAM_BOT_TOKEN"] = "123456:offline-test-token"
        os.environ["OPENAI_API_KEY"] = "offline-test-key"
        os.environ["ADMIN_ID"] = str(ADMIN)
        os.environ["CHANNEL_ID"] = "@offline_test_channel"
        sys.modules.pop("bot", None)
        import bot  # noqa: E402
        from telegram import Chat, Document, Message, MessageEntity, Update, User
        from telegram.constants import ChatType
        from telegram.ext import Application

        cls.bot = bot
        cls.Chat, cls.Document, cls.Message = Chat, Document, Message
        cls.MessageEntity, cls.Update, cls.User = MessageEntity, Update, User
        cls.ChatType = ChatType
        application = Application.builder().token("123456:offline-test-token").build()
        bot.register_handlers(application)
        cls.handlers = [handler for group in application.handlers.values() for handler in group]

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._saved_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.modules.pop("bot", None)

    def _message(self, chat_type, chat_id, text=None, document=False, user_id=ADMIN):
        kwargs = {}
        if text and text.startswith("/"):
            command_length = len(text.split()[0])
            kwargs["entities"] = [
                self.MessageEntity(type="bot_command", offset=0, length=command_length)
            ]
        if document:
            kwargs["document"] = self.Document(
                file_id="file", file_unique_id="unique", file_name="law.txt"
            )
        return self.Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=self.Chat(id=chat_id, type=chat_type),
            from_user=self.User(id=user_id, first_name="Admin", is_bot=False)
            if chat_type != self.ChatType.CHANNEL
            else None,
            text=text,
            **kwargs,
        )

    def _matching(self, update):
        return [handler for handler in self.handlers if handler.check_update(update)]

    def test_channel_posts_reach_no_handler(self):
        for text in ("Kanaldagi oddiy post", "/start", "/generate 10"):
            with self.subTest(text=text):
                update = self.Update(
                    update_id=1,
                    channel_post=self._message(self.ChatType.CHANNEL, -1001, text=text),
                )
                self.assertEqual(self._matching(update), [])
        document_post = self.Update(
            update_id=2,
            channel_post=self._message(self.ChatType.CHANNEL, -1001, document=True),
        )
        self.assertEqual(self._matching(document_post), [])

    def test_group_messages_and_edits_reach_no_handler(self):
        group = self.Update(
            update_id=3,
            message=self._message(self.ChatType.SUPERGROUP, -1002, text="Salom"),
        )
        self.assertEqual(self._matching(group), [])
        edited = self.Update(
            update_id=4,
            edited_message=self._message(self.ChatType.PRIVATE, ADMIN, text="Tahrir"),
        )
        self.assertEqual(self._matching(edited), [])

    def test_private_admin_messages_are_handled(self):
        text = self.Update(
            update_id=5, message=self._message(self.ChatType.PRIVATE, ADMIN, text="Matn")
        )
        self.assertEqual(len(self._matching(text)), 1)
        command = self.Update(
            update_id=6, message=self._message(self.ChatType.PRIVATE, ADMIN, text="/help")
        )
        self.assertEqual(len(self._matching(command)), 1)
        document = self.Update(
            update_id=7, message=self._message(self.ChatType.PRIVATE, ADMIN, document=True)
        )
        self.assertEqual(len(self._matching(document)), 1)

    def test_admin_check_stays_silent_outside_private_chats(self):
        # The message has no bot attached, so any reply attempt would raise.
        for chat_type, chat_id in (
            (self.ChatType.CHANNEL, -1001),
            (self.ChatType.GROUP, -1003),
        ):
            with self.subTest(chat_type=chat_type):
                message = self._message(chat_type, chat_id, text="hello")
                field = "channel_post" if chat_type == self.ChatType.CHANNEL else "message"
                update = self.Update(update_id=8, **{field: message})
                self.assertFalse(asyncio.run(self.bot.is_authorized_admin(update, None)))

    def test_bot_does_not_request_channel_posts_or_edits(self):
        self.assertIn(self.Update.MESSAGE, self.bot.ALLOWED_UPDATES)
        self.assertIn(self.Update.CALLBACK_QUERY, self.bot.ALLOWED_UPDATES)
        self.assertNotIn(self.Update.CHANNEL_POST, self.bot.ALLOWED_UPDATES)
        self.assertNotIn(self.Update.EDITED_MESSAGE, self.bot.ALLOWED_UPDATES)

    def test_identifiers_come_from_the_environment(self):
        self.assertEqual(self.bot.ADMIN_ID, ADMIN)
        self.assertEqual(self.bot.CHANNEL_ID, "@offline_test_channel")


if __name__ == "__main__":
    unittest.main()
