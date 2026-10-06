# Knowledge-System

Das Knowledge-System nimmt Wissen auf, speichert es nachvollziehbar und macht es
durch Suche und Retrieval nutzbar. Es unterstützt Vorschläge, menschliches Review
und kontrolliertes Apply. Das Produkt wird persönlich und selbst gehostet genutzt.
Menschenlesbares Markdown mit Git-Historie ist die kanonische Wissensquelle.

## Orientierung

| Information | Maßgeblicher Ort |
| --- | --- |
| Dauerhafte Produktziele, Grenzen und Akzeptanzkriterien | [REQUIREMENTS.md](REQUIREMENTS.md) |
| Phasen, Abhängigkeiten und historische Meilensteine | [ROADMAP.md](ROADMAP.md) |
| Aktuelle Aufgaben, Prioritäten, Blocker und Restarbeiten | Ausschließlich [GitHub Issues](https://github.com/Hengsto/Knowledge-System/issues) |
| Regeln und Lesewege für Coding Agents | [AGENTS.md](AGENTS.md) |
| Architektur, Datenklassen und Review-/Apply-Verträge | [Architektur](docs/conversation-architecture.md) und [V3.3-Design](docs/v33-design.md) |
| Änderungen und Prüfnachweise | Zugehörige [Pull Requests](https://github.com/Hengsto/Knowledge-System/pulls?q=is%3Apr) und [CI-Läufe](https://github.com/Hengsto/Knowledge-System/actions) |

## Belegter Funktionsumfang

Der geprüfte Dokumentationsausgangspunkt ist `ebded84edb3a8a17ab2ec26ca9aa7f1035215b9e`
auf `main` (27. September 2026). Darin sind implementiert und gemergt:

- Markdown-Quelladapter, abschnittsbezogenes Chunking, lokale Embeddings und
  inkrementeller PostgreSQL-/pgvector-Index. `fast` nutzt deutsche Volltextsuche;
  `quality` ergänzt einen lokalen Reranker. Separate Retrieval-Evaluationen sind vorhanden.
- Zwei lesende MCP-Tools, `search_knowledge` und `get_document`, über stdio und
  Streamable HTTP. Im historischen Ausgangsstand hatte der HTTP-Endpunkt selbst
  keine Authentifizierung oder TLS.
  Ein eingerichteter ChatGPT-Client oder ein aktuell geschützter Tunnel ist aus dem
  Repository nicht belegt; das Smoke-Skript unterstützt optionale Cloudflare-Access-Header.
  Der aktuelle Bearer-Vertrag steht unten.
- Gesprächsverlauf, Zusammenfassungen, Themenrouting und Proposals mit dauerhafter
  Speicherung; Telegram als Eingabe-/Chat-Client und ein OpenAI-kompatibler LLM-Adapter.
- Authentifizierter Browser-Review mit vollständigem Diff und revisionsgebundener
  Entscheidung; V3.3 ergänzt **Accept & Apply**, Konflikterkennung und getrennte
  Wiederholung von Datei-Apply und dokumentbezogener Indexierung.

Für diesen Ausgangscommit bestanden [CI / admission und CI / verify](https://github.com/Hengsto/Knowledge-System/actions/runs/36186972997).
Historische Bereichsprüfungen sind in der [Roadmap](ROADMAP.md#historische-meilensteine-v0-bis-v33)
verlinkt. Diese Nachweise belegen keinen aktuellen Produktionsstand. Die
Produktionsmigration für V3.2/V3.3 ist im letzten dokumentierten Stand offen;
ein späterer Rollout wurde hier nicht nachgewiesen.

Automatische Generierung erzeugt zunächst Proposals. Telegram erzeugt sie über
Befehle oder Intent-Verarbeitung, gegebenenfalls nach Vorschlagsbestätigung, trifft
aber keine Review-/Apply-Entscheidungen. Der Browser stößt nach
Review das kontrollierte Schreiben an. Die Anwendung führt keine automatischen
Git-Commits, Pushes oder Wissens-PRs aus; die Git-Versionierung des Datenbestands
bleibt ein ausdrücklicher Operator-Schritt. Produktänderungen nutzen bereits PRs.

## Architektur und Produktgrenzen

```text
CLI / lesende MCP-Clients -> KnowledgeService -> SourceAdapter / Retrieval
Telegram -> ConversationRouter -> ConversationService -> ProposalService
                                 |-> KnowledgeService
                                 |-> austauschbare LLM-Protokolle
Maschinen-Client -> ProposalIngressService -> pending-Proposal / Owner-Queue
Browser-Review -> ProposalReviewService -> ProposalApplyService
                                          |-> kanonisches Markdown
                                          |-> dokumentbezogener Suchindex
PostgreSQL: abgeleiteter Index UND dauerhafter Gesprächs-/Review-/Apply-Zustand
```

Der Index ist reproduzierbar. Gespräche, Entscheidungen und Apply-Journal sind
dauerhafte Anwendungsdaten und dürfen bei einem Index-Neuaufbau nicht verloren gehen.
Die [Architektur](docs/conversation-architecture.md) erläutert die Trennung.

Allgemeine Assistenz, Orchestrierung, Dialogführung und langfristige Oberflächen
einschließlich Windows-Desktop-Client gehören zu **Haley**. Journaling ist eine
mögliche Quelle beziehungsweise ein Client. Knowledge-System stellt dafür künftig
authentifizierte, clientunabhängige Integrationsschnittstellen bereit. Der vorhandene
Gesprächskern wird durch diese Produktabgrenzung nicht umgebaut.

Produktcode und Produktdokumentation bleiben in diesem Repository. Laufzeitwissen
soll unabhängig davon in einem separaten privaten Daten-Repository verwaltet und
gesichert werden können; Name, Migration und Backupbetrieb sind noch festzulegen.
Bis zu einer gesonderten Migration bleibt vorhandenes `knowledge/` kanonisch.

## Entwicklung und Betrieb

Der aktuelle MCP-HTTP-Zugang verlangt eine serverseitig geprüfte
Maschinenberechtigung und erlaubt zunächst nur kanonisches Markdown.
[Vertrag, Konfiguration und lokale Grenze](docs/mcp-http-access.md).

Der separate [Proposal-Eingang](docs/proposal-ingress.md) nimmt fertige Entwürfe
über authentifizierten `POST /v1/proposals` atomar und idempotent in die Owner-Queue
auf. `GET /v1/proposals/{proposal_id}/status` liefert mit demselben Token ausschließlich
minimale Zustände der eigenen Einreichung bei exakt gespeicherter Owner-Delegation;
Einreichung, Akzeptanz, Datei-Apply und Indexierung bleiben unterscheidbar.
Sein Token ist vom lesenden MCP und Browser-Login getrennt; Review und Apply
bleiben ausdrückliche Browseroperationen.

- [Lokale Einrichtung, Start, MCP und Retrieval-Evaluation](docs/local-development.md)
  mit Python ab 3.11, pip und PostgreSQL/pgvector.
- [Prüfauswahl, lokale Befehle und vorhandene CI](docs/CI.md#prüfauswahl-und-nachweise).
- [Debian-/Docker-Betrieb und Migrationsgrenzen](docs/DEPLOYMENT_DEBIAN.md), einschließlich
  des geplanten [Betriebsprofils](docs/DEPLOYMENT_DEBIAN.md#zielkonvention-und-bestehende-konfiguration).
- [Browser-Konfiguration und lokaler Start](docs/v321-browser-review.md#local-start-and-checks).

Eine spätere kommerzielle Nutzung bleibt eine Option. Die Leitplanken und die
Lizenzprüfung stehen in [REQUIREMENTS.md](REQUIREMENTS.md#kommerzielle-option);
kommerzielle Funktionen gehören derzeit nicht zum Produktumfang.
