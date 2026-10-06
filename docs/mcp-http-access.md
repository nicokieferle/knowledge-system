# MCP-HTTP: erster lesender Maschinenzugang

## Schnittstellenvertrag

`knowledge-mcp` bietet über Streamable HTTP dieselben zwei Tools wie über stdio:
`search_knowledge` (`query`, `mode`, `limit`) und `get_document` (`source_id`,
`source_path`). Suche liefert Quell-ID und logischen Quellpfad; der Abruf liefert
das Originaldokument mit beiden Angaben. Es gibt keine Schreib-, Review-, Apply-
oder Indexierungsoperation in diesem MCP-Server.

Der HTTP-Prozess hat genau einen konfigurierten Maschinen-Principal
(`MCP_HTTP_CLIENT_ID`). Jeder HTTP-Request, einschließlich MCP-Initialisierung,
Tool-Liste, Tool-Aufruf und Sitzungsfortsetzung, muss genau einen Header
`Authorization: Bearer <token>` tragen. Der Server vergleicht den opaken Token
mit `MCP_HTTP_BEARER_TOKEN` in konstanter Zeit. Das Secret benötigt mindestens
32 druckbare ASCII-Zeichen ohne Leerraum; ein zufälliger 32-Byte-Wert in Hexform
ist geeignet. Der Principal und seine Rechte stammen ausschließlich aus der
Serverkonfiguration. Client-Header, Tool-Argumente und Sitzungs-IDs können sie
nicht erweitern. Mehrere oder anders formatierte Authorization-Header,
alternative Credentials und Query-Parameter werden abgewiesen.

Für diese Stufe ist nur `knowledge-git` freigegeben. Suche filtert die Quelle
bereits in der Indexabfrage und prüft das Ergebnis erneut. `get_document` prüft
Quell-ID und den lesenden logischen Markdown-Pfad vor dem Quellenadapter.
Andere registrierte Quellen bleiben gesperrt; ihre Freigabe benötigt eine neue
ausdrückliche Code-/Vertragsänderung. stdio bleibt der bisherige lokale Zugang
und nutzt diesen HTTP-Principal nicht.

Der lesende Pfad ist der exakte, relative `/`-Pfad, den `GitMarkdownSource.discover`
für eine reguläre `.md`-Datei unter dem konfigurierten Wissensroot ausgibt. Unicode
und Leerzeichen in Dateinamen sind zulässig; Pfade werden nicht normalisiert oder
umbenannt. Absolute Pfade, leere, `.`-, `..`- und versteckte Komponenten, andere
Endungen sowie die Root-`README.md` sind ausgeschlossen. Symlinks und Junctions
innerhalb des Root werden weder indexiert noch gelesen; der POSIX-Abruf öffnet
Verzeichnisse und Datei ohne Symlink-Folgen. Der konfigurierte Root selbst bleibt
eine vertrauenswürdige lokale Konfiguration. Die strengere portable ASCII-Grenze
von Review/Apply gilt weiterhin für Schreibziele und wird hier nicht erweitert.

Fehlende, falsche oder mehrdeutige Berechtigungsnachweise liefern HTTP 401 mit
`WWW-Authenticate: Bearer` und ohne Dokumentinhalt. Unbekannte, nicht freigegebene
und ungültige Quellen/Pfade ergeben nach erfolgreicher Authentifizierung einen
MCP-Toolfehler ohne Dokumentinhalt. Ein fehlender Index oder eine unlesbare Quelle
wird als bestehender generischer MCP-Toolfehler gemeldet. Tokens erscheinen weder
in Fehlermeldungen noch in den abgeschalteten HTTP-Access-Logs.

## Konfiguration und lokale Grenze

Bei `MCP_TRANSPORT=streamable-http` sind `MCP_HTTP_CLIENT_ID` und
`MCP_HTTP_BEARER_TOKEN` Pflichtwerte. Ohne sie startet der HTTP-Prozess nicht.
Bei `stdio` sind sie nicht erforderlich. Der Prozess liest die Werte aus der
Umgebung beziehungsweise der lokalen `.env`; die Beispiel-Dateien enthalten
keine Tokens. `compose.server.yml` verlangt beide Werte nur für den MCP-Dienst.
Dadurch muss auch die Compose-Interpolation vor einem `run`/`build` einen lokalen,
privat verwalteten Wert sehen. Das bestehende PostgreSQL-only-`docker-compose.yml`
und `knowledge-mcp` über stdio brauchen keine neue Konfiguration.

Lokales Beispiel in einer privaten Shell, nach Installation und Indexaufbau:

```bash
export MCP_TRANSPORT=streamable-http
export MCP_HTTP_CLIENT_ID=external-read
export MCP_HTTP_BEARER_TOKEN="$(openssl rand -hex 32)"
knowledge-mcp
```

Ein Client erhält den Token über einen gesonderten sicheren Kanal und sendet ihn
als Bearer-Header an `http://127.0.0.1:8000/mcp`. Für den lokalen Smoke verwendet
`scripts/mcp_http_smoke.py` denselben Umgebungswert; optionale Cloudflare-Access-
Header allein genügen nicht mehr. Keine Tokenwerte in Befehlsargumenten, Tickets,
Logs oder PRs ablegen. Rotation erfolgt durch Austausch des serverseitigen Werts
und Neustart des MCP-Prozesses; bestehende Clients benötigen danach den neuen Wert.

Die Default-Bindung bleibt `127.0.0.1`; Server-Compose veröffentlicht weiterhin
nur auf Host-Loopback. Host-/Origin-Allowlist und DNS-Rebinding-Schutz bleiben
zusätzliche Prüfungen, keine Identität. HTTP auf Loopback schützt einen Token
nicht gegen andere Prozesse oder Benutzer mit Zugriff auf diesen Host. Für einen
entfernten Client wären ein gesondert geprüfter privater Transport mit TLS,
Secret-Verteilung und Netzwerkfreigabe nötig; das gehört nicht zu dieser Änderung.
Die Browser-Review-App hat eine getrennte Authentifizierung und erhält keine
MCP-Maschinenberechtigung.

Der gemeinsame lokale [Prüfnachweis](proposal-ingress.md#prüfgrenzen) verbindet
authentifizierte Suche und Originalabruf mit der anschließenden Einreichung eines
fertigen Entwurfs über den separaten Proposal-Eingang. Er prüft die getrennten
Tokens und den unveränderten Wissensbestand. Die dort berichtete Suche bleibt
eine ungeprüfte Clientangabe; der Eingang verifiziert keinen MCP-Retrievalnachweis.
