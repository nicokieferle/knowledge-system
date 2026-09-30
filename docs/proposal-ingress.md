# Authentifizierter Proposal-Eingang v1

`knowledge-proposal-ingress` nimmt mit `POST /v1/proposals` einen fertigen,
ausdrücklich eingereichten Markdown-Entwurf entgegen. Der Client generiert ihn selbst.
Eine Maschinenidentität ist einem festen Browser-Review-Owner zugeordnet. Das ist
eine Delegation zur Einreichung in dessen Queue, keine menschliche Entscheidung.
Vertrag und Abnahme: [Issue #35](https://github.com/Hengsto/Knowledge-System/issues/35).
Die v1-Entscheidungen wurden für die Implementierung vom Maintainer bestätigt.

Dieser Vertrag konkretisiert KS-CORE-003, KS-DATA-002/003/005,
KS-SEC-001/002/003 und KS-CLIENT-001/002/003 aus den
[Requirements](../REQUIREMENTS.md) und die Phase clientunabhängiger Integration der
[Roadmap](../ROADMAP.md#weitere-phasen-und-abhängigkeiten). Ein Deployment ist damit
nicht belegt. Haley, Journaling, Dialog-/Status-APIs und serverseitige Generierung
bleiben außerhalb dieser Operation.

## Konfiguration und lokale Vertrauensgrenze

| Variable | Vertrag |
| --- | --- |
| `PROPOSAL_INGRESS_CLIENT_ID` | Pflicht; stabile Maschinenkennung mit 1–64 ASCII-Buchstaben, Ziffern, `_` oder `-`. Tokenrotation erhält diese Identität. |
| `PROPOSAL_INGRESS_BEARER_TOKEN` | Pflicht; unabhängig zufällig erzeugter opaker Token mit mindestens 32 druckbaren ASCII-Zeichen ohne Whitespace. Niemals in Git oder Logs speichern. |
| `PROPOSAL_INGRESS_OWNER_CLIENT_TYPE`, `PROPOSAL_INGRESS_OWNER_CHAT_ID`, `PROPOSAL_INGRESS_OWNER_USER_ID` | Alle Pflicht; vollständige `ClientIdentity`. Müssen exakt mit den ebenfalls verpflichtenden `REVIEW_OWNER_CLIENT_TYPE`, `REVIEW_OWNER_CHAT_ID`, `REVIEW_OWNER_USER_ID` übereinstimmen. |
| `PROPOSAL_INGRESS_HOST`, `PROPOSAL_INGRESS_PORT` | Default `127.0.0.1`, `8081`; Port 1–65535. |
| `PROPOSAL_INGRESS_ALLOWED_HOSTS` | Kommagetrennte explizite Hosts inklusive benötigtem Port; Default `127.0.0.1:8081,localhost:8081`. Keine Wildcards. Ein abweichender Port verlangt angepasste Hosts. |
| `DATABASE_URL` | Vorhandene PostgreSQL-Verbindung; Schema muss vorher durch den ausdrücklichen Operator-Schritt eingerichtet sein. |

Der bestehende [Konfigurationsloader](local-development.md#konfiguration) lädt `.env`
relativ zum Arbeitsverzeichnis mit `setdefault`; Prozessvariablen haben Vorrang.
Fehlende Zuordnung, unzulässige Werte und gleiche Submit-/MCP-/Browser-Secretwerte
werden beim Start abgewiesen, soweit die jeweiligen Werte gemeinsam verfügbar sind.
Browser-Passworthash und Sitzungsschlüssel werden vom Eingang nicht zur Anmeldung
benutzt. Bei getrennten Prozessen muss der Betreiber unabhängige Credentials selbst
sicherstellen. Private Ownerwerte und Credential-Verteilung sind Betreiberkonfiguration.

Start nach Konfiguration: `knowledge-proposal-ingress`. Genau ein privater
Uvicorn-Prozess; HTTP-Access-Logging und Proxy-Header-Vertrauen sind deaktiviert.
Kein Schema-Init, Reindex oder Modellaufruf beim Start. Der Prozess konstruiert nur
den Eingangsservice, keinen Review-/Apply-Service oder Dateischreiber. Für den Betrieb
keinen beschreibbaren Wissensmount bereitstellen; PostgreSQL enthält dauerhafte Daten.

Loopback und Host-Allowlist richten keinen sicheren entfernten Zugang ein.
Entfernte Nutzung benötigt einen gesondert beauftragten privaten HTTPS-/TLS-Transport
und Secret-Verteilung. Es gibt keine CORS-Freigabe, Cookie-Anmeldung, öffentliche
Registrierung oder Mehrmandantenverwaltung.

## Request und Validierung

Genau ein `Authorization: Bearer <Token>` und ein `Idempotency-Key: <kanonische UUID>`
(kleine Hexzeichen mit Bindestrichen), `Content-Type: application/json`, keine Query.
Authentifizierung erfolgt vor jedem Body-Lesezugriff, auch auf unbekannten Routen.
Doppelte Authorization-Header, alternative Credential-Header und Cookies werden
abgewiesen. Der Vergleich des Bearers erfolgt konstantzeitlich.
Der [MCP-Token](mcp-http-access.md) besitzt keine Einreichungsberechtigung.

Synthetisches Beispiel:

```json
{
  "summary": "Beispiel einer Hypothese",
  "reason": "Suche nach verwandten Thesen: kein passender Treffer. Aufnahme zur Prüfung.",
  "proposed_content": "# Hypothese\r\n\r\nSynthetischer Entwurf.\r\n",
  "title": "Beispiel",
  "target_source_path": "notes/example.md",
  "provenance": {
    "source_ref": "external-session/example/message/7",
    "origin_kind": "model_output",
    "statement_type": "hypothesis",
    "original_text": "Synthetische ursprüngliche Aussage."
  }
}
```

`summary`, `reason`, `proposed_content` und die vier Provenienzfelder sind Pflicht.
`title` und `target_source_path` dürfen fehlen oder null sein. Strings bleiben exakt
erhalten; keine Kürzung, Unicode- oder Zeilenumbruchnormalisierung. Leere Strings,
NUL, ungültiges UTF-8, unbekannte Felder und doppelte JSON-Schlüssel sind unzulässig.
Der vollständige Kandidat besteht zusätzlich `validate_content`; kein Patch/Delete.
Interne Owner-/Conversation-/Message-/Review-/Apply-IDs, Status, Quell-ID, Basisrevision,
Altbytes und Berechtigungsangaben sind keine Eingabefelder.

| Feld | Festes Limit |
| --- | --- |
| Gesamter Request | 512.000 Bytes, auch beim Streaming geprüft |
| Kandidat / Originaltext | Je 128.000 UTF-8-Bytes |
| summary / reason | Je 4.000 UTF-8-Bytes |
| title | 256 UTF-8-Bytes |
| source_ref | 1.024 UTF-8-Bytes |
| Zielpfad | 1.024 Zeichen als Eingangsobergrenze; der vorhandene strengere Schreibvalidator begrenzt zusätzlich auf 240 Zeichen und portable sichere Markdown-Pfade. |

`origin_kind`: `human_statement`, `document_excerpt`, `model_output`.
`statement_type`: `fact`, `interpretation`, `hypothesis`, `assumption`, `forecast`,
`decision`, `proposal`, `open_question`. Beides sind ungeprüfte Clientangaben.
Zusammengesetzte Aussagen kennzeichnet der Client zusätzlich im Markdown.
`source_ref` ist eine opake Herkunftsreferenz; der Dienst ruft keine URLs ab und
benutzt sie nicht für Lookups oder Autorisierung. Ein optionaler Zielpfad schlägt
nur ein Ziel in `knowledge-git` vor; Unicode-Lesepfade erweitern die Schreibgrenze nicht.
Ohne Ziel legt der Mensch dieses im Browser fest, der auch die aktuelle Basis ermittelt.

Vor neuen Thesen sucht der Client über den lesenden Zugang nach verwandtem Wissen.
`reason` berichtet Ergebnis/Vorbezüge und begründet materielle Änderungen. Der Eingang
verifiziert weder die Suche noch die Wahrheit der Aussage. Menschliches Review prüft
Fakten, Bedeutung, Belege und den vollständigen Alt-/Neuvergleich.

## Atomare Speicherung und Wiederholung

Schlüssel ist `(client_id, Idempotency-Key)` ohne Ablauf während der Lebensdauer
des Auditdatensatzes. Fingerprint ist SHA-256 über das validierte v1-Objekt einschließlich
auf null ergänzter optionaler Felder: Python-JSON mit `sort_keys=True`,
`ensure_ascii=False`, `separators=(",", ":")`, `allow_nan=False`, als UTF-8.
JSON-Whitespace und Reihenfolge verändern ihn nicht, geänderte Feldwerte schon.

Eine PostgreSQL-Transaktion speichert gemeinsam:

1. Einen unmittelbar archivierten Gesprächscontainer, `client_type=proposal-ingress`,
   `external_conversation_id=v1:<client_id>:<UUID>`. Vorhandener Unique-Index und
   Zeilensperre serialisieren konkurrierende Requests.
2. Owner-Elterneintrag soweit nötig und `client_conversations`-Zuordnung. Aktives
   Thema, Version und Zeitstempel eines bestehenden `client_states` bleiben erhalten.
   Archivierte Container erscheinen nicht als Router-/Telegram-Themen.
3. Exakten Originaltext und separate lesbare Herkunftsnachricht. Clientkennung,
   Quellenart, Referenz und Aussagetyp sind ausdrücklich eingereichte Angaben.
   Die Rolle `user` bedeutet hier Kontext, keine bestätigte menschliche Autorschaft.
4. Separaten Kontrolltrigger mit `metadata.control=external_submission`, Fingerprint,
   authentifizierter Clientkennung, Owner-Bindung und Receipt. Nur Original-/Herkunfts-
   nachrichten stehen in `originating_message_ids`, jeweils aus derselben Conversation.
5. Genau ein unverändertes `pending`-Proposal mit `source_client=<client_id>` und
   `trigger_type=command`. Keine Review-Revision, Entscheidung, Apply- oder Indexdaten.

`201` folgt erst nach Commit. Beispielreceipt:

```json
{"proposal_id":"<UUID>","conversation_id":"<UUID>","submission_state":"recorded"}
```

Gleicher Schlüssel/Fingerprint liefert `200` und genau das gespeicherte Receipt,
auch nach Neustart, verlorener Antwort, Tokenrotation oder abgeschlossenem Review.
Es bestätigt die historische Einreichung, keinen aktuellen Proposalstatus.
Abweichende Payload: `409 idempotency_conflict`. Geänderte Owner-Bindung:
`409 binding_conflict`, auch bei geänderter Payload. Kein Umhängen bestehender
Einreichungen. Andere Maschinenidentität hat einen eigenen Schlüsselraum.
DB-Fehler vor Commit rollen sämtliche Komponenten zurück. Es gibt keine neue Tabelle
oder Migration; bestehende Archiv-/Restore-Verfahren erfassen die Metadaten und Beziehungen.

## Browser und Fehler

Die ownershipgefilterte paginierte Queue erreicht auch archivierte Auditcontainer.
Das Detail rendert Original und Herkunft mit HTML-Escaping. Queue-/Detail-GET und
Submit/Replay bereiten kein Review vor. Erst der gesondert angemeldete Browser-POST
mit CSRF erzeugt eine Revision; Entscheidung und Accept & Apply bleiben
[revisionsgebunden](v33-design.md). Submit-Token ersetzt weder Anmeldung noch CSRF.

| Status | Sichere Fehlercodes / Bedeutung |
| --- | --- |
| 400 | `invalid_json`, `invalid_idempotency_key`, `invalid_host`, `query_not_allowed` |
| 401 | `unauthorized`, mit `WWW-Authenticate: Bearer`; hat Vorrang vor Eingabevalidierung |
| 413 | `request_too_large`, `content_too_large` |
| 415 | `unsupported_media_type` |
| 422 | `invalid_submission` |
| 409 | `idempotency_conflict`, `binding_conflict` |
| 503 | `submission_unavailable`; DB-Wiederholung mit demselben Schlüssel |
| 404 / 405 | `not_found` / `method_not_allowed`, keine zusätzlichen Operationen |

Fehlerkörper enthalten nur `error` und eine serverseitige `correlation_id`, keine
Eingabewerte, SQL, Tokens oder Tracebacks. Keine Body-/Provenienz-/Diff-Logs.

## Prüfgrenzen

[HTTP-Tests](../tests/test_proposal_ingress.py) verwenden einen Ersatzservice zur
Validierung der Transportgrenze. [Integration](../tests/integration/test_proposal_ingress_postgres.py)
verwendet echte Stores/Services, PostgreSQL und Browser-TestClient; sie prüft
Ownership, Parallelität, Konflikte, Rollback, unveränderte aktive Themen und Pagination.
Der [Archiv-/Restore-Test](../tests/integration/test_proposal_review_postgres.py)
prüft zusätzlich exakte Inhalte, Metadaten und Replay nach Wiederherstellung.

[Image-Smoke](../scripts/proposal_ingress_image_smoke.py) startet im gebauten Image
als dessen Benutzer die echten `knowledge-proposal-ingress`- und `knowledge-review`-
Prozesse mit Uvicorn, synthetischen Credentials und eigener PostgreSQL-Schema-Namespace.
Gültiger POST, Replay, Konflikt, fehlender/falscher Token, getrennte Browseranmeldung
und sichtbare Herkunft werden über echtes Loopback-HTTP geprüft. Kein Apply, kein
Modell, Telegram, Remote-Client, TLS-Terminator oder produktiver Server. Build und
Smoke laufen über [CI-Reproduktion](CI.md#lokale-reproduktion); aktuelle Ergebnisse
und Artefaktidentität gehören in den Implementierungs-PR.
