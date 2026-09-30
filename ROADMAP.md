# Roadmap

Planungsgrundlage: [REQUIREMENTS.md](REQUIREMENTS.md).
**Aktuelle Aufgaben, Prioritäten, Blocker und konkrete Restarbeiten stehen ausschließlich
in [GitHub Issues](https://github.com/Hengsto/Knowledge-System/issues).** Diese Roadmap
ordnet Ergebnisse, Reihenfolge und Abhängigkeiten ein. Sie ist kein Issue-Tracker.
Änderungen und Prüfnachweise gehören zu [PRs](https://github.com/Hengsto/Knowledge-System/pulls?q=is%3Apr),
Commits und den jeweils passenden CI-Läufen. Es sind keine Termine zugesagt.

## Historische Meilensteine V0 bis V3.3

Abgleich am 27. September 2026 gegen `origin/main` bei
`ebded84edb3a8a17ab2ec26ca9aa7f1035215b9e`, Implementierung, vorhandene Tests, PRs,
Issues und Workflow. Die implementierten Etappen sind kompakt zusammengefasst;
frühere unerledigte Ideen stehen bei den späteren Entwicklungsrichtungen.
„Gemergt“ und „getestet“ bedeuten hier nicht „produktiv ausgerollt“.

| Etappe | Übernommenes Ergebnis und Implementierungsbeleg | Prüf- und Betriebsnachweis |
| --- | --- | --- |
| V0 – Retrieval-Grundlage | Markdown/Git, PostgreSQL/pgvector, Überschriften-Chunking, lokale mehrsprachige Embeddings, Inhaltshashes, inkrementelles Re-Embedding und exakte Cosinus-Suche. [Indexer](src/knowledge_system/indexer.py), [Chunking](src/knowledge_system/chunking.py), [Suche](src/knowledge_system/search.py); Historie ab Commit `234db64`. | Vorhandene [Chunking-](tests/test_chunking.py), [Index-](tests/test_indexer.py) und [Suchtests](tests/test_search.py); kein gesonderter aktueller Produktionsnachweis. |
| V0.1 – Retrieval-Qualität | Erweiterbares JSONL-Eval, 35 Fragen im übernommenen Korpus, Hit@1/3/5 und MRR, deutsche Volltextsuche, experimentelles RRF und mehrsprachiges Cross-Encoder-Reranking. [Evaluation](src/knowledge_system/evaluation.py), [Befehle und Grenzen](docs/local-development.md#retrieval-evaluation). | [Evaluationstests](tests/test_evaluation.py); das kleine thematische Korpus ist kein allgemeiner Benchmark. Modell-/Chunkingänderungen weiterhin gegen die Baseline messen. |
| V1 – Servicegrenze | `KnowledgeService`, `SourceAdapter` und strukturierte Suche/Originalabruf; [Service](src/knowledge_system/service.py), [Quellen](src/knowledge_system/sources.py), Commit `8243b5d`. | [Servicetests](tests/test_service.py), [Quelltests](tests/test_sources.py). Der damalige allgemeine FastAPI-Service ist nicht mit der späteren Browser-App gleichzusetzen. |
| V2 – MCP | Genau zwei lesende Tools `search_knowledge` und `get_document`, stdio als Standard; [MCP-Server](src/knowledge_system/mcp_server.py), Commit `d6ec982`. | [MCP-Tests](tests/test_mcp_server.py). ChatGPT-Clientanbindung ist nicht durch die bloße Protokollimplementierung belegt. |
| V2.1 – Streamable HTTP | Gleiche lesende Tools über lokalen HTTP-Transport, Loopback-Defaults und Host-/Origin-Allowlist, Commit `44e80b7`. | MCP-Tests enthalten einen echten lokalen Protokollclient. Die frühere Roadmap markierte Debian-Deployment als erledigt; ein aktueller Zielhost-/Clientnachweis liegt diesem Refactor nicht vor. |
| V2.2 – Debian/Docker | Non-root-CPU-Image, separates Server-Compose, lokale MCP-Portfreigabe, persistente Daten/Modellcache und explizite Initialisierungs-/Updateverfahren; Commits `553b7de`, `75c5e1c`. Damals war `knowledge/` aus dem Produktcheckout nur lesend eingebunden; V3.3 ersetzt diesen vorgesehenen Mount. | [Deployment-Tests](tests/test_deployment.py), [Betriebsanleitung](docs/DEPLOYMENT_DEBIAN.md). Die frühere Roadmap markierte den Zielhost-Lebenszyklus als geprüft; das bleibt historische Angabe, keine neue Betriebsbestätigung. |
| V3.0 – Gespräch und Memory | Dauerhafte Nachrichten, Zusammenfassungen mit Snapshotgrenzen, providerneutrale Ports, direkter/semantischer Proposal-Intent und atomare, idempotente Vorschlagsbestätigung ohne Wissensschreibzugriff; [PR #1](https://github.com/Hengsto/Knowledge-System/pull/1). | [Historische isolierte Zielhost-Recovery vom 31. August 2026](docs/DEPLOYMENT_DEBIAN.md#verified-target-host-recovery-rehearsal), [PostgreSQL-Test](tests/integration/test_conversation_postgres.py). Der isolierte Versuch ist kein Nachweis eines V3-Rollouts. |
| V3.1, V3.1.1, V3.1.2 – Clients und Routing | OpenAI-kompatibler LLM-Provider, dünner Telegram-Adapter, dauerhafte Client-/Themenzuordnung, `/new`, `/topics`, `/switch`, getrennte Memory-Kontexte, Retrieval und Vorschlagsbestätigung; [PR #3](https://github.com/Hengsto/Knowledge-System/pull/3). Retry-/Diagnose-Härtung in [PR #4](https://github.com/Hengsto/Knowledge-System/pull/4), Routing-Normalisierung in [PR #5](https://github.com/Hengsto/Knowledge-System/pull/5). | [V3.1.1-Prüfgeschichte](docs/v311-final-local-review.md), [Retry-Grenzen](docs/v311-telegram-hotfix.md), [V3.1.2-Regression](docs/v312-routing-hotfix.md). Die alte Roadmap bezeichnete V3.1.2 als „production-tested“; PR #5 belegt den Produktionsfehler der Vorgängerversion und lokale Tests, aber keinen eigenen Rollout. Aktueller Betrieb bleibt unbestätigt. |
| V3.2 – Revisionsgebundener Review | `pending/accepted/rejected/deferred`, unveränderliche Revisionen, vollständiger Diff, geprüfte Hashbasis, Stale-Erkennung, Ownership und atomare/idempotente Entscheidungen; [PR #6](https://github.com/Hengsto/Knowledge-System/pull/6). Backup-/Restore-Härtung in [PR #7](https://github.com/Hengsto/Knowledge-System/pull/7). | [Review-Validierung](docs/v32-validation.md), [Restore-Erkenntnisse](docs/v32-backup-restore-hardening.md). Das damalige Accept speicherte nur Zustimmung; damalige Telegram-Review-Oberfläche durch V3.2.1 abgelöst. |
| V3.2.1 – Browser-Review | Private Browserkonsole mit Single-Admin-Auth, sitzungsgebundenem CSRF, eigener Queue, expliziter Review-Vorbereitung und revisionsgebundenen Entscheidungen; [PR #8](https://github.com/Hengsto/Knowledge-System/pull/8). Telegram sammelt nur noch Proposals. | [Browser-Validierung](docs/v321-validation.md), [Design](docs/v321-browser-review.md). Die Test-/Merge-Nachweise enthalten keinen produktiven Rollout. |
| V3.3 – Kontrolliertes Apply | Separat konfigurierbarer persistenter Wissensroot, geschütztes Apply freigegebener Bytes, dauerhaftes Journal, sichere Wiederholungen, dokumentbezogener Indexersatz und Konflikt-Nachfolgeproposal; [PR #11](https://github.com/Hengsto/Knowledge-System/pull/11). | [Design](docs/v33-design.md), [Validierung](docs/v33-validation.md), [Apply-Integrationstests](tests/integration/test_proposal_apply_postgres.py). Implementiert, geprüft und gemergt; die produktive Storage-Migration ist dadurch nicht ausgeführt. |

Der Workflow aus [PR #9](https://github.com/Hengsto/Knowledge-System/pull/9) und die
spätere Auto-Coding-Anbindung aus [PR #13](https://github.com/Hengsto/Knowledge-System/pull/13)
sind Entwicklungsinfrastruktur. Die gemergten Smoke-Dokumente aus
[PR #18](https://github.com/Hengsto/Knowledge-System/pull/18) und
[PR #26](https://github.com/Hengsto/Knowledge-System/pull/26) bleiben erhalten;
sie sind keine Knowledge-System-Produktversionen oder Deploymentnachweise.
Für den oben genannten Ausgangscommit bestanden beide Checks im
[CI-Lauf 36186972997](https://github.com/Hengsto/Knowledge-System/actions/runs/36186972997).

## Weitere Phasen und Abhängigkeiten

Die Reihenfolge trennt sichere Betriebsübernahme, Integrationsfähigkeit und optionale
Erweiterungen. Eine Phase ist kein Umsetzungsauftrag; konkrete Arbeit wird als Issue
beauftragt. Beim Dokumentationsabgleich vom 27. September 2026 wurden für die folgenden
Produktphasen keine zugeordneten Produkt-Issues gefunden; notwendige Übertragungen
werden im Refactor-PR und Abschluss an den Maintainer übergeben, ohne hier einen
zweiten Aufgabenstatus anzulegen.

| Phase | Angestrebtes Ergebnis | Anforderungen | Voraussetzung und Detailquelle |
| --- | --- | --- | --- |
| Betriebsübernahme V3.2/V3.3 | Kontrollierte Einführung von Review/Apply mit geprüftem Datenbestand und nachvollziehbarer Git-Versionierung außerhalb des Produktcheckouts. | [KS-DATA-001](REQUIREMENTS.md#ks-data-001), [KS-DATA-006](REQUIREMENTS.md#ks-data-006), [KS-OPS-001](REQUIREMENTS.md#ks-ops-001), [KS-OPS-002](REQUIREMENTS.md#ks-ops-002), [KS-OPS-003](REQUIREMENTS.md#ks-ops-003) | Separater Betriebsauftrag, bestätigter Istzustand und abgestimmte Migration; siehe unten und [Betriebsdokumentation](docs/DEPLOYMENT_DEBIAN.md). |
| Clientunabhängige Integration | Authentifizierte Verträge für Kernfunktionen, Quellenberechtigungen und sichere Einbindung externer Clients, einschließlich Haley. | [KS-SEC-001](REQUIREMENTS.md#ks-sec-001), [KS-CLIENT-001](REQUIREMENTS.md#ks-client-001), [KS-CLIENT-002](REQUIREMENTS.md#ks-client-002), [KS-CLIENT-003](REQUIREMENTS.md#ks-client-003) | Vor schreibender Clientfreigabe Review-/Apply- und Identitätsverträge klären. Ein allgemeiner FastAPI-Service und ein sicherer Tunnel/ChatGPT-Client waren frühere Optionen. Für fertige Entwürfe konkretisieren [Issue #35](https://github.com/Hengsto/Knowledge-System/issues/35) und der [v1-Vertrag](docs/proposal-ingress.md) den separaten authentifizierten Eingang; weitere Fähigkeiten und Remote-Transport bleiben gesonderte Entscheidungen. Kein Windows-Client im Knowledge-System-Kern. |
| Retrieval und Quellenqualität | Metadaten/Frontmatter bei Bedarf auswerten, quellengebundene Qualität messbar verbessern und Diagnose gezielt ausbauen. | [KS-CORE-002](REQUIREMENTS.md#ks-core-002), [KS-DATA-002](REQUIREMENTS.md#ks-data-002), [KS-DATA-003](REQUIREMENTS.md#ks-data-003), [KS-DATA-004](REQUIREMENTS.md#ks-data-004) | Baseline und echte Nutzungsfälle. `find_related`, erweiterte strukturierte Logs und HNSW bleiben mögliche spätere Entscheidungen, keine Zusage. Vorhandene Diagnostik bleibt erhalten; Frontmatter wird derzeit entfernt, nicht strukturiert erschlossen. Parallel zur Integrationsplanung möglich. |
| V4 – Weitere Quellen | Mögliche Adapter für Journaling/SQLCipher, PDFs/Dokumente und Projekt-Repositories. | [KS-CORE-001](REQUIREMENTS.md#ks-core-001), [KS-CORE-002](REQUIREMENTS.md#ks-core-002), [KS-DATA-002](REQUIREMENTS.md#ks-data-002), [KS-CLIENT-001](REQUIREMENTS.md#ks-client-001) | Quellrechte, Provenienz, Normalisierung und Datenschutz vor jedem Adapter festlegen. Journaling bleibt eigenständige Quelle/Client; vorhandene SourceAdapter-Grenze nutzen. |
| Commercial Readiness | Übertragbarkeit, Datenschutz, Datenexport/-löschung, Sicherheitsgrenzen sowie Rechte an Code, Abhängigkeiten, Modellen und Assets prüfen. | [KS-COM-001](REQUIREMENTS.md#ks-com-001), [KS-COM-002](REQUIREMENTS.md#ks-com-002), [KS-COM-003](REQUIREMENTS.md#ks-com-003), [KS-SEC-002](REQUIREMENTS.md#ks-sec-002) | Spätere gesonderte Bewertung auf Basis belastbarer Daten-/Integrationsverträge. Produktlizenz und Konflikt zwischen Löschung, Audit und Git-/Backup-Historie klären. Kein Beschluss für Billing, Registrierung, Multi-Tenancy, Abos, Support, SLA/HA oder Cloud-Plattform. |

## Produktionsmigration als eigene Betriebsphase

Die [V3.3-Validierung](docs/v33-validation.md#known-limits-and-production-prerequisites)
und die vorherige Roadmap halten Storage-Seed, Ownership, Backupprüfung, Migration
und Rollout ausdrücklich als noch nicht ausgeführt fest. Im Abgleich wurde kein
späterer Nachweis gefunden. Der aktuelle Serverzustand ist unbekannt; vor einem
Betriebsauftrag muss er neu festgestellt werden. Es erfolgte kein Serverzugriff.

Die spätere Betriebsphase muss Produktcheckout und Wissensdaten trennen, ein privates
Daten-Repository samt Name/Versionierungsverfahren bestimmen und PostgreSQL-Auditdaten
sowie Markdown/Git gemeinsam wiederherstellbar machen. Das geplante
[Betriebsprofil](docs/DEPLOYMENT_DEBIAN.md#zielkonvention-und-bestehende-konfiguration)
ist von den bestehenden Compose-Pfaden zu unterscheiden. Ein optionaler
Operator-Branch-/Commit-Workflow ersetzt keine dokumentierte Sicherungsentscheidung;
der Dienst erhält weiterhin keine Git-Credentials und führt keine Pushes aus.

## Herkunft und Pflege

Diese Datei löst die [bisherige Roadmap](https://github.com/Hengsto/Knowledge-System/blob/ebded84edb3a8a17ab2ec26ca9aa7f1035215b9e/docs/ROADMAP.md)
ab. Die doppelte V3.1-Struktur wurde zusammengeführt; sämtliche V0–V3.3-Meilensteine,
relevanten offenen Entwicklungsrichtungen und die gesonderte Produktionsmigration
sind übernommen. `docs/ROADMAP.md` bleibt allein als Weiterverweis für ältere Links.
Historische Designs und Prüfberichte bleiben mit ihrem damaligen Geltungsbereich
bestehen. Bei Ziel- oder Reihenfolgeänderungen Requirements, Roadmap und Issue-Bezüge
abgleichen; reine Aufgabenstatuswechsel ausschließlich in Issues pflegen.
