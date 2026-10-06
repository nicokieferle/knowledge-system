"""Independent local client: MCP reads and explicit proposal submission/status only."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import warnings
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

READ_TOKEN = "MCP_HTTP_BEARER_TOKEN"
SUBMIT_TOKEN = "PROPOSAL_INGRESS_BEARER_TOKEN"
TIMEOUT = 60


class ClientError(Exception):
    """Only fixed, public messages may cross the terminal boundary."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Fehler: Argumente ungültig; --help zeigt die erlaubten Befehle.\n")


def canonical_uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise ClientError("Eine kanonische UUID mit kleinen Buchstaben ist erforderlich.") from None
    return value


def local_url(value, *, ingress=False):
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "http"
            and url.hostname in ("127.0.0.1", "::1")
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
            and "?" not in value
            and "#" not in value
            and url.port is not None
            and (url.path in ("", "/") if ingress else url.path.startswith("/"))
            and all(33 <= ord(c) <= 126 for c in value)
            and "%" not in value
        )
    except ValueError:
        valid = False
    if not valid:
        raise ClientError("URL ungültig: lokales HTTP mit Loopback-IP und Port erforderlich.")
    return value.rstrip("/") if ingress else value


def credential(name):
    value = os.environ.get(name)
    if value is None:
        if not sys.stdin.isatty():
            raise ClientError("Token fehlt: Prozessumgebung setzen oder im Terminal starten.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                value = getpass.getpass(
                    "MCP-Lesetoken: " if name == READ_TOKEN else "Einreichungstoken: "
                )
            except getpass.GetPassWarning:
                raise ClientError(
                    "Verdeckte Tokeneingabe nicht verfügbar; Prozessumgebung nutzen."
                ) from None
    if len(value) < 32 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ClientError("Token ungültig: mindestens 32 druckbare ASCII-Zeichen ohne Leerraum.")
    other = os.environ.get(SUBMIT_TOKEN if name == READ_TOKEN else READ_TOKEN)
    if value == other:
        raise ClientError("MCP und Einreichung benötigen unterschiedliche Tokens.")
    return value


def protect_output(text, token):
    secrets = (token, os.environ.get(READ_TOKEN), os.environ.get(SUBMIT_TOKEN))
    if any(secret and secret in text for secret in secrets):
        raise ClientError("Ausgabe gesperrt: Antwort enthält einen Zugangstoken.")
    if any(ord(c) < 32 and c not in "\r\n\t" or 127 <= ord(c) < 160 for c in text):
        raise ClientError("Ausgabe gesperrt: Antwort enthält Terminal-Steuerzeichen.")
    return text


def protect_values(value, token):
    if isinstance(value, str):
        protect_output(value, token)
    elif isinstance(value, dict):
        for key, item in value.items():
            protect_values(key, token)
            protect_values(item, token)
    elif isinstance(value, list):
        for item in value:
            protect_values(item, token)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ClientError("JSON enthält doppelte Feldnamen.")
        result[key] = value
    return result


def read_utf8(path, limit):
    try:
        with Path(path).open("rb") as file:
            data = file.read(limit + 1)
        if len(data) > limit:
            raise ClientError("Eingabedatei überschreitet das Vertragslimit.")
        return data.decode("utf-8")
    except (OSError, UnicodeError):
        raise ClientError("Eingabedatei nicht lesbar oder kein gültiges UTF-8.") from None


def submission(markdown, metadata):
    try:
        body = json.loads(read_utf8(metadata, 512_000), object_pairs_hook=unique_object)
    except ValueError:
        raise ClientError("Metadaten sind kein gültiges JSON.") from None
    allowed = {"summary", "reason", "title", "target_source_path", "provenance"}
    required = {"summary", "reason", "provenance"}
    if not isinstance(body, dict) or not required <= body.keys() or body.keys() - allowed:
        raise ClientError("Metadatenfelder ungültig; siehe Beispiel und Eingangsvertrag.")
    body["proposed_content"] = read_utf8(markdown, 128_000)
    try:
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError):
        raise ClientError("Metadaten enthalten ungültige Werte.") from None
    if len(encoded) > 512_000:
        raise ClientError("Gesamte Einreichung überschreitet 512.000 Bytes.")
    return encoded


