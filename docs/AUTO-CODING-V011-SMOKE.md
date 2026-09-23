# Auto-Coding v0.1.1: Smoke-Test

Dieser Auftrag testet die Verarbeitung eines GitHub-Issues durch Auto-Coding bis
zur Erstellung eines Pull Requests. Automatisches Mergen ist für diesen Test
laut Auftragsbeschreibung global deaktiviert.

Ob die nachgelagerte GitHub-CI erfolgreich ist, muss nach der PR-Erstellung
separat geprüft werden. In diesem Task wurde kein Pull Request erstellt und
keine GitHub-CI ausgeführt oder geprüft; die Ausführungsgrenzen untersagen
PR-Erstellung und Push.

Tatsächlich ausgeführter Befehl:

```bash
python3 -c 'assert 2 + 2 == 4; print("smoke-tool-ok")'
```

Tatsächliches Ergebnis (Exit-Code 0):

```text
smoke-tool-ok
```

Die vorhandene Test-/Lint-Konfiguration wurde gesichtet: pytest und Ruff prüfen
Python-Code; ein Markdown-Linter ist dort nicht konfiguriert. Für diese reine
Dokumentationsänderung wurden keine Python-Anwendungstests ausgeführt.
