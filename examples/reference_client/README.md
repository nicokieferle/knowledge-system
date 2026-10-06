# Lokales Referenzbeispiel

Dieses unabhängige Terminalbeispiel macht die bestehenden
[MCP-HTTP](../../docs/mcp-http-access.md)- und
[Proposal-Verträge](../../docs/proposal-ingress.md) praktisch nutzbar.
Es gehört als ausführbares Schnittstellenbeispiel zu KS-CLIENT-002, liegt außerhalb
von Produktpaket und Produktimage und importiert keinen Knowledge-System-Code.
Allgemeine Assistenz, Orchestrierung, Dialog und dauerhafte Oberflächen bleiben
eigenständigen Clientprojekten beziehungsweise Haley zugeordnet. Das Beispiel
erzeugt keinen Text und führt keine Browser-, Review- oder Apply-Operation aus.

## Einrichtung

Python ab 3.11. In einer eigenen Virtualenv genügt die kleine Clientabhängigkeit;
Datenbank-, Modell- oder Knowledge-System-Pakete benötigt der Client nicht:

```bash
python3 -m venv /tmp/ks-reference-venv
/tmp/ks-reference-venv/bin/pip install -r examples/reference_client/requirements.txt
```

`client.py` kann mit `requirements.txt` in ein separates Verzeichnis kopiert und
von dort ausgeführt werden. Die relativen Befehle unten gelten vom Repository-Root;
außerhalb davon absolute Pfade verwenden. Lokal konfigurierte und bereits gestartete
Dienste werden vorausgesetzt: MCP auf `127.0.0.1:8000/mcp`, Proposal-Eingang auf
`127.0.0.1:8081`. Einrichtung/Indexaufbau sind gesonderte Operator-Schritte gemäß
[lokaler Entwicklung](../../docs/local-development.md); dieses Beispiel startet,
konfiguriert oder migriert keinen Dienst.

Ohne Prozessvariablen fragt jeder Befehl den passenden Token verdeckt am Terminal
ab. Alternativ ausschließlich `MCP_HTTP_BEARER_TOKEN` beziehungsweise
`PROPOSAL_INGRESS_BEARER_TOKEN` über die private Prozessumgebung bereitstellen.
Keine Tokenargumente, keine `.env`-Datei, keine Tokenausgabe oder Speicherung durch
den Client. Beide Tokens müssen unabhängig sein. Prozessumgebung und Loopback
bleiben die dokumentierte lokale Vertrauensgrenze; fremde lokale Prozesse mit
entsprechenden Rechten sind dadurch nicht ausgeschlossen.

## Ablauf

1. Verwandtes Wissen suchen; Treffer enthalten `source_id` und `source_path`:

   ```bash
   /tmp/ks-reference-venv/bin/python examples/reference_client/client.py search "verwandte These"
   ```

   Standard: `--mode fast --limit 5`; optional `--mode quality` gemäß bestehendem
   Retrievalvertrag. Keine Treffer ist ein erfolgreiches Suchergebnis.

2. Das Original mit der exakten Trefferreferenz lesen, einschließlich Unicode und
   Leerzeichen im Pfad:

   ```bash
   /tmp/ks-reference-venv/bin/python examples/reference_client/client.py get knowledge-git "notes/example.md"
   ```

   Ausgabe ist der Originaltext als UTF-8 ohne zusätzliches JSON oder angehängten
   Zeilenumbruch. Ausgaben erscheinen ausschließlich im eigenen Terminal; der Client
   legt keine Wissens- oder Ergebnisdateien an. Nicht in CI-/geteilte Logs umleiten.

