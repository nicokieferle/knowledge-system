# Produktanforderungen

Diese Datei beschreibt das beschlossene Zielbild und seine überprüfbaren Kriterien.
Sie behauptet keine vollständige Implementierung oder produktive Einführung.
Phasen und Abhängigkeiten stehen in [ROADMAP.md](ROADMAP.md), aktuelle Aufgaben,
Prioritäten und Blocker ausschließlich in [GitHub Issues](https://github.com/Hengsto/Knowledge-System/issues).
Technische Verträge erläutern die [Architektur](docs/conversation-architecture.md)
und die dort verlinkten Designs. Die IDs bleiben bei redaktionellen Änderungen stabil.

## Zweck und Kern

### KS-CORE-001

**Wissensprodukt mit klarer Grenze.** Das persönlich und selbst gehostet genutzte
Knowledge-System verantwortet Aufnahme, Normalisierung, Speicherung, Suche/Retrieval,
Wissensvorschläge, Review/Apply, Indexierung, Nachvollziehbarkeit und Auditierbarkeit.
Es ist kein allgemeiner persönlicher Assistent.

**Akzeptanz:** Neue Funktionen lassen sich einer dieser Verantwortlichkeiten zuordnen.
Allgemeine Assistenz, Orchestrierung, Dialogführung und langfristige Oberflächen
einschließlich Windows-Desktop-Client sind Haley zugeordnet. Bestehende Gesprächs-
und Clientmodule begründen keinen erweiterten Produktauftrag und werden nicht allein
aufgrund dieser Abgrenzung entfernt.

### KS-CORE-002

**Quellengebundenes Retrieval.** Suche liefert verständliche Verweise auf kanonische
Originalquellen; Normalisierung und Indexierung erhalten deren Herkunft.

**Akzeptanz:** Treffer tragen mindestens logische Quell-ID und Quellpfad; ein
Originaldokument kann über den zugehörigen Adapter gelesen werden. Ungültige Pfade
und unbekannte Quellen werden abgewiesen. Retrieval-Änderungen werden anhand
reproduzierbarer Tests und geeigneter Qualitätsmessungen bewertet.

### KS-CORE-003

**Proposal, Review und Apply.** Automatisch erzeugtes Wissen beginnt als nicht
vertrauenswürdiger Vorschlag. Review prüft eine konkrete unveränderliche Revision;
Apply wendet ausschließlich deren freigegebenen Inhalt kontrolliert an.

**Akzeptanz:** Vorschlagsbestätigung ist keine Schreibfreigabe. Review zeigt Ziel,
Basis, vollständigen Inhalt/Diff und Provenienz. Entscheidung, Actor und Revision
sind nachvollziehbar gebunden. Unfreigegebene, überholte oder fremde Änderungen
werden nicht angewendet. Ältere akzeptierte Vorschläge werden nicht durch eine
Migration automatisch angewendet. Details: [Review-/Apply-Vertrag](docs/v33-design.md).

## Datenintegrität und Wissensquellen

### KS-DATA-001

**Markdown/Git als Source of Truth.** Menschenlesbares Markdown und nachvollziehbare
Git-Versionen bilden die kanonische Wissensquelle; Änderungen bleiben überprüfbar.

**Akzeptanz:** Kanonische Aussagen sind ohne Suchindex lesbar und exportierbar.
Wissensänderungen werden vor Übernahme geprüft und anschließend im zuständigen
Datenbestand versioniert. Der Betriebsablauf berücksichtigt, dass der bestehende
Apply-Service Dateien schreibt, aber selbst keine Git-Commits oder Pushes erstellt.

### KS-DATA-002

**Bedeutung, Provenienz und Historie erhalten.** Bestehende Thesen dürfen nicht
still gelöscht oder mit anderer Bedeutung überschrieben werden. Duplikate vermeiden.

**Akzeptanz:** Vor neuen Thesen wird verwandtes Wissen gesucht. Materielle Änderungen
lassen frühere Aussage, Herkunft und Änderungsgrund rekonstruieren. Relevante
Revisionskontexte bleiben in Dokument/Git und Review-/Apply-Historie erhalten.

### KS-DATA-003

**Aussagetypen unterscheiden.** Fakten, Interpretationen, Hypothesen, Annahmen,
Prognosen, Entscheidungen, Vorschläge und offene Fragen bleiben unterscheidbar.

**Akzeptanz:** Kuratierte Inhalte kennzeichnen den jeweiligen Aussagetyp und unsichere
Begründungen. Weder Modellantwort noch Gesprächszusammenfassung gelten allein als
bestätigter Fakt oder automatisch als kanonisches Wissen.

### KS-DATA-004

**Reproduzierbare Ableitungen.** Wissensabbilder in Datenbanken, Vektorindex,
Embeddings, Suchindizes und Cache sind abgeleitet und niemals alleinige Informationsquelle.

**Akzeptanz:** Ein Index lässt sich aus Originalquellen und dokumentierter
Konfiguration neu erzeugen. Modell-/Indexformatwechsel verändern keine kanonischen
Inhalte. Ein Neuaufbau verändert keine dauerhaften Gesprächs- oder Review-Daten.

### KS-DATA-005

**Dauerhaften Anwendungszustand schützen.** Originalnachrichten, Proposals,
Entscheidungen und Apply-Journal sind eigenständige, dauerhaft zu sichernde Daten.
Die gemeinsame PostgreSQL-Instanz macht sie nicht zu einem löschbaren Suchindex.

**Akzeptanz:** Speicherung, Export und Wiederherstellung unterscheiden diese Daten
von Retrieval-Tabellen. Zusammenfassungen ersetzen oder löschen keine Originalnachrichten.
Migrationen erhalten Herkunft, Beziehungen und Auditdaten; Index-Reset löscht sie nicht.

### KS-DATA-006

**Produkt und Laufzeitwissen trennen.** Anwendungscode und Produktdokumentation
bleiben in `Hengsto/Knowledge-System`. Für versioniertes Laufzeitwissen ist ein
separates privates Daten-Repository vorzusehen.

**Akzeptanz:** Wissen lässt sich unabhängig vom Produktcheckout verwalten, versionieren
und sichern; Codeupdates überschreiben es nicht. Der Dienst erhält keine Git-Credentials.
Repository-Name, Migration und Backupbetrieb werden vor einer Umsetzung entschieden.
Bis dahin bleibt vorhandenes `knowledge/` kanonisch; diese Anforderung verschiebt keine Daten.

## Sicherheit und Datenschutz

### KS-SEC-001

**Authentifizierte und autorisierte Grenzen.** Kernfunktionen sollen für Clients
über klar definierte authentifizierte Schnittstellen erreichbar sein; mutationsfähige
Schnittstellen erzwingen zusätzlich Ownership und konkrete Freigabe im Backend.

**Akzeptanz:** Fehlende Identität, fremde Ressourcen oder manipulierte Clientangaben
gewähren keinen Zugriff. Browser-Mutationen benötigen gültige Sitzung und CSRF-Schutz.
Der vorhandene unauthentifizierte MCP-HTTP-Endpunkt bleibt innerhalb seiner lokalen
Vertrauensgrenze; eine Host-/Origin-Allowlist ersetzt keine Authentifizierung.

### KS-SEC-002

**Private Inhalte und Secrets schützen.** Wissens-, Gesprächs- und Review-Inhalte
sowie Zugangsdaten werden nur innerhalb dokumentierter Vertrauensgrenzen verarbeitet.

**Akzeptanz:** Keine Secrets oder unnötigen privaten Nutzdaten in Repository, Logs,
CI-Artefakten oder PRs. Tests verwenden synthetische Daten. Externe Provider und Clients
sind als Datenempfänger dokumentiert; lokale Embeddings bedeuten nicht, dass LLM-Chat
lokal bleibt. Übertragene Kontexte und Providerkonfiguration werden vor Nutzung geprüft.

### KS-SEC-003

**Konfliktsicheres und nachvollziehbares Apply.** Apply prüft die freigegebene Basis
erneut, schützt Pfadgrenzen und überschreibt keinen abweichenden dritten Zustand.

**Akzeptanz:** Nur die exakten freigegebenen Bytes werden geschrieben. Konflikte
erhalten vorhandene Daten. Datei-Apply und Indexierung haben getrennte nachvollziehbare
Zustände und sichere Wiederholungen. Kein globales Transaktionsversprechen über
PostgreSQL, Dateisystem und Index. Plattform-/Dateisystemgrenzen sind dokumentiert.

## Clients und Provider

### KS-CLIENT-001

**Adapter statt Produktkopplung.** Telegram ist derzeit Eingabe-/Chat-Client,
Browser-Review ein kontrollierter Review-/Apply-Client. Journaling ist Quelle oder
Client. Allgemeine Oberflächen und der zukünftige Windows-Client gehören zu Haley.

**Akzeptanz:** Clientadapter greifen über Domänendienste zu und umgehen keinen
Proposal-/Review-/Apply-Schritt. Messenger-IDs, UI-Details oder Journaling-spezifische
Annahmen werden nicht zu allgemeinen Kernverträgen.

### KS-CLIENT-002

**Clientunabhängige Integration.** Der Kern soll unabhängig von Telegram oder einem
einzelnen Client nutzbar sein. Haley ist ein externes Integrationsziel.

**Akzeptanz:** Ein weiterer Client kann die benötigten Fähigkeiten über definierte,
authentifizierte Verträge nutzen, ohne kanonische Wissensdaten zu migrieren oder
direkt auf Datenbank/Dateisystem schreiben zu müssen. Der Windows-Client selbst ist
kein Knowledge-System-Feature. Die konkrete externe Integrations-API bleibt zu entwerfen.

### KS-CLIENT-003

**Modelle und Provider austauschbar halten.** Domänenlogik hängt von klaren
Protokollen ab, nicht dauerhaft von einem Anbieter oder Modell.

**Akzeptanz:** Modell-/Providerwechsel erfolgen über Konfiguration beziehungsweise
Adapter. Kanonische Wissensdaten benötigen dafür keine Migration; abgeleitete
Embeddings dürfen neu aufgebaut werden. Ein neuer Adapter lässt sich mit Fakes
prüfen. Ein OpenAI-kompatibler Adapter allein belegt keine ChatGPT-Clientintegration.

## Betrieb und Nachweise

### KS-OPS-001

**Konfiguration und Betriebsprofile trennen.** Hostwerte sind konfigurierbar;
Code, Konfiguration, Secrets, persistente Daten und Ableitungen haben klare Ablagen.

**Akzeptanz:** Das Linux-Zielprofil ordnet Code `/srv/projects/knowledge-system`,
persistente Daten `/data/knowledge-system/` und Hostkonfiguration
`/data/config/knowledge-system/.env` zu. Starter/Deployment übergeben Werte und Mounts;
die Anwendungslogik setzt keine Hostpfade voraus. Hostwerte dürfen abweichen.
Pflichtwerte, Defaults, Ladepriorität und tatsächliche Startvalidierung sind beschrieben;
fehlende technische Umsetzung wird als solche benannt.

### KS-OPS-002

**Unabhängige Wiederherstellbarkeit.** Kanonisches Wissen mit Git-Historie und
dauerhafter Anwendungszustand müssen unabhängig vom Produktcode sicherbar sein.

**Akzeptanz:** Das Betriebsverfahren unterscheidet Datenbankarchiv, Markdown/Git
und neu aufbaubaren Index. Eine isolierte Wiederherstellungsprüfung verifiziert
Inhalte/Beziehungen und Apply-Hashes, ohne Produktivdaten zu verändern. Namen,
Aufbewahrung, Sicherungsziel und Verantwortlichkeiten sind vor produktiver Migration
festgelegt. Eine Archivauflistung allein ist kein Wiederherstellungsnachweis.

### KS-OPS-003

**Zustände belegen.** Entwurf, Implementierung, Test, Merge und Deployment sind
unterschiedliche Zustände.

**Akzeptanz:** Prüfnachweise nennen Umgebung und geprüften Commit. Merge wird durch
PR/Git, Deployment durch einen gesonderten Betriebsnachweis belegt. Unbekannte
Zustände heißen unbekannt/offen. Aktuelle Aufgaben werden ausschließlich in Issues
geführt; ein grüner Build oder eine konfigurierte Compose-Datei belegt keinen Rollout.

## Kommerzielle Option

### KS-COM-001

**Übertragbarkeit und Datenkontrolle offenhalten.** Der Kern enthält keine
personenspezifischen Annahmen; Benutzer, Hosts und Pfade werden konfiguriert.
Benutzerdaten sollen exportierbar und kontrolliert löschbar sein.

**Akzeptanz:** Neue Kernfunktionen brauchen keine fest eingebauten persönlichen Werte.
Ein künftiges Export-/Löschverfahren erfasst kanonische Daten, Gesprächs-/Auditdaten,
Ableitungen und Sicherungskopien. Zielkonflikte mit unveränderlicher Historie und
Aufbewahrung werden vor Umsetzung entschieden; es wird keine bestehende vollständige
Löschfunktion behauptet.

### KS-COM-002

**Nachvollziehbare Nutzungsrechte.** Abhängigkeiten, Modelle und Assets müssen für
die beabsichtigte kommerzielle Nutzung geeignet sein; die Lizenzlage ist zu prüfen.

**Akzeptanz:** Vor kommerzieller Weitergabe liegen ein überprüftes Inventar,
Nutzungsbedingungen und eine Entscheidung zur Produktlizenz vor. Fehlende oder
unklare Rechte werden benannt und vor Freigabe geklärt. Der derzeitige private
Repository-Zugriff stellt keine Lizenzfreigabe dar.

### KS-COM-003

**Kommerzielle Vorbereitung ohne Scope-Ausweitung.** Das Produkt bleibt aktuell
persönlich und selbst gehostet. `Commercial Readiness` ist eine spätere Prüfphase.

**Akzeptanz:** Billing, öffentliche Registrierung, Multi-Tenancy, Abonnements,
kommerzieller Support, SLA/Hochverfügbarkeit und öffentliche Cloud-Plattform sind
keine beschlossenen aktuellen Produktfunktionen. Ihre Einführung benötigt jeweils
eine ausdrückliche spätere Produktentscheidung.

## Offene Entscheidungen

Noch festzulegen sind der Name des privaten Daten-Repositories und dessen
Migrations-/Backupbetrieb, die konkrete authentifizierte Integrations-API für Haley
und andere Clients, das Export-/Löschverfahren samt Historienaufbewahrung sowie
Produktlizenz und kommerzielle Freigabekriterien. Diese Fragen ändern die oben
beschlossenen Leitplanken nicht. Operative Bearbeitung und Priorisierung erfolgen
ausschließlich über Issues; die [Roadmap](ROADMAP.md) ordnet die Entwicklungsrichtungen ein.
