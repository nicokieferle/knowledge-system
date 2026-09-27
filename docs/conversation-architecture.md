# Architektur und Systemgrenzen

Diese Datei beschreibt den implementierten Aufbau bis V3.3. Dauerhafte Produktziele
stehen in [REQUIREMENTS.md](../REQUIREMENTS.md), Phasen und historische Nachweise in
[ROADMAP.md](../ROADMAP.md). Ein Implementierungsvertrag belegt kein Deployment.

## Verantwortlichkeiten und Datenfluss

| Bereich | Implementierung | Vertrag und Prüfung |
| --- | --- | --- |
| Suche und Originalquellen | [service.py](../src/knowledge_system/service.py), [sources.py](../src/knowledge_system/sources.py) | `KnowledgeService`, registrierte `SourceAdapter`, logische Quell-IDs/-Pfade; [Servicetests](../tests/test_service.py), [Quelltests](../tests/test_sources.py) |
| Index und Retrieval | [indexer.py](../src/knowledge_system/indexer.py), [search.py](../src/knowledge_system/search.py), [reranker.py](../src/knowledge_system/reranker.py) | Reproduzierbare Ableitungen; [lokale Evaluation](local-development.md#retrieval-evaluation) |
| Lesende Clients | [cli.py](../src/knowledge_system/cli.py), [mcp_server.py](../src/knowledge_system/mcp_server.py) | CLI ruft den Service direkt auf; MCP bietet nur `search_knowledge` und `get_document`; [MCP-Tests](../tests/test_mcp_server.py) |
| Gespräch und Vorschläge | [conversation_service.py](../src/knowledge_system/conversation_service.py), [conversation_memory.py](../src/knowledge_system/conversation_memory.py), [proposal_service.py](../src/knowledge_system/proposal_service.py) | Nachrichten, Zusammenfassungen, Intent und Proposals; [Gesprächstests](../tests/test_conversation_service.py), [Memory-Tests](../tests/test_conversation_memory.py) |
| Routing und Integrationen | [conversation_router.py](../src/knowledge_system/conversation_router.py), [telegram_adapter.py](../src/knowledge_system/telegram_adapter.py), [llm_provider.py](../src/knowledge_system/llm_provider.py) | Client-/Provideradapter an Domänengrenzen; [Routingtests](../tests/test_conversation_router.py), [Providertests](../tests/test_llm_provider.py) |
| Review und Identität | [proposal_review.py](../src/knowledge_system/proposal_review.py), [review_store.py](../src/knowledge_system/review_store.py), [review_auth.py](../src/knowledge_system/review_auth.py), [review_web.py](../src/knowledge_system/review_web.py) | Unveränderliche Revisionen, Auth, Ownership, CSRF; [Browser-Design](v321-browser-review.md), [Webtests](../tests/test_review_web.py) |
| Apply und Wiederholung | [proposal_apply.py](../src/knowledge_system/proposal_apply.py), [knowledge_apply.py](../src/knowledge_system/knowledge_apply.py), [index_coordination.py](../src/knowledge_system/index_coordination.py) | Revisionsgebundene Bytes und Journal; [V3.3-Vertrag](v33-design.md), [Integrationstests](../tests/integration/test_proposal_apply_postgres.py) |

```text
Telegram Long Polling -> TelegramAdapter -> ConversationRouter -> ConversationService
                                                              |-> ConversationMemory
                                                              |-> KnowledgeService
                                                              |-> LLM-Protokolle
                                                              `-> ProposalService
Browser -> ProposalReviewService -> ProposalApplyService
                                   |-> SecureKnowledgeWriter -> Markdown
                                   `-> index_document -> abgeleiteter Index
CLI / MCP -> KnowledgeService -> Retrieval / SourceAdapter -> Originalquellen
```

Der vorhandene Gesprächskern unterstützt Wissensaufnahme und Retrieval. Langfristig
liegen allgemeine Assistenz, Orchestrierung, Dialogführung und Oberflächen bei Haley,
einschließlich dessen Windows-Client. Eine Herauslösung vorhandener Module ist damit
nicht beschlossen oder implementiert. Journaling bleibt externe Quelle/Client;
Telegram und Browser sind vorhandene Adapter. Neue Clients benötigen künftig definierte,
authentifizierte Integrationsverträge; sie erhalten keinen direkten Datenbank- oder
Dateisystemweg um den Review-/Apply-Vertrag herum.

## Datenklassen und kanonische Quelle

1. **Kanonisches Wissen:** überprüftes Markdown mit Git-Historie. Im bisherigen
   Repository liegt es unter `knowledge/`; V3.3-Server-Compose verlangt einen separat
   konfigurierten persistenten Root. Geplant ist ein eigenes privates Daten-Repository.
   [Zielprofil und aktueller Konfigurationsstand](DEPLOYMENT_DEBIAN.md#zielkonvention-und-bestehende-konfiguration)
   unterscheiden diese Zustände; keine Datenmigration ist durch Dokumentation erfolgt.
2. **Dauerhafter Anwendungszustand:** vollständige rohe User-/Assistant-/Systemnachrichten,
   Zusammenfassungen, Vorschlags- und Reviewdaten, Entscheidungen und Apply-Journal.
   Gesprächshistorie ist Auditkontext, nicht automatisch kanonisches Wissen.
3. **Reproduzierbare Ableitungen:** `chunks`, Retrieval-Indizes und `index_metadata`,
   Embeddings und Modellcache. Sie dürfen aus Originalquellen neu aufgebaut werden.

Zusammenfassungen unterstützen den Modellkontext, löschen aber keine Originalnachrichten.
Auch wenn sie ableitbar sind, zählen sie im bestehenden Betrieb zum dauerhaft gesicherten
Gesprächszustand. PostgreSQL enthält beide Datenklassen; sein gesamtes Datenvolume ist
kein löschbarer Suchcache. Die elf dauerhaften Tabellen sind zentral in
[durable_tables.sh](../scripts/durable_tables.sh) aufgeführt: `conversations`, `messages`,
`conversation_summaries`, `proposal_suggestions`, `proposals`, `client_states`,
`client_conversations`, `client_message_bindings`, `proposal_reviews`,
`proposal_decisions` und `proposal_applies`.

`knowledge index` verändert keine dauerhaften Tabellen. `knowledge init-db` verwendet
additive Initialisierung und benannte Migrationen; es setzt bestehende Daten nicht zurück.
Backup/Restore muss zusätzlich zum Datenbankzustand den kanonischen Markdown-/Git-Bestand
berücksichtigen. Verfahren und historische Nachweise stehen in der
[Betriebsdokumentation](DEPLOYMENT_DEBIAN.md#durable-state-backup).

## Gesprächskontext und Modellgrenzen

`ConversationService` ruft `KnowledgeService` über die injizierte schmale Retrieval-
Schnittstelle direkt auf, nicht über MCP/HTTP. Quell-IDs bleiben für spätere Adapter
erhalten. `ChatModel`, `ConversationSummarizer`, `IntentClassifier`, `ProposalGenerator`
und `ConversationRoutingModel` sind Protokolle. Der Kern enthält kein Provider-SDK,
keinen Schlüssel und keinen eigenen Provider-Netzwerkaufruf; reale Aufrufe liegen im
OpenAI-kompatiblen Adapter. Tests setzen deterministische Fakes ein.

Die Memory-Policy behält standardmäßig zwölf aktuelle Nachrichten und beginnt nach
30 nicht zusammengefassten Nachrichten mit Kompaktierung. Ein unveränderlicher,
geordneter Nachrichten-Snapshot liefert die exakte letzte ID für Compare-and-set.
Während der Zusammenfassung eintreffende Nachrichten gelten dadurch nicht als bereits
zusammengefasst. Eine Versionsprüfung verhindert, dass ein konkurrierender Worker eine
neuere Zusammenfassung durch veraltete Ausgabe ersetzt.

`ConversationContext.recent_messages` schließt `current_user_message` aus; die aktuelle
Nachricht erscheint genau einmal im Modellkontext. Alle Nachrichten nach der
Zusammenfassungsgrenze bleiben erhalten. Liegt eine konkurrierende Zusammenfassung
bereits an oder hinter der Grenze eines älteren Requests, rekonstruiert dieser seinen
Kontext aus den ursprünglichen Nachrichten vor seiner eigenen Snapshotgrenze.

## Clientidentität und Themenrouting

Telegram übersetzt Updates, IDs, Kommandos und Antworten. Der Adapter besitzt weder
SQL-, Memory-, Retrieval-, Proposal-Generierungs- noch Promptlogik. Generische
`client_type`, externe Chat-/User-IDs und Nachrichten-IDs erhalten die Clientreferenz.
Ein anderer Adapter kann denselben serverseitigen Gesprächskontext verwenden, sofern
dessen autorisierte Zuordnung dieselbe Gesprächs-ID ergibt; daraus folgt kein
bereits vorhandener allgemeiner Netzwerkzugang für weitere Clients.

`client_states` speichert das aktive Thema pro vollständigem Identitätstupel.
`client_conversations` dokumentiert Ownership, `client_message_bindings` bindet
Wiederholungen an das ursprüngliche Thema. PostgreSQL-Advisory-Locks serialisieren
Aktivthemenwechsel. Der Router sieht höchstens 20 jüngste eigene Themen mit ID,
Titel, Zusammenfassungsfeld, Aktivität und Aktivmarkierung, nicht deren vollständige
Verläufe. Nur eigene, angebotene, nicht archivierte Themen sind Wechselziele.
Ungültige Ziel-IDs führen zu einem neuen isolierten Thema. `/new`, `/topics` und
`/switch` umgehen Modellrouting. Memory bleibt pro Gespräch getrennt.

Nach dauerhafter Bindung beziehungsweise gespeichertem Ergebnis spielen serielle
Retries dieses wieder ab. Vor Commit der Bindung bleiben zusätzliche ungenutzte
Themen möglich; Telegram-Zustellung bleibt at-least-once und permanente Fehler können
den einzelnen Poller blockieren. Keine globale Exactly-once-Zusage. Bestätigte Ursachen,
Regressionen und Grenzen: [V3.1.1](v311-telegram-hotfix.md),
[abschließende lokale Reviewgeschichte](v311-final-local-review.md) und
[V3.1.2](v312-routing-hotfix.md).

## Vom Vorschlag zum Review

`/remember` und `/propose` umgehen die semantische Intentklassifikation und erzeugen
Proposals aus relevantem vorherigem Kontext. `CREATE_PROPOSAL` führt zum gleichen
Ergebnis. `SUGGEST_PROPOSAL` erzeugt zunächst nur eine dauerhafte Nachfrage;
ausdrückliche Bestätigung erzeugt den Proposal, Ablehnung keinen.
Kontrollnachrichten bleiben für Audit erhalten; `trigger_message_id` ist von
`originating_message_ids` getrennt. Der Generator erhält nur die relevanten
Ursprungsnachrichten. Eine eindeutige `source_suggestion_id` sowie Zeilensperre und
gemeinsame Transaktion binden Bestätigung und Proposal atomar/idempotent.

Ein Proposal ist ein unvertrauenswürdiger Entwurf. Die Review-Revision bindet dagegen
Ziel, verifizierte Basis, alte/neue Bytes, Hashes und vollständigen Diff unveränderlich.
Vorbereitung verwendet ausschließlich den sicheren `GitMarkdownSource.snapshot`;
eine vom Generator gelieferte Basis ist nicht maßgeblich. Entscheidungen prüfen das
vollständige Ownership-Tupel unter Sperre. `pending`/`deferred` können entschieden
werden, `accepted`/`rejected` sind terminal. Wiederholungen derselben Aktion/Revision
sind idempotent; widersprüchliche oder überholte Entscheidungen scheitern.
Deferral öffnet keinen neuen `pending`-Zustand. DB-Trigger schützen Revisionen und Audit.

Seit V3.2.1 ist der Browser der einzige Review-/Entscheidungsclient. GET liest nur,
Vorbereitung und Mutationen erfolgen per authentifiziertem, CSRF-geschütztem POST.
Der serverseitig konfigurierte Principal entspricht dem gesamten Clienttupel;
Queue/Provenienz gelten für alle eigenen Gespräche, unabhängig vom 20-Themen-Routerlimit.
Ursprungsnachrichten werden nur innerhalb des eigenen Proposal-Gesprächs geladen.
Fehlende historische Provenienz wird angezeigt, nicht erfunden oder automatisch in
Markdown übernommen. Telegram konstruiert keinen Review-Service; alte `rv:`-/`pv:`-
Callbacks bleiben schon vor ID-Auswertung oder Domänenaufruf wirkungslos.

Die frühere Telegram-Diff-Zustellung mit 3000-UTF-16-Grenze und separater `.diff`-Datei
ist als abgelöstes Clientdesign in [V3.2](v32-review-design.md) erhalten. Dessen
Domänenentscheidungen bleiben nachvollziehbar; die maßgebliche Oberfläche steht im
[Browser-Design](v321-browser-review.md). Dies sind keine parallelen aktuellen Clients.

## Apply und Indexierung

Der einzige Schreibpfad ist `review-web -> ProposalApplyService -> SecureKnowledgeWriter`.
**Accept & Apply** bindet Entscheidung, akzeptierte unveränderliche Revision und
Apply-Intent in einer PostgreSQL-Transaktion. Erst anschließend erfolgen die getrennten
Datei- und Indexphasen. Ein früher akzeptierter Datensatz benötigt weiterhin die
explizite Aktion **Apply accepted revision**. Reject/Defer schreiben kein Wissen.

Das Journal bindet Proposal, Review, Ziel, alte/fehlende Basis, neuen Hash und Actor;
Versuche, sichere Fehlerklassen und Zeitpunkte bleiben erhalten. Es gibt keine globale
Transaktion über PostgreSQL, Dateisystem und Index. Vor Rename prüft ein Retry die
alte Basis; danach kann er die exakten erwarteten neuen Bytes erkennen und die
Dauerhaftigkeit verifizieren. Ein dritter Zustand bleibt Konflikt. Ein Indexfehler
lässt die erfolgreich geschriebene Datei bestehen und erhält einen eigenen Retry.

Dokumentindexierung ersetzt nur die Chunks von `(source_id, source_path)` in einer
Transaktion. Vollindex, Apply und Dokumentindex verwenden koordinierte globale,
dokumentbezogene und bei Dateischreibzugriffen zusätzliche Elternverzeichnis-Sperren.
Die exakten Linux-/Dateisystem-, Hash-, Lock- und Callback-Verträge sind im
[V3.3-Design](v33-design.md) maßgeblich beschrieben.

Ein akzeptierter Konflikt wird niemals umgebunden oder überschrieben. **Refresh review**
erzeugt atomar einen eigenen Nachfolgeproposal samt Kontroll-Auditnachricht und erhält
Ziel und Inhalt der freigegebenen Revision. Wiederholungen verwenden denselben
Nachfolger; die ursprüngliche Entscheidung bleibt unverändert. Telegram und MCP
können weder Apply noch Konflikt-Refresh oder Index-Retry auslösen.

Die Anwendung commitet oder pusht kein Git. Apply kann einen dedizierten Datencheckout
verändern; dessen Review/Versionierung bleibt Operator-Aufgabe. Die Migration auf das
getrennte private Daten-Repository und das Zielprofil ist eine gesonderte Betriebsphase.