def http_failure(status):
    messages = {
        400: "Anfrage abgewiesen: URL, Host oder Einreichungsschlüssel prüfen.",
        401: "Authentifizierung fehlgeschlagen: passenden Token prüfen.",
        404: "Einreichung nicht verfügbar: ID und Maschinen-/Owner-Zuordnung prüfen.",
        409: "Konflikt: Schlüssel bereits anders verwendet oder Owner-Zuordnung geändert.",
        413: "Einreichung zu groß für den Eingangsvertrag.",
        415: "Eingang akzeptiert das Datenformat nicht.",
        422: "Entwurf oder Metadaten ungültig; Limits, Provenienz und Zielpfad prüfen.",
        503: "Dienst vorübergehend nicht verfügbar; später erneut versuchen.",
    }
    return messages.get(status, "Unerwartete HTTP-Antwort; Dienst und lokalen Endpunkt prüfen.")


def public_failure(error):
    # Async transport cleanup can wrap even our own safe errors in exception groups.
    if isinstance(error, ClientError):
        return str(error)
    if isinstance(error, httpx2.HTTPStatusError):
        return http_failure(error.response.status_code)
    if isinstance(error, TimeoutError):
        return "Zeitlimit erreicht; lokalen Dienst prüfen."
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            message = public_failure(nested)
            if message:
                return message
    return None


async def mcp_read(url, token, tool, arguments):
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + token},
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        ) as http,
        Client(streamable_http_client(url, http_client=http), mode="legacy") as client,
    ):
        result = await client.call_tool(tool, arguments)
        if result.is_error:
            raise ClientError("MCP-Anfrage fehlgeschlagen; Suchindex oder Quellreferenz prüfen.")
        body = result.structured_content
        if not isinstance(body, dict):
            raise ClientError("MCP-Antwort entspricht nicht dem dokumentierten Vertrag.")
        protect_values(body, token)
        return body


async def ingress_request(url, token, *, body=None, key=None):
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        headers.update({"Idempotency-Key": key, "Content-Type": "application/json"})
    async with httpx2.AsyncClient(timeout=TIMEOUT, follow_redirects=False, trust_env=False) as http:
        response = await http.request(
            "POST" if body is not None else "GET", url, headers=headers, content=body
        )
    if response.status_code not in ((200, 201) if body is not None else (200,)):
        raise ClientError(http_failure(response.status_code))
    try:
        result = response.json()
    except ValueError:
        raise ClientError("Antwort ist kein gültiges JSON.") from None
    if not isinstance(result, dict):
        raise ClientError("Antwort entspricht nicht dem dokumentierten Vertrag.")
    return result, response.status_code


def status_text(body, pid):
    proposal = {
        "pending": "Wartet auf menschliches Review",
        "deferred": "Zurückgestellt",
        "rejected": "Abgelehnt",
        "accepted": "Akzeptiert (geprüfte Revision)",
    }
    apply = {
        None: "Kein Apply-Journal",
        "pending": "Ausstehend",
        "applied": "Angewendet",
        "conflict": "Konflikt",
        "failed": "Fehlgeschlagen",
    }
    index = {
        None: "Kein Apply-Journal",
        "pending": "Ausstehend",
        "indexed": "Indexiert",
        "failed": "Fehlgeschlagen",
    }
    if (
        set(body)
        != {"proposal_id", "proposal_status", "accepted_review_id", "apply_status", "index_status"}
        or body["proposal_id"] != pid
        or body["proposal_status"] not in proposal
        or body["apply_status"] not in apply
        or body["index_status"] not in index
        or (body["apply_status"] is None) != (body["index_status"] is None)
    ):
        raise ClientError("Statusantwort entspricht nicht dem dokumentierten Vertrag.")
    revision = body["accepted_review_id"]
    if (body["proposal_status"] == "accepted") != (revision is not None):
        raise ClientError("Statusantwort enthält keine konsistente akzeptierte Revision.")
    if revision is not None:
        canonical_uuid(revision)
    return (
        f"Proposal: {pid}\nReview: {proposal[body['proposal_status']]}\n"
        f"Akzeptierte Revision: {revision or 'Keine'}\n"
        f"Datei-Apply: {apply[body['apply_status']]}\nIndexierung: {index[body['index_status']]}\n"
    )


