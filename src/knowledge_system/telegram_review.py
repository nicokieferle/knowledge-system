"""Retired Telegram review UI: informational only, with no domain capability."""

from .review_auth import browser_base

NOTICE = "Reviews und Entscheidungen sind nur noch in der Weboberfläche verfügbar."


class TelegramReview:
    def __init__(self, transport, base_url=""):
        self.transport = transport
        self.base_url = browser_base(base_url)

    def command(self, identity, command, argument):
        text = NOTICE
        if self.base_url:
            text += "\n" + self.base_url + "/reviews"
        self.transport.send_message(identity.external_chat_id, text)

    def callback(self, identity, callback_id, data):
        # Old rv:/pv: messages remain clickable indefinitely. Never resolve their
        # IDs, prepare revisions, read private proposal data, or dispatch decisions.
        self.transport.answer_callback(callback_id, NOTICE)
