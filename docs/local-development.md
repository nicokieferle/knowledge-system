# Lokale Entwicklung und Retrieval

Diese Anleitung übernimmt die bisherigen Setup-, MCP- und Eval-Inhalte aus dem
README. Sie beschreibt die vorhandenen Befehle, keinen in diesem Dokumentationsrefactor
neu ausgeführten Start oder Test. Architektur: [conversation-architecture.md](conversation-architecture.md).
Prüfauswahl und CI: [CI.md](CI.md#prüfauswahl-und-nachweise).

## Voraussetzungen und Einrichtung

Python ab 3.11, pip/venv und Docker mit Compose für das lokale PostgreSQL/pgvector
werden benötigt. Abhängigkeiten und Einstiegspunkte stehen in [pyproject.toml](../pyproject.toml).
Die CI nutzt Python 3.13, das Serverimage Python 3.12. Im Repository-Root:

```bash
cp .env.example .env
docker compose up -d
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
knowledge init-db
knowledge index
knowledge search "Wie können Staatsschulden die Geldpolitik beeinflussen?" --mode quality
knowledge search "Wie können Staatsschulden die Geldpolitik beeinflussen?" --mode fast
```

Unter Windows PowerShell statt der Bash-Aktivierung:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Vorhandene lokale `.env` nicht überschreiben. [docker-compose.yml](../docker-compose.yml)
startet ausschließlich die lokale Datenbank mit Entwicklungswerten und Loopback-Port
5432. Daten bleiben im Volume `knowledge_pgdata`; darin können auch dauerhafte
Gesprächs-/Review-Daten liegen. Ein Index-Neuaufbau ist kein Grund, dieses Volume zu löschen.
`knowledge init-db` legt das Schema additiv an. Indexierung liest kanonisches Markdown;
Embeddings und Reranking können beim ersten Gebrauch lokale Modelldownloads auslösen.

## Konfiguration

[config.py](../src/knowledge_system/config.py) liest `.env` relativ zum Arbeitsverzeichnis.
Bereits gesetzte Umgebungsvariablen haben Vorrang (`setdefault`). Der kleine Loader
führt keine Shell-Befehle aus. [.env.example](../.env.example) enthält ausschließlich
Entwicklungsbeispiele; Werte für persönliche Hosts und Secrets gehören nicht in Git.

| Einstellung | Zweck, Pflichtstatus und vorhandener Default |
| --- | --- |
| `DATABASE_URL` | PostgreSQL-Verbindung; lokaler Default entspricht Compose. Ohne vorhandenes `connect_timeout` ergänzt der Loader bei PostgreSQL-URLs 5 Sekunden. Secrets enthalten. |
| `KNOWLEDGE_ROOT` | Quellverzeichnis, Default `./knowledge`; wird als Pfad aufgelöst. Review-Apply benötigt einen vorhandenen sicheren, beschreibbaren Root außerhalb produktiven Anwendungscodes. |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` | Modellname und ganzzahlige Dimension; Defaults `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` und `384`. Modellwechsel kann Index-Neuaufbau erfordern. |
| `MCP_TRANSPORT` | `stdio` (Default) oder `streamable-http`. |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | Defaults `127.0.0.1`, `8000`, `/mcp`; Port 1–65535, absoluter URL-Pfad ohne Query/Fragment. |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | Kommagetrennte explizite Allowlist ohne Wildcards; Origins benötigen Hosts. Ohne explizite Hosts gilt Loopback-Schutz. |
| `MCP_HTTP_CLIENT_ID` / `MCP_HTTP_BEARER_TOKEN` | Für Streamable HTTP verpflichtender serverseitiger Principal und opaker Token (mindestens 32 druckbare ASCII-Zeichen); stdio benötigt sie nicht. Siehe [MCP-HTTP-Vertrag](mcp-http-access.md). |
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` | Für Telegram unterstützt der Start derzeit `openai-compatible`; Modell und geheimer API-Key müssen nichtleer gesetzt sein. |
| `LLM_BASE_URL` / `LLM_TIMEOUT_SECONDS` | Defaults `https://api.openai.com/v1` und `30`; konfigurierbarer Endpunkt und numerischer Timeout. |
| `TELEGRAM_BOT_TOKEN` | Für `knowledge-telegram` verpflichtender geheimer Token, getrennt vom Journaling-Bot. |
| `REVIEW_*` | Authentifizierung, Owner, Sitzung und erlaubte Hosts gemäß [Browser-Konfiguration](v321-browser-review.md#authentication-and-sessions); es gibt kein Standardpasswort. |

Die Konfiguration prüft unter anderem MCP-Transport/Port/Allowlist, nichtleere
Chat-Pflichtwerte und numerische Umwandlungen. Das ist keine umfassende Validierung
aller Providerwerte oder ein Verbindungsnachweis. Der Browser prüft Auth-Konfiguration
und Apply-Root zusätzlich beim Start. Datenbankinitialisierung und Indexierung laufen
explizit, nicht beim Start des MCP- oder Browserdienstes.

## Retrieval-Evaluation

Die vorhandene Suite enthält 35 Fälle mit `id`, `query`, `expected_sources`, optionalen
`expected_headings` und optionaler `description`. Sie liefert Hit@1, Hit@3, Hit@5 und
MRR. Das thematisch begrenzte Korpus ist eine lokale Baseline, kein breiter Benchmark.
Aus dem Root nach Datenbankinitialisierung und Indexierung:

```bash
knowledge eval --suite eval/retrieval_v01.jsonl
knowledge eval --suite eval/retrieval_v01.jsonl --retriever keyword --text-config german
knowledge eval --suite eval/retrieval_v01.jsonl --retriever hybrid
knowledge eval --suite eval/retrieval_v01.jsonl --retriever reranker
knowledge eval --suite eval/retrieval_v01.jsonl --json
```

Der Eval-Default ist die semantische Vektorbaseline mit exakter Cosinus-Suche.
`keyword` unterstützt PostgreSQL `german` und `simple`; `german` ist für die bestehende
deutsche Suite voreingestellt. `hybrid` kombiniert Vektor- und Keyword-Ergebnisse per
Reciprocal Rank Fusion. `reranker` bewertet Keyword-Kandidaten mit
`BAAI/bge-reranker-v2-m3`. Diese Vergleichsmodi sind getrennt von der Produktsuche:
`fast` verwendet deutsche Volltextsuche, `quality` deren Kandidaten plus Reranker.
Der Reranker wird verzögert geladen und innerhalb eines Serviceprozesses wiederverwendet.
Es gibt derzeit keinen HNSW-Index. Frontmatter wird beim Chunking entfernt; eine
strukturierte Metadatenauswertung ist eine mögliche spätere Erweiterung.

## Lesender MCP-Zugang

Ein MCP-Client kann den stdio-Prozess mit `knowledge-mcp` starten. Für lokalen
Streamable-HTTP-Betrieb unter PowerShell:

```powershell
$env:MCP_TRANSPORT="streamable-http"
$env:MCP_HOST="127.0.0.1"
$env:MCP_PORT="8000"
$env:MCP_PATH="/mcp"
$env:MCP_HTTP_CLIENT_ID="external-read"
$env:MCP_HTTP_BEARER_TOKEN="<lokal erzeugter zufälliger Token>"
knowledge-mcp
```

Die lokale URL lautet `http://127.0.0.1:8000/mcp`. Unter Bash lassen sich dieselben
Umgebungsvariablen vor `knowledge-mcp` setzen. Vor echten Suchaufrufen Datenbank und
Index wie oben vorbereiten. Protokolltests: [CI.md](CI.md#prüfauswahl-und-nachweise).

Es gibt genau zwei Tools: `search_knowledge` sucht ohne Schreib-/Indexoperation;
`get_document` liest ein Original anhand der logischen `source_id`/`source_path` aus
dem Suchergebnis. Beliebige Betriebssystempfade sind kein zulässiger Ersatz.

DNS-Rebinding-Schutz und Host-/Origin-Allowlist sind vorhanden. Nicht-Loopback-Bindings
benötigen explizite Sicherheitskonfiguration. Der Endpunkt verlangt jetzt zusätzlich
den [serverseitigen Maschinen-Token](mcp-http-access.md). TLS und öffentlicher Zugriff
sind damit nicht eingerichtet.
Das [HTTP-Smoke-Skript](../scripts/mcp_http_smoke.py) kann optionale
`CF_ACCESS_CLIENT_ID`-/`CF_ACCESS_CLIENT_SECRET`-Header zusätzlich zum Pflicht-Bearer
senden. Das belegt weder einen
aktuell eingerichteten Tunnel noch einen authentifizierten ChatGPT-Client.

## Gespräch, Telegram und Browser

`knowledge-telegram` startet nach Konfiguration den Long-Polling-Client mit
OpenAI-kompatiblem Provider. `/new`, `/topics` und `/switch` steuern Themen;
`/remember`, `/propose` und Vorschlagsbestätigungen erzeugen Proposals.
Review-Kommandos/-Altbuttons verweisen nur noch auf den Browser.

Für isolierte Browserarbeit den [lokalen Review-Start](v321-browser-review.md#local-start-and-checks)
mit synthetischer Datenbank, sicherem temporärem Wissensroot und eigenen Test-Secrets
verwenden. V3.3-Apply schreibt tatsächlich freigegebene Dateien und unterstützt
Linux-Dateisystemoperationen; dafür keine produktiven Wissensdaten einsetzen.

Chat/Router/Zusammenfasser und Proposal-Generator können Gesprächskontext und
retrievtes Wissen an den konfigurierten LLM-Endpunkt übertragen. Telegram empfängt
Nachrichten und Antworten. Dies sind externe Datenschutzgrenzen; lokale Embeddings
machen diese Übertragungen nicht lokal. Historische Retry-/Idempotenzgrenzen stehen
im [Telegram-Hotfixbericht](v311-telegram-hotfix.md).