3. Einen fertigen eigenen UTF-8-Markdown-Entwurf und separate JSON-Metadaten
   bereitstellen. [metadata.example.json](metadata.example.json) zeigt alle Felder.
   Originalaussage, Aussagetyp, Quellenart und Herkunftsreferenz selbst korrekt setzen;
   `reason` beschreibt Suche, Vorbezug und Änderungsgrund. `title` und Zielpfad sind
   optional. Metadaten enthalten kein `proposed_content`; dieses kommt vollständig
   aus der Markdown-Datei. Keine Secrets in Entwurf oder Metadaten ablegen.

   Einen Einreichungsschlüssel erzeugen und für diesen Entwurf aufbewahren. Er ist
   kein Secret; der Client erzeugt oder speichert ihn nicht automatisch:

   ```bash
   /tmp/ks-reference-venv/bin/python -c 'from uuid import uuid4; print(uuid4())'
   /tmp/ks-reference-venv/bin/python examples/reference_client/client.py submit /private/draft.md /private/metadata.json --idempotency-key <UUID>
   ```

   `<UUID>` durch die ausgegebene UUID ersetzen. Markdown wird binär gelesen und
   strikt als UTF-8 dekodiert: CRLF und Unicode bleiben erhalten. Der Server prüft
   Inhalte, Provenienz und sichere Schreibziele abschließend. Receipt und Proposal-ID
   bestätigen die Aufnahme in die Owner-Queue, keine Akzeptanz oder Übernahme.
   Bei Timeout/verlorener Antwort dieselben Dateien mit demselben Schlüssel erneut
   einreichen; `Replay` bestätigt die bestehende Aufnahme. Geänderte Inhalte brauchen
   einen neuen Schlüssel. Ein HTTP-409-Konflikt wird nicht automatisch umgangen.

4. Den eigenen Status mit der Proposal-ID aus dem Receipt abfragen:

   ```bash
   /tmp/ks-reference-venv/bin/python examples/reference_client/client.py status <PROPOSAL-UUID>
   ```

   Jede Abfrage ist ein einzelner GET. Bei Bedarf manuell erneut ausführen.
   Review, akzeptierte Revision, Datei-Apply und Indexierung werden getrennt gezeigt.
   „Kein Apply-Journal“ bedeutet auch bei Akzeptanz keine bestätigte Dateiübernahme.
   „Angewendet“ und „Indexierung fehlgeschlagen“ können gleichzeitig gelten.
   Status beschreibt gespeicherte Verarbeitung, keine aktuelle Dateiintegrität,
   Retrievalqualität, Git-Versionierung oder Deploymentbestätigung. Korrekturen im
   Browser können den Inhalt der akzeptierten Revision gegenüber dem Entwurf ändern.

Für andere lokale Ports `--mcp-url http://127.0.0.1:PORT/mcp` beziehungsweise
`--ingress-url http://127.0.0.1:PORT` am jeweiligen Befehl verwenden. Nur numerische
Loopback-Adressen, HTTP und explizite Ports sind zugelassen; keine URL-Credentials,
Query, Fragmente, Redirects oder Umgebungsproxys. Der Netzwerkablauf ist nach der
Tokeneingabe auf 60 Sekunden begrenzt. Fehler melden feste verständliche Hinweise
und Exitcode 1 (ungültige Befehlsargumente: 2), ohne
Serverantworten, Tracebacks oder Credentials auszugeben. Exitcode 0 bedeutet nur
den erfolgreichen jeweiligen Befehl. Inhalte mit Tokens oder Terminal-Steuerzeichen
werden nicht ausgegeben; solche Originale bleiben über dieses Beispiel unabrufbar.

## Nachweisgrenze

Die gezielten [Tests](../../tests/test_reference_client.py) verbinden den echten
Client mit lokalem MCP-HTTP und der bestehenden Eingangs-HTTP-App. Suchdienst und
dauerhafter Eingangsservice sind synthetische Ersatzkomponenten; PostgreSQL,
Suchqualität, Modell, Remote-/TLS-Transport und Produktivbetrieb werden damit nicht
geprüft. Die bestehenden PostgreSQL-Nachweise der Verträge bleiben getrennte Belege.
Das Beispiel bindet Suche und Einreichung nicht serverseitig zusammen; Provenienz und
Suchbegründung bleiben ungeprüfte Clientangaben. Konkrete Prüfläufe stehen im PR zu
[Issue #43](https://github.com/Hengsto/Knowledge-System/issues/43).
