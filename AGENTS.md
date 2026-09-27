# Arbeitsregeln für Knowledge-System

Basis: Repository Standard **1.5** · Übernommen: **27. September 2026**.
Referenz: [Hengsto/repository-template, verbindliche Revision](https://github.com/Hengsto/repository-template/blob/09bcc83d2ecc132823e9484e5c075b9e0db970f2/REPOSITORY_STANDARD.md).
Die folgenden Projektregeln gelten eigenständig; spätere Standardversionen werden
bewusst übernommen.

## Maßgebliche Dokumente und Arbeitsbeginn

- [README.md](README.md): Einstieg und belegter Funktionsumfang.
  [REQUIREMENTS.md](REQUIREMENTS.md): dauerhafte Ziele und Akzeptanzkriterien.
  [ROADMAP.md](ROADMAP.md): Phasen und Abhängigkeiten.
- Ausschließlich [GitHub Issues](https://github.com/Hengsto/Knowledge-System/issues)
  führen Aufgabenstatus, Prioritäten, Blocker, Restarbeiten und Verbesserungsvorschläge.
  PRs und Prüfläufe belegen Änderungen; keine zweite Statusliste in Markdown führen.
- Bei jeder neuen Aufgabe/Sitzung geltende `AGENTS.md` lesen und Branch, `origin/main`,
  Arbeitsbaum, Index, untracked Dateien sowie relevante offene/kürzlich gemergte PRs
  prüfen. Fremde Arbeit erhalten; bei Überschneidungen einen eigenen Worktree nutzen.
- Issues und Dokumente nach Bereich, Pfaden, Begriffen und Schnittstellen durchsuchen.
  Bereiche: Retrieval/Quellen, Gespräch/Clients, Proposal/Review/Apply, Daten/Betrieb,
  CI und Dokumentation. Vorhandene Labels oder `Scope:` ergänzen die Suche, ersetzen
  sie nicht. Relevante Einträge vollständig lesen; Abhängigkeiten berücksichtigen.

## Befehle und Lesewege

| Aufgabe | Zuerst lesen |
| --- | --- |
| Setup, lokaler Start, Retrieval-Evaluation, MCP | [Lokale Entwicklung](docs/local-development.md) |
| Prüfungen, CI und Merge-Nachweise | [Prüfauswahl](docs/CI.md#prüfauswahl-und-nachweise) und [lokale CI-Reproduktion](docs/CI.md#lokale-reproduktion) |
| Quellen, Gespräch, Routing, Produktgrenzen | [Architektur](docs/conversation-architecture.md) und betroffene Requirements |
| Review, Authentifizierung, Apply | [Browser-Review](docs/v321-browser-review.md), [V3.3-Design](docs/v33-design.md), jeweilige Tests und Validierungsberichte |
| Konfiguration, Daten, Migration, Betrieb | [Betriebsdokumentation](docs/DEPLOYMENT_DEBIAN.md); historische Verfahren vor Nutzung gegen den aktuellen Stand prüfen |

## Wissen, Daten und Sicherheit

- Markdown/Git ist die kanonische Wissensquelle; vorhandenes `knowledge/` bleibt bis
  zur gesonderten Migration maßgeblich. Vor neuen Thesen nach verwandtem Wissen suchen.
- Bestehende Thesen nie stillschweigend löschen oder semantisch verändern. Provenienz,
  relevante Kommentare und Revisionshistorie erhalten; materielle Änderungen mit
  vorheriger Aussage, Änderung und Begründung nachvollziehbar machen.
- Fakten, Interpretationen, Hypothesen, Annahmen, Prognosen, Entscheidungen, Vorschläge
  und offene Fragen unterscheiden. Gesprächs-Memory ist kein kanonisches Wissen.
- Vektorindex, Embeddings, Suchtabellen und Cache sind reproduzierbare Ableitungen,
  niemals alleinige Wissensquelle. Dauerhafte Gesprächs-, Proposal-, Review- und
  Apply-Daten in PostgreSQL sind dagegen kein löschbarer Suchindex.
- Automatische Wissensänderungen bleiben bis zum Review Proposals. Clients dürfen
  weder Review noch authentifiziertes, revisionsgebundenes Apply umgehen. MCP bleibt
  lesend; Telegram sammelt Proposals; Browser-Review entscheidet und stößt Apply an.
- Keine Secrets oder privaten Wissens-/Gesprächsinhalte in Logs, Commits, PRs oder
  Ausgaben. Tests nutzen synthetische Daten und isolierte Ressourcen. Code,
  Konfiguration, Secrets, persistente Daten und abgeleitete Daten getrennt halten.

## Änderungen, Prüfungen und Übergabe

- Kleine, fachlich zusammengehörige Änderungen über eigenen Branch und PR umsetzen;
  nicht automatisch auf `main` schreiben. Bestehende lokale/fremde Änderungen weder
  überschreiben noch ungefragt bereinigen.
- Vor neuen oder wesentlich geänderten Funktionen Requirements und überprüfbare
  Abnahmekriterien zuordnen. Komplexe oder riskante Änderungen technisch planen;
  passende vorhandene Pläne und bestätigtes Fehlerwissen verwenden.
- Klare Module und explizite Datenflüsse erhalten. Nützliche, möglichst schaltbare
  Diagnostik ohne sensible Inhalte vorsehen. Neue Retrieval-Funktionen benötigen Tests;
  Indexformatänderungen müssen aus kanonischen Quellen neu aufbaubar bleiben.
- Funktionale Änderungen benötigen Kern- und erforderliche Bereichsprüfungen gemäß
  [CI-Dokumentation](docs/CI.md#prüfauswahl-und-nachweise). Reine Markdown-Änderungen
  benötigen Link-, Konsistenz- und Diff-Prüfung, keinen pauschalen Volltest.
- Integrations-/E2E-Nachweise nennen reale Komponenten, Testersatz und ungeprüfte Grenzen.
  Bei Artefaktänderungen Build und risikogerechte Prüfung des erzeugten Images gemäß
  [Test- und Artefaktgrenzen](docs/CI.md#test--und-artefaktgrenzen) nachweisen;
  Build-Erfolg allein belegt keine Nutzbarkeit oder vollständige Live-Funktion.
- Für jede relevante Prüfung Befehl, Umgebung, Commit samt etwaigen uncommitteten
  Änderungen, Ergebnis und Beleg im PR festhalten. Lokaler Erfolg ersetzt keine CI;
  erforderliche Checks müssen zum maßgeblichen aktuellen PR-Stand gehören.
- Betroffene Dokumentation im selben PR pflegen. Bei Ziel-/Planänderungen Requirements,
  Roadmap und Aufgabenbezüge abgleichen. Gesamten Diff auf unbeabsichtigte Änderungen,
  Daten, Secrets und gelöschte Inhalte prüfen.
- Unabhängige Verbesserungen nur im autorisierten Rahmen als Issues erfassen.
  Vorschläge nur mit Nachweis und Begründung schließen. Fehlen Zugriff oder Befugnis,
  im PR/Abschluss als „noch nicht übertragen“ mit Zielort, Zuständigkeit und nächstem
  Abgleich übergeben. Abnahmerelevante Lücken verhindern Merge-Bereitschaft; unabhängige
  Ideen bleiben nachvollziehbar offen.
- Bei Unterbrechung technischen Stand, Prüfungen, Branch/Commit und nächsten Schritt
  im vorhandenen Plan oder PR festhalten. Dokumentation und Agentenregeln deutsch;
  Dateinamen und technische Identifikatoren englisch.

## Produkt- und Betriebsgrenzen

- Knowledge-System verantwortet Wissensaufnahme, Speicherung, Retrieval, Review/Apply,
  Indexierung und Auditierbarkeit. Haley verantwortet langfristig allgemeine Assistenz,
  Orchestrierung, Dialog und Oberflächen einschließlich Windows-Client. Journaling und
  weitere Clients bleiben außerhalb des Kerns; Provider-/Clientadapter getrennt halten.
- Entwurf, Implementierung, Test, Merge und Deployment getrennt belegen. Ein Merge
  bestätigt kein Deployment. PR-Erstellung erlaubt weder Auto-Merge noch Deployment;
  konkrete bestehende Befugnisse und Einschränkungen des Auftrags beachten.
- `.auto-coding.toml` beschreibt die externe Automatisierung; daraus keine zusätzliche
  Befugnis für eigene Git-/Betriebsaktionen ableiten. Nicht beiläufig CI, Merge-Policy,
  Branch Protection, produktive Konfiguration oder Infrastruktur ändern.
- Produktives Deployment, Migration, Backup/Restore und Serverzugriff benötigen einen
  entsprechenden Auftrag. Das [Zielprofil](docs/DEPLOYMENT_DEBIAN.md#zielkonvention-und-bestehende-konfiguration)
  ist kein Nachweis seiner Einrichtung und kein Auftrag, Daten zu verschieben.
- Persönliche Nutzung bleibt der aktuelle Rahmen. Kommerzielle Nutzbarkeit durch
  austauschbare Integrationen, konfigurierbare Werte, Datenschutz und nachvollziehbare
  Lizenzlage offenhalten; keine kommerziellen Funktionen ohne Produktentscheidung ergänzen.
