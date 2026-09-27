# GitHub Actions CI

## Prüfauswahl und Nachweise

Diese Datei ist der maßgebliche Prüfungsort. Befehle aus dem Repository-Root mit
installiertem `dev`-Extra ausführen; Einrichtung: [lokale Entwicklung](local-development.md).
Prüfungen, Umgebung, Commit einschließlich uncommitteter Änderungen und Ergebnis
im zugehörigen PR belegen. Historische Zahlen weiter unten gelten nur für den dort
genannten Stand. Aktuelle Aufgaben und Blocker gehören ausschließlich in
[GitHub Issues](https://github.com/Hengsto/Knowledge-System/issues).

| Änderung / Prüfung | Lokaler Befehl oder Auswahl | Erwartung und Grenze |
| --- | --- | --- |
| Nur Markdown | `git diff --check`; geänderte relative Links samt Ankern, Requirement-IDs, Statusaussagen und gesamten Diff gegen Quellen prüfen | Keine kaputten Links, unbeabsichtigten Dateien, Platzhalter oder unbelegten Betriebsbehauptungen. Es gibt keinen dedizierten Markdown-/Link-Checker im Repository. |
| Betriebsdokumentation | `python -m pytest tests/test_deployment.py::test_debian_documentation_has_durable_backup_and_defensive_restore_commands -q` | Vorhandene statische Dokumentationsprüfung; startet keinen Dienst und erstellt kein Backup. |
| Funktionale Änderungen: Kernprüfungen | `python -m ruff check .`, `python -m ruff format --check .`, `python -m pytest tests --ignore=tests/integration -q -ra` | Lint, Format und Unit-/lokale Protokolltests bestehen; keine produktiven Credentials. |
| Retrieval-/Quelländerungen | Zusätzlich betroffene `test_chunking`, `test_sources`, `test_indexer`, `test_search`, `test_reranker`, `test_evaluation`, `test_service` unter `tests/`; bei Qualitätsänderung [Retrieval-Evaluation](local-development.md#retrieval-evaluation) | Quellen-/Indexintegrität und messbare Suchqualität; Modelldownloads/isolierte DB bei realer Evaluation berücksichtigen. |
| MCP, Gespräch, Routing, Review/Apply | Entsprechende `tests/test_mcp_server.py`, `test_conversation_*.py`, `test_telegram_*.py`, `test_proposal_*.py`, `test_review_web.py`, `test_knowledge_apply.py`; betroffene PostgreSQL-Integration gemäß lokaler Reproduktion | Plattform-, Auth-, Revisions- und Persistenzgrenzen testen. Reale DB-Prüfung nur mit den dokumentierten isolierten Opt-ins; Skips nicht als Erfolg melden. |
| Schema, dauerhafte Daten, Betrieb oder Build | [Lokale Reproduktion](#lokale-reproduktion), einschließlich isolierter Integration und gebautem Image-Smoke | Keine Produktionsressourcen; separates Testprojekt. |

Für funktionale Änderungen müssen Kern- und betroffene Bereichsprüfungen bestehen.
Bei reiner Dokumentation sind lokale Volltests nicht pauschal nötig. Die bestehende
PR-CI hat keinen Markdown-Pfadfilter und führt trotzdem ihr unverändertes Programm aus.

Vor einer Merge-Bereitschaft sind **CI / admission** und **CI / verify** am aktuellen
PR-Head beziehungsweise zugehörigen Test-Merge-Commit nachzuweisen. Ein lokaler Erfolg
oder ein älterer grüner Lauf genügt nicht. Das ist eine fachliche Prüfpflicht,
keine Behauptung einer technischen Merge-Sperre: Beim lesenden Abgleich am
27. September 2026 verweigerten GitHubs Branch-Protection- und Ruleset-APIs den Zugriff
mit HTTP 403 und Tarifhinweis. Eine technische Erzwingung konnte daher nicht bestätigt
werden. Die Einstellungen wurden nicht verändert.

Die bestehende `.auto-coding.toml` nennt für den externen Controller nur `CI / verify`
und `auto_merge = true`. Das belegt weder die effektive Controller-Policy noch erlaubt
es einem Agenten eigenständig Auto-Merge. Diese Konfiguration bleibt von den hier
dokumentierten Prüfpflichten und den konkreten Befugnissen eines Auftrags getrennt.

## Ablauf und Vertrauensgrenze

`.github/workflows/ci.yml` prüft PRs gegen `main`, Pushes auf `main` und manuelle
Läufe auf `main`. Der Hauptbranch wurde über `origin/HEAD` und die GitHub-API bestätigt.
Concurrency bricht ältere Läufe desselben PRs/Branches ab, nicht andere PRs.

Die externe Sicherheitsgrenze für Forks wurde am 2026-09-17 vom Betreiber direkt in
GitHub unter Settings → Actions → General → Fork pull request workflows bestätigt:
**„Run workflows from fork pull requests“ ist nicht aktiviert.** Dies wurde nicht per
API verifiziert. Fork-PR-Code wird damit nicht auf dem persistenten Runner ausgeführt.
Ein Maintainer muss geprüfte Änderungen in einen vertrauenswürdigen internen Branch/PR
übernehmen; es gibt keine automatische Übernahme oder Freigabe per Label.

`CI / admission` ist eine zusätzliche Prüfung auf GitHub-hosted Ubuntu und führt
keinen Checkout aus. Ereignis-Sender, `actor` und `triggering_actor` müssen vorhanden
und identisch sein. Für menschliche PRs müssen Kopf und Ziel im selben Repository
liegen; die Autoren-Zuordnung muss OWNER, MEMBER oder COLLABORATOR sein.
Seit [PR #13](https://github.com/Hengsto/Knowledge-System/pull/13) erlaubt der Workflow
zusätzlich die App `hengsto-auto-coding-runner[bot]`, ausschließlich bei eigenen PRs
aus demselben Repository mit Branchpräfix `codex/` und übereinstimmendem Bot-Sender.
Andere Bots, Forks und Re-runs durch einen anderen Account werden abgewiesen.
Kein `pull_request_target`. Maßgeblich ist die Bedingung in
[ci.yml](../.github/workflows/ci.yml).

Admission ist keine unveränderbare Sicherheitsgrenze gegen Personen oder Bots mit
Workflow-Schreibrechten: Workflow-Dateien sind selbst PR-Code und können von solchen
Konten geändert werden. Die Prüfung ersetzt weder GitHub-Zugriffskontrolle noch die
oben bestätigte Fork-Einstellung. Änderungen an CI-Dateien müssen weiterhin geprüft
werden. Dependabot schlägt wöchentlich ausschließlich Action-Updates vor.

`CI / verify` führt seriell aus: Preflight, frische pip-Installation, `ruff check .`,
`ruff format --check .`, Unit-Tests, PostgreSQL-Integration einschließlich
Archive/Restore, Produktions-Docker-Build und den bestehenden
`conversation_postgres_smoke.py` im gebauten Image. Ein Job vermeidet mehrfaches
Installieren auf dem einzelnen Runner. Gesamtlimit: 45 Minuten; Teilprüfungen haben
zusätzliche Limits. Pflichtfehler bleiben Fehler, auch bei `tee` (`pipefail`).

Stabile Prüfnamen: **`CI / admission` und `CI / verify`**. Beide nachweisen:
Ein übersprungener Self-hosted-Job allein macht einen ausgeschlossenen PR nicht
mergefähig. Technische Erzwingung: siehe [Prüfauswahl](#prüfauswahl-und-nachweise).

## Runner und vorhandene Projektwerkzeuge

Labels: `[self-hosted, linux, x64, ci, rootless-docker, knowledge-system]`.
Die Job-Metadaten beider erfolgreichen GitHub-Läufe bestätigen genau diese sechs
Labels sowie den Runner-Namen `ci` und die Gruppe `Default`.
Am 2026-09-16 wurden laut Betreiber auf der CI-VM als `runner-knowledge-system`
Python **3.13.5**, eine erfolgreiche Virtualenv-Erstellung mit `python3.13 -m venv`,
pip **25.1.1** und GitHub-Actions-Runner **2.337.0** geprüft.
Die Action-Releases verwenden Node 24 (Runner mindestens 2.327.1);
die bestätigte Runner-Version erfüllt diese Voraussetzung.

Benutzer `runner-knowledge-system`, Socket `unix:///run/user/1000/docker.sock`.
Preflight verlangt Rootless und den integrierten Buildx-`docker`-Treiber.
Docker/Compose/Buildx, Git, Bash, tar und **Python 3.13 mit venv/ensurepip** müssen
vorhanden sein; nichts installiert oder ändert Systemdienste oder Linux-Benutzer.
Auf Debian ist dafür auch das Paket **`python3.13-venv`** erforderlich. Es wurde
auf der CI-VM durch den Betreiber nachinstalliert; der Workflow installiert keine
Systempakete. Der Preflight prüft zusätzlich, dass `python3` auf Python 3.13 zeigt.
Debian 13 liefert Python 3.13, passend zu `requires-python >=3.11`; der Build und
Container-Smoke prüfen zusätzlich das unveränderte Python-3.12-Produktionsimage.
Ein Ubuntu-spezifischer `setup-python`-Download wird auf Debian nicht vorausgesetzt.

Das Projekt hat `pyproject.toml` mit pip/setuptools und dem vorhandenen `dev`-Extra,
Ruff und pytest. Keine Lockdatei, keine Typprüfung und kein Coverage-Plugin vorhanden;
deshalb werden keine neuen Werkzeuge oder Coverage-Schwellen eingeführt.
CPU-PyTorch wird wie im Dockerfile vor `pip install -e '.[dev]'` installiert.
`pip check` prüft die Auflösung; `dependencies.txt` dokumentiert installierte Versionen.
Die vorliegenden Versionsbereiche und beweglichen Docker-Tags erlauben keine
bitgenaue Wiederholung einer alten Auflösung. CI behauptet keine solche Garantie;
eine spätere Lockfile-/Digest-Policy wäre eine eigene Projektentscheidung.

## Caches

Python: genau ein Cache, der pip-Download-/Wheelcache über `actions/cache`.
Er wird unter `.ci-pip-cache` wiederhergestellt; Checkout bereinigt den Workspace
zuvor. Der Archivpfad bleibt stabil, weil er in die interne Cache-Version eingeht.
Schlüssel: `pip-v1-<runner.os>-<runner.arch>-py<volle Python-Version>-<SHA256 über
pyproject.toml und scripts/ci.sh>`.
Keine Restore-Präfixe, keine Virtualenv-, Home-, Credential- oder `.env`-Archive.
Eine neue Virtualenv wird in jedem Lauf erstellt; ein bestehender Pfad wird abgewiesen.
Installation und Tests laufen unabhängig vom Cache-Hit. Miss oder Cache-Service-Ausfall
führt zum normalen Download. Nur dieser optionale Cache-Schritt hat
`continue-on-error`; ein Restorefehler wird zusätzlich explizit als Warnung gemeldet.
Cache-Hit/Miss erscheint im Log und in der Zusammenfassung.

Invalidierung: `pyproject.toml`, die CI-Installationslogik in `scripts/ci.sh`, die
Python-Version oder Schema `pip-v1` ändern.
GitHub begrenzt/entfernt Cache-Einträge nach seiner Repository-Quota und
Nutzungsdauer; alte `pip-v1-`-Einträge können in Actions → Caches gezielt gelöscht
werden. Das ist keine Voraussetzung für einen erfolgreichen Lauf. Cache-Save erfolgt
nach erfolgreichem Job im Action-Post-Step. Der nächste Checkout entfernt den lokalen
Downloadcache; der Runner räumt seine temporäre Virtualenv und Reports auf.
Nach harten Abbrüchen kann manueller Temp-Cleanup nötig sein.

Docker: bewusst **nur der lokale integrierte BuildKit-Cache** des eigenen
Repository-Daemons unter `/home/runner-knowledge-system/.local/share/docker`.
Buildx verwendet `--builder default --pull --load --progress=plain`.
BuildKit-Schlüssel ergeben sich aus Basisimage, Dockerfile-Instruktionen, Argumenten
und kopierten Inhalten. `CACHED` im Buildlog zeigt echte Schichttreffer; kein
erfundener globaler Hit-Wert. Der Build lädt das laufbezogen getaggte Image in
denselben Daemon für Compose-Smoke. Cleanup entfernt den Tag, nicht den Buildcache.
Keine Registry-Pushes, kein GHA-Buildcache, kein Export, kein zusätzlicher Builder
und keine privilegierten Container. Entsprechend gibt es keinen optionalen Export,
der Buildfehler verschleiern könnte. Ein leerer/eviktierter Cache baut normal neu.

BuildKit verwendet die vorhandene Daemon-Garbage-Collection; deren konkrete Limits
werden im Preflight ausgegeben und hier nicht verändert. Regelmäßig als Runner-Benutzer
mit explizitem `DOCKER_HOST` prüfen:

```bash
export DOCKER_HOST=unix:///run/user/1000/docker.sock
unset DOCKER_CONTEXT
docker buildx du --builder default
docker buildx inspect default
```

Die Engine-GC ist kein hier garantiertes hartes Speicherlimit. Bei Platzdruck zuerst
Zuordnung und aktive Läufe prüfen. **Nur wenn dieser Daemon/Builder ausschließlich
diesem Repository gehört**, außerhalb aktiver Builds alte unbenutzte Cache-Einträge
gezielt mit `docker buildx prune --builder default --filter until=168h` entfernen
(interaktive Bestätigung beibehalten). Keine Fremd-Daemons/Builder anfassen und kein
`docker system prune`. Für einen absichtlich kalten Build `--no-cache` verwenden;
das löscht keine fremden Daten. Keine Host-Cronjobs oder neuen Dienste.

## Testdienste, Ergebnisse und Cleanup

`compose.ci.yml` ist eigenständig und liest über `--env-file /dev/null` keine
Produktions-`.env`. PostgreSQL: `pgvector/pgvector:0.8.6-pg17-bookworm`, wie im
Projekt; die Fixtures aktivieren `vector`. Es gibt keine weiteren externen Dienste,
LLM-/Telegram-Aufrufe oder Modell-Downloads in den Tests.

Projektname: `knowledge-v32-review-<repository_id>-<run_id>-<run_attempt>`.
Das Präfix und `codex.task=v32-review` erfüllen die Sicherheitsprüfung des bestehenden
Archive-Tests. Compose erzeugt eigene Container, Netzwerk und Datenvolume; keine
festen Containernamen, externen Volumes oder Produktionsmounts. PostgreSQL veröffentlicht
einen dynamischen Loopback-Port. `compose up --wait --wait-timeout 90` prüft Readiness;
`compose port` ermittelt danach die tatsächliche Adresse.

Host-Tests bekommen `TEST_DATABASE_URL`, `TEST_V311_DATABASE_URL`,
`TEST_V32_DATABASE_URL` und `TEST_V32_CONTAINER`; getrennte Testdatenbanken und
Fixture-Schemas vermeiden Kollisionen. Integration darf keinen Test überspringen.
`fetch-depth: 0` erhält den von Migrationstests gelesenen historischen Commit.
Der Container-Smoke nutzt den internen Dienstnamen `postgres` und eine weitere
frische Datenbank. Kein Docker-Socket wird in Container gemountet. CPU-/RAM-Limits
begrenzen Testcontainer, Threadlimits die Python-Numerik; der integrierte Docker-Build
teilt weiterhin die VM-Ressourcen mit den anderen Repository-Runnern.

pytest liefert getrennte JUnit-XML-Dateien. Builder-Diagnose, Abhängigkeitsliste,
Build-/Smoke- und begrenzte Dienstlogs werden zusammen sieben Tage als Artefakt
aufbewahrt, auch bei Fehlern. Es werden nur synthetische Daten verwendet. Keine
vollständigen Umgebungsvariablen, Backups oder Secrets werden hochgeladen.

Logs werden vor dem `always()`-Cleanup gesammelt. Cleanup verwendet ausschließlich
das aktuelle Compose-Projekt, `down --volumes --remove-orphans` und dessen Image-Tag.
Bei einem normalen Prüffehler sowie einer üblichen manuellen oder durch Concurrency
ausgelösten Cancellation wertet GitHub `always()` erneut aus und gibt dem Runner Zeit
für Cleanup. Das ist keine Garantie: Nach Ablauf des Cancellation-Fensters beendet
GitHub Schritte zwangsweise; auch der 45-Minuten-Job-Timeout, ein erzwungener Abbruch,
Runner-Prozess-/VM-Ausfall oder Netzwerkverlust können Cleanup und Artefakt-Post-Steps
verhindern. Historische Fehler-/Abbruchtests betreffen nur die jeweils geprüften
Szenarien; sie belegen keinen Cleanup bei beliebigem hartem Ausfall.

Nach einem solchen Ereignis aktive Läufe prüfen, den verwaisten Projektnamen über
`docker compose ls --all` identifizieren, `CI_PROJECT`, `CI_IMAGE` und ein temporäres
`CI_WORK_DIR` auf genau diesen Lauf setzen und `bash scripts/ci.sh logs`, danach
`bash scripts/ci.sh cleanup` ausführen.
Projektvolumes nur nach Kontrolle des Labels `com.docker.compose.project` entfernen.
Verwaiste `knowledge-ci.<run>.<attempt>.*`-Tempverzeichnisse erst nach Ausschluss eines
aktiven Laufs gezielt entfernen. Kein pauschales Löschen des Workspaces oder Home.

## Lokale Reproduktion

Im vollständigen Checkout mit Python 3.13 und eigenem rootless Docker:

```bash
export DOCKER_HOST=unix:///run/user/1000/docker.sock
export CI_WORK_DIR=$(mktemp -d /tmp/knowledge-ci.XXXXXX)
run_id=$(date +%s)-$$
export CI_PROJECT=knowledge-v32-review-local-$run_id
export CI_IMAGE=knowledge-system-ci:local-$run_id
trap 'status=$?; bash scripts/ci.sh logs || status=$?; bash scripts/ci.sh cleanup || status=$?; exit "$status"' EXIT
set -e
bash scripts/ci.sh preflight
bash scripts/ci.sh install
bash scripts/ci.sh quality
bash scripts/ci.sh unit
bash scripts/ci.sh services
bash scripts/ci.sh integration
bash scripts/ci.sh build
bash scripts/ci.sh smoke
```

Ergebnisse liegen in `$CI_WORK_DIR/reports`. Mit `actionlint` aus dem Repository-Root
lassen sich Syntax und Expressions prüfen; `.github/actionlint.yaml` deklariert die
Custom-Labels (ohne deren Registrierung zu behaupten). `bash -n scripts/ci.sh` und
`docker compose --env-file /dev/null -f compose.ci.yml config --quiet` ergänzen dies;
für letzteren Befehl muss `CI_IMAGE` gesetzt sein.

## Erste Runner-Abnahme und Diagnose

Die folgenden Einrichtungsschritte beschreiben die historische Erstabnahme von
PR #9, als der Workflow noch nicht auf `main` lag. Sie sind kein aktueller
Aufgabenstatus. Die spätere App-Ausnahme ist oben dokumentiert; die zeitweise
Fehler-/Timeout-/Abbruchprüfung in [PR #10](https://github.com/Hengsto/Knowledge-System/pull/10)
wurde separat geführt und geschlossen. Ihr damaliger Umfang ersetzt keinen aktuellen
Prüfnachweis für einen anderen Commit oder einen harten Runner-/Hostausfall.

Erster Lauf: CI-Dateien auf `ci/self-hosted-rootless` committen, diesen Branch nach
`origin` pushen und als vertrauenswürdiger menschlicher Repository-Mitarbeiter einen
internen PR gegen `main` öffnen. Erst das PR-Ereignis startet den Workflow; der
Arbeitsbranch-Push allein ist kein Trigger. `workflow_dispatch` ist für diese
Erstabnahme ungeeignet: Der Workflow liegt noch nicht auf `main`, und die Admission
erlaubt manuelle Läufe ausschließlich auf `main`. Weder Merge noch Main-Push nötig.

Beide Checks müssen erfolgreich sein. Runner-Labels, Python/venv, Runner-Version,
Docker-Anbindung und Compose `--wait` wurden durch die unten dokumentierte Abnahme
bestätigt. Bei `queued` dennoch zuerst Runner-Verfügbarkeit und Labels prüfen.
Fork-PR-Workflows bleiben extern deaktiviert. Dependabot-/Bot-PRs sowie von Bots
ausgelöste Aktualisierungen interner PRs müssen im Admission-Job scheitern und dürfen
keinen Self-hosted-Job starten. Die Admission-Regel auch nach Action-Updates erhalten.

Im ersten Lauf Ruff, Unit-/Integrationstests (lokaler Referenzstand: 344/38, keine
Skips), Build und Smoke kontrollieren. JUnit-Dateien sind die tatsächliche Evidenz.
Cache-Miss und anschließenden pip-Cache-Save im Post-Step prüfen; einen kalten
Docker-Build nur behaupten, wenn das Buildlog dies zeigt. Vorhandene Cache-Schichten
sind kein Fehler, erlauben aber keinen Nachweis eines vollständig kalten Laufs.
Falls nötig, eine gesonderte Kalt-Abnahme mit neuem pip-Schlüsselschema und einem
einmaligen `--no-cache`-Build vorbereiten, ohne den Daemon global zu bereinigen.

Das Artefakt `ci-results-<run_id>-1` herunterladen und `unit.xml`, `integration.xml`,
`dependencies.txt`, `builder.log`, `build.log`, `smoke.log` und `services.log` öffnen.
Cleanup muss erfolgreich sein. Anschließend auf der CI-VM als Runner-Benutzer mit
dem rootless Socket prüfen, dass `docker ps -a`, `docker volume ls` und
`docker network ls`, jeweils gefiltert nach
`label=com.docker.compose.project=knowledge-v32-review-<repository_id>-<run_id>-1`,
keine Ressourcen dieses Laufs mehr finden. Auch der genaue Image-Tag
`knowledge-system-ci:<repository_id>-<run_id>-1` darf nicht verbleiben.
Die Dienstlogs zeigen absichtlich den Zustand vor dem Cleanup.

Zweiter Lauf: Nach vollständigem Erfolg einschließlich Post-Steps im selben GitHub-Lauf
**Re-run all jobs** wählen, alternativ
`gh run rerun <run_id> --repo Hengsto/Knowledge-System`. Keinen Commit und keine
Abhängigkeits-/Workflow-Änderung dazwischen einführen. Run-ID und Commit bleiben gleich,
Run-Attempt wird 2. pip muss bei erfolgreichem Save/Restore `cache-hit: true` melden;
BuildKit sollte unveränderte Schichten als `CACHED` ausweisen. Basisimage-Updates oder
Cache-Eviction können Treffer verhindern und müssen dann untersucht werden.
Frische Virtualenv, erneute Installation, sämtliche Tests und Smoke müssen trotzdem
laufen. `ci-results-<run_id>-2` ebenfalls herunterladen und prüfen. Cleanup-Nachweis
mit Projekt-/Image-Suffix `-2` wiederholen. Laufzeiten nur aus den echten Läufen ablesen.

Fehler-/Abbruchpfade sind damit noch nicht nachgewiesen: Dafür anschließend einen
gesonderten kontrollierten Fehler-/Abbruchlauf auf dem Arbeitsbranch prüfen,
einschließlich Diagnose-Upload und Cleanup; danach den Fehler wieder entfernen.

Queued: zuerst Labels/Runner-Verfügbarkeit prüfen. Docker-Zugriffsfehler: Socket,
Userdienst und Rootless prüfen, keine Socket-Rechte lockern. Python-Fehler: 3.13 und
venv-Verfügbarkeit kontrollieren. Integrations-Skips: erzeugte Opt-ins/Readiness prüfen.
Historischer Commit fehlt: vollständigen Checkout prüfen. Bei DB-/Buildfehlern zuerst
JUnit, `services.log` und `build.log` ansehen. Bei Platzdruck Cache-Wartung wie oben.

## Verifikation bei der Einrichtung (2026-09-16)

Lokal auf der Entwicklungs-VM erfolgreich: frische Installation mit CPU-PyTorch,
`pip check`, Ruff-Lint und Formatprüfung, 344 Unit-Tests, 38 PostgreSQL-Integrationstests
(jeweils 0 Skips), Compose-Validierung/Healthcheck/dynamischer Port, Docker-Build und
Container-Smoke. Ein zweiter Build nach Entfernen des ersten Teststacks und Image-Tags
verwendete alle sechs ausführbaren/COPY-Arbeitsschichten erneut (`CACHED`).
Testcontainer, Netzwerk, Volume und Lauf-Image wurden projektgebunden entfernt.
Die Tests meldeten eine Starlette/AnyIO-DeprecationWarning. Ein erster Unit-Lauf in
der eingeschränkten Sandbox hing bei lokalen Protokolltests und wurde abgebrochen;
der vollständige Lauf außerhalb dieser Sandbox bestand.

Statisch erfolgreich: actionlint 1.7.12 (Release-Prüfsumme verifiziert), Workflow-Syntax
und Expressions, `bash -n`, `git diff --check`, SHA-Abgleich aller drei Action-Releases.

GitHub-Run 35047809171 bestand am 2026-09-16 zweimal auf Commit
`aedc8ab7bcec3fac9869263569c1521df51090ba`: Admission, frische Installation,
Ruff, 344 Unit-Tests, 38 PostgreSQL-Integrationstests ohne Skips, Build, Smoke,
Diagnoseartefakt, Cleanup und alle Post-Steps. Versuch 1 hatte einen pip-Cache-Miss
und speicherte 324.283.389 Bytes; Versuch 2 hatte einen exakten Hit und verwendete
alle sechs Dockerfile-Arbeitsschichten aus BuildKit erneut. Beide Artefakte wurden
heruntergeladen und geprüft.

Danach bestätigte der Betreiber unabhängig direkt auf der CI-VM: keine verbliebenen
Container oder Volumes, ausschließlich Docker-Standardnetzwerke und keine laufbezogenen
Testimages. Basisimages und 2,05 GB BuildKit-Cache blieben wie vorgesehen erhalten.
Diese VM-Prüfung stammt aus der Betreiberbestätigung und wurde nicht über eine API
oder diese Arbeitsumgebung wiederholt. Fehler-, Timeout- und Abbruchpfade bleiben bis
zu den gesondert geplanten Tests ungeprüft.

## Offizielle Referenzen

Action-SHAs wurden gegen die Commit-API der angegebenen Releases geprüft:
[checkout v7.0.1](https://github.com/actions/checkout/tree/v7.0.1),
[cache v6.1.0](https://github.com/actions/cache/tree/v6.1.0),
[upload-artifact v7.0.1](https://github.com/actions/upload-artifact/tree/v7.0.1).
Cache-/Treiberwahl basiert auf
[Docker-Treiber](https://docs.docker.com/build/builders/drivers/docker/),
[Build-Cache](https://docs.docker.com/build/ci/github-actions/cache/) und
[BuildKit-GC](https://docs.docker.com/build/cache/garbage-collection/).
Siehe außerdem [Self-hosted-Sicherheit](https://docs.github.com/en/actions/reference/security/secure-use).