async def execute(args, token):
    if args.command in ("search", "get"):
        url = local_url(args.mcp_url)
        if args.command == "search":
            body = await mcp_read(
                url,
                token,
                "search_knowledge",
                {
                    "query": args.query,
                    "mode": args.mode,
                    "limit": args.limit,
                },
            )
            if not isinstance(body.get("results"), list):
                raise ClientError("Suchantwort entspricht nicht dem dokumentierten Vertrag.")
            output = (
                json.dumps(body, ensure_ascii=False, indent=2) + "\n"
                if body["results"]
                else "Keine Treffer.\n"
            )
        else:
            body = await mcp_read(
                url,
                token,
                "get_document",
                {
                    "source_id": args.source_id,
                    "source_path": args.source_path,
                },
            )
            if (
                body.get("source_id") != args.source_id
                or body.get("source_path") != args.source_path
                or not isinstance(body.get("content"), str)
            ):
                raise ClientError("Dokumentantwort entspricht nicht der angefragten Quelle.")
            output = body["content"]
    else:
        url = local_url(args.ingress_url, ingress=True)
        if args.command == "submit":
            key = canonical_uuid(args.idempotency_key)
            encoded = submission(args.markdown, args.metadata)
            # Never persist a credential inadvertently included in a draft or metadata.
            protect_values(json.loads(encoded), token)
            body, code = await ingress_request(url + "/v1/proposals", token, body=encoded, key=key)
            if (
                set(body) != {"proposal_id", "conversation_id", "submission_state"}
                or body["submission_state"] != "recorded"
            ):
                raise ClientError("Receipt entspricht nicht dem dokumentierten Vertrag.")
            pid = canonical_uuid(body["proposal_id"])
            canonical_uuid(body["conversation_id"])
            output = f"{'Eingereicht' if code == 201 else 'Bereits eingereicht (Replay)'}.\n"
            output += f"Proposal: {pid}\nAufnahme bestätigt; Review und Apply stehen separat.\n"
        else:
            pid = canonical_uuid(args.proposal_id)
            body, _ = await ingress_request(url + f"/v1/proposals/{pid}/status", token)
            output = status_text(body, pid)
    return protect_output(output, token)


def parser():
    result = SafeParser(
        prog="reference-client", description="Lokales Knowledge-System-Referenzbeispiel"
    )
    commands = result.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="Verwandtes Wissen über MCP suchen")
    search.add_argument("query")
    search.add_argument("--mode", choices=("fast", "quality"), default="fast")
    search.add_argument("--limit", type=int, choices=range(1, 21), default=5)
    get = commands.add_parser("get", help="Original anhand einer Quellreferenz abrufen")
    get.add_argument("source_id")
    get.add_argument("source_path")
    for command in (search, get):
        command.add_argument("--mcp-url", default="http://127.0.0.1:8000/mcp")
    submit = commands.add_parser("submit", help="Fertige Markdown-Datei ausdrücklich einreichen")
    submit.add_argument("markdown")
    submit.add_argument("metadata")
    submit.add_argument("--idempotency-key", required=True)
    status = commands.add_parser("status", help="Eigenen gespeicherten Bearbeitungsstand abfragen")
    status.add_argument("proposal_id")
    for command in (submit, status):
        command.add_argument("--ingress-url", default="http://127.0.0.1:8081")
    return result


def main(argv=None):
    # SDK/network logs and exception representations may contain credentials/content.
    logging.disable(logging.CRITICAL)
    args = parser().parse_args(argv)
    try:
        token = credential(READ_TOKEN if args.command in ("search", "get") else SUBMIT_TOKEN)
        output = asyncio.run(asyncio.wait_for(execute(args, token), timeout=TIMEOUT))
        sys.stdout.buffer.write(output.encode("utf-8"))
        return 0
    except ClientError as error:
        print(f"Fehler: {error}", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("Abgebrochen.", file=sys.stderr)
    except TimeoutError:
        print("Fehler: Zeitlimit erreicht; lokalen Dienst prüfen.", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - never expose SDK/transport exception details
        message = (
            public_failure(error) or "Verbindung oder Antwort ungültig; Endpunkt und Token prüfen."
        )
        print(f"Fehler: {message}", file=sys.stderr)
    if args.command == "submit":
        print(
            "Bei unbekanntem Ausgang: dieselben Dateien mit demselben Schlüssel wiederholen.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
