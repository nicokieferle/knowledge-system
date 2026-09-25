"""Explicit synthetic local browser smoke; never production config or knowledge paths.

Run with TEST_V32_DATABASE_URL pointing at the isolated test instance. Uses a new
fixture-owned schema and temporary source directory; cleanup is fixture-scoped.
"""

import secrets
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from knowledge_system.conversation_models import MessageRole, ProposalDraft, ProposalTriggerType
from knowledge_system.conversation_store import PostgresConversationStore
from knowledge_system.proposal_review import ProposalStatus
from knowledge_system.proposal_store import PostgresProposalStore, ProposalCreate
from knowledge_system.review_web import create_app
from tests.integration.test_proposal_review_postgres import database, initialize, seed
from tests.test_review_web import web_settings


def main():
    with TemporaryDirectory(prefix="knowledge-v321-browser-") as folder:
        fixture = database.__wrapped__(Path(folder))
        settings = next(fixture)
        try:
            initialize(settings)
            service, who, initial = seed(settings)
            convs = PostgresConversationStore(settings)
            origin = convs.add_message(
                initial.conversation_id,
                MessageRole.USER,
                "Synthetische Aussage: Wissen braucht nachvollziehbare Quellen. 🧪",
            )
            for name, content, state in (
                ("Kleiner Vorschlag", "# Notiz\nNeue Erkenntnis.\n", "pending"),
                (
                    "Großer Vorschlag",
                    "# Vollständiger Text\n" + "Kontext 🧪 und Herkunft.\n" * 2000,
                    "pending",
                ),
                ("Veraltete Basis", "neuer Kandidat\n", "stale"),
                ("Abgeschlossen akzeptiert", "akzeptierter Kandidat\n", "accepted"),
                ("Abgeschlossen abgelehnt", "abgelehnter Kandidat\n", "rejected"),
                ("Sehr langer Zielpfad", "noch zu prüfen\n", "deferred"),
            ):
                trigger = convs.add_message(
                    initial.conversation_id, MessageRole.USER, "synthetic control"
                )
                path = secrets.token_hex(4) + ".md"
                if name == "Sehr langer Zielpfad":
                    path = "long/" * 30 + "notes.md"
                p = PostgresProposalStore(settings).create_proposal(
                    ProposalCreate(
                        initial.conversation_id,
                        "test",
                        ProposalTriggerType.COMMAND,
                        trigger.id,
                        (origin.id,),
                        ProposalDraft(
                            name,
                            "Nur lokale Testdaten",
                            content,
                            title=name,
                            target_source_id="knowledge-git",
                            target_source_path=path,
                        ),
                    )
                )
                r = service.prepare(who, p.id).revision
                if state in ("accepted", "rejected", "deferred"):
                    service.decide(who, p.id, r.id, ProposalStatus(state))
                if state == "stale":
                    (Path(folder) / path).write_text("Basis geändert", encoding="utf-8")
            web = replace(
                web_settings(),
                owner=who,
                allowed_hosts=("127.0.0.1:8080", "localhost:8080"),
                session_seconds=60,
            )
            uvicorn.run(
                create_app(web, service),
                host="127.0.0.1",
                port=8080,
                access_log=False,
                proxy_headers=False,
            )
        finally:
            try:
                next(fixture)
            except StopIteration:
                pass


if __name__ == "__main__":
    main()
