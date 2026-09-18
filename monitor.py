#!/usr/bin/env python3
"""Erzeugt eine statische Prüfübersicht für PDFs auf arbeit.swiss."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"
LINK_DATE_PATTERN = re.compile(r"/files/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/")
LANGUAGE_ORDER = {"de": 0, "fr": 1, "it": 2}
CATEGORY_ORDER = {"Weisungen": 0, "Audit Letter": 1}


class MonitorError(RuntimeError):
    """Erwarteter Fehler, der ohne Python-Traceback ausgegeben werden kann."""


@dataclass(frozen=True)
class Source:
    language: str
    url: str
    directives_heading: str
    audit_heading: str
    end_heading: str


@dataclass(frozen=True)
class Publication:
    language: str
    category: str
    title: str
    displayed_date: str
    download_name: str
    pdf_url: str
    link_date: str
    pdf_id: str
    size_bytes: int = 0
    sha256: str = ""


@dataclass(frozen=True)
class Change:
    change_type: str
    identity: str
    language: str
    category: str
    title: str
    pdf_url: str
    changed_fields: tuple[str, ...]
    downloadable: bool


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def has_class(attrs: dict[str, str], class_name: str) -> bool:
    return class_name in attrs.get("class", "").split()


class PublicationsParser(HTMLParser):
    """Liest nur Download-Einträge aus den zwei gewünschten H2-Bereichen."""

    def __init__(self, source: Source) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.publications: list[Publication] = []
        self._category: str | None = None
        self._capture: str | None = None
        self._capture_tag: str | None = None
        self._buffer: list[str] = []
        self._item: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}
        if tag == "h2":
            self._start_capture("heading", tag)
            return

        if tag == "a" and self._category and has_class(attrs, "u-base-download-item"):
            self._item = {
                "href": attrs.get("href", ""),
                "download_name": attrs.get("download", ""),
                "title": "",
                "displayed_date": "",
            }
            return

        if self._item is not None and tag == "h5" and has_class(attrs, "u-base-download-item__title"):
            self._start_capture("title", tag)
        elif self._item is not None and tag == "span" and has_class(attrs, "u-base-download-item__created"):
            self._start_capture("displayed_date", tag)

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture is not None and tag == self._capture_tag:
            value = normalize_text("".join(self._buffer))
            capture = self._capture
            self._capture = None
            self._capture_tag = None
            self._buffer = []
            if capture == "heading":
                self._set_heading(value)
            elif self._item is not None:
                self._item[capture] = value

        if tag == "a" and self._item is not None:
            self._finish_item()

    def _start_capture(self, name: str, tag: str) -> None:
        self._capture = name
        self._capture_tag = tag
        self._buffer = []

    def _set_heading(self, heading: str) -> None:
        if heading == self.source.directives_heading:
            self._category = "Weisungen"
        elif heading == self.source.audit_heading:
            self._category = "Audit Letter"
        else:
            self._category = None

    def _finish_item(self) -> None:
        assert self._item is not None
        item = self._item
        self._item = None
        href = item["href"].strip()
        title = item["title"].strip()
        if not href or not title or self._category is None:
            return
        pdf_url = urllib.parse.urljoin(self.source.url, href)
        link_date, pdf_id = derive_link_details(pdf_url)
        self.publications.append(
            Publication(
                language=self.source.language,
                category=self._category,
                title=title,
                displayed_date=item["displayed_date"].strip(),
                download_name=item["download_name"].strip(),
                pdf_url=pdf_url,
                link_date=link_date,
                pdf_id=pdf_id,
            )
        )


def derive_link_details(url: str) -> tuple[str, str]:
    match = LINK_DATE_PATTERN.search(url)
    link_date = ""
    if match:
        link_date = f"{match.group('year')}-{match.group('month')}-{match.group('day')}"
    pdf_id = Path(urllib.parse.urlparse(url).path).stem
    return link_date, pdf_id


def parse_publications(source: Source, page_html: str) -> list[Publication]:
    parser = PublicationsParser(source)
    parser.feed(page_html)
    parser.close()
    if not parser.publications:
        raise MonitorError(
            f"Auf der Seite {source.url} wurden keine Weisungen oder Audit Letters gefunden."
        )
    return parser.publications


def request_with_retries(
    url: str,
    *,
    user_agent: str,
    timeout_seconds: int,
    max_attempts: int,
    accept: str,
) -> urllib.response.addinfourl:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": accept,
                "Accept-Encoding": "identity",
            },
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout_seconds)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < max_attempts:
                time.sleep(min(2 ** (attempt - 1), 8))
    raise MonitorError(f"Abruf nach {max_attempts} Versuch(en) fehlgeschlagen: {url}: {last_error}")


def fetch_html(
    source: Source,
    *,
    user_agent: str,
    timeout_seconds: int,
    max_attempts: int,
) -> str:
    with request_with_retries(
        source.url,
        user_agent=user_agent,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        accept="text/html,application/xhtml+xml",
    ) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    try:
        return body.decode(charset)
    except UnicodeDecodeError:
        return body.decode("utf-8")


def hash_pdf_chunks(
    chunks: Iterable[bytes], max_bytes: int, destination: BinaryIO | None = None
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    prefix = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        if len(prefix) < 5:
            prefix.extend(chunk[: 5 - len(prefix)])
        size += len(chunk)
        if size > max_bytes:
            raise MonitorError(f"PDF überschreitet die konfigurierte Höchstgröße von {max_bytes} Bytes.")
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    if bytes(prefix) != b"%PDF-":
        raise MonitorError("Die heruntergeladene Datei beginnt nicht mit einer PDF-Signatur.")
    return digest.hexdigest(), size


def response_chunks(response: urllib.response.addinfourl, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    while True:
        chunk = response.read(chunk_size)
        if not chunk:
            return
        yield chunk


def fingerprint_pdf(
    publication: Publication,
    *,
    user_agent: str,
    timeout_seconds: int,
    max_attempts: int,
    max_pdf_bytes: int,
    temporary_dir: Path,
) -> tuple[Publication, Path]:
    temporary_path = temporary_dir / f"{hashlib.sha256(publication.pdf_url.encode('utf-8')).hexdigest()}.pdf"
    try:
        with request_with_retries(
            publication.pdf_url,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            accept="application/pdf,application/octet-stream;q=0.9,*/*;q=0.1",
        ) as response, temporary_path.open("wb") as destination:
            sha256, size_bytes = hash_pdf_chunks(
                response_chunks(response), max_pdf_bytes, destination
            )
        return replace(publication, sha256=sha256, size_bytes=size_bytes), temporary_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def publication_sort_key(item: Publication) -> tuple[Any, ...]:
    return (
        LANGUAGE_ORDER.get(item.language, 99),
        CATEGORY_ORDER.get(item.category, 99),
        item.title.casefold(),
        item.pdf_url,
    )


def validate_publications(publications: list[Publication], expected_languages: set[str]) -> None:
    found_languages = {item.language for item in publications}
    if found_languages != expected_languages:
        raise MonitorError(
            f"Sprachabdeckung unvollständig: erwartet {sorted(expected_languages)}, "
            f"gefunden {sorted(found_languages)}."
        )

    identities: set[tuple[str, str, str]] = set()
    urls: set[str] = set()
    for item in publications:
        identity = (item.language, item.category, item.download_name or item.title)
        if identity in identities:
            raise MonitorError(f"Doppelter Dokumenteintrag: {identity}")
        identities.add(identity)
        if item.pdf_url in urls:
            raise MonitorError(f"Dieselbe PDF-Adresse erscheint mehrfach: {item.pdf_url}")
        urls.add(item.pdf_url)
        parsed = urllib.parse.urlparse(item.pdf_url)
        if parsed.scheme != "https" or parsed.netloc != "www.arbeit.swiss":
            raise MonitorError(f"Unerwartete PDF-Adresse: {item.pdf_url}")


def enrich_publications(
    publications: list[Publication],
    *,
    user_agent: str,
    timeout_seconds: int,
    max_attempts: int,
    max_pdf_bytes: int,
    max_workers: int,
    temporary_dir: Path,
) -> tuple[list[Publication], dict[str, Path]]:
    completed: list[Publication] = []
    downloaded_paths: dict[str, Path] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                fingerprint_pdf,
                item,
                user_agent=user_agent,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                max_pdf_bytes=max_pdf_bytes,
                temporary_dir=temporary_dir,
            ): item
            for item in publications
        }
        for number, future in enumerate(as_completed(futures), start=1):
            original = futures[future]
            try:
                completed_item, temporary_path = future.result()
                completed.append(completed_item)
                downloaded_paths[completed_item.pdf_url] = temporary_path
            except Exception as exc:
                if isinstance(exc, MonitorError):
                    raise MonitorError(f"PDF konnte nicht geprüft werden: {original.title}: {exc}") from exc
                raise MonitorError(f"PDF konnte nicht geprüft werden: {original.title}: {exc}") from exc
            if number % 10 == 0 or number == len(publications):
                print(f"PDF-Prüfung: {number}/{len(publications)}", flush=True)
    return sorted(completed, key=publication_sort_key), downloaded_paths


def publication_identity_values(
    language: str, category: str, download_name: str, title: str
) -> str:
    logical_name = normalize_text(download_name or title).casefold()
    return "\x1f".join((language.casefold(), category.casefold(), logical_name))


def publication_identity(item: Publication) -> str:
    return publication_identity_values(
        item.language, item.category, item.download_name, item.title
    )


def manifest_identity(item: dict[str, Any]) -> str:
    return str(item.get("identity") or publication_identity_values(
        str(item.get("language", "")),
        str(item.get("category", "")),
        str(item.get("download_name", "")),
        str(item.get("title", "")),
    ))


def compare_publications(
    publications: list[Publication], previous_manifest: list[dict[str, Any]]
) -> list[Change]:
    current_by_identity = {publication_identity(item): item for item in publications}
    previous_by_identity = {manifest_identity(item): item for item in previous_manifest}
    changes: list[Change] = []

    field_labels = (
        ("sha256", "PDF-Inhalt"),
        ("pdf_url", "PDF-Adresse"),
        ("title", "Titel"),
        ("displayed_date", "Seitendatum"),
        ("link_date", "Link-Datum"),
        ("download_name", "Downloadname"),
    )

    for identity, current in current_by_identity.items():
        previous = previous_by_identity.get(identity)
        if previous is None:
            changes.append(
                Change(
                    change_type="Neu",
                    identity=identity,
                    language=current.language,
                    category=current.category,
                    title=current.title,
                    pdf_url=current.pdf_url,
                    changed_fields=("Neues Dokument",),
                    downloadable=True,
                )
            )
            continue

        changed_fields = tuple(
            label
            for field, label in field_labels
            if str(previous.get(field, "")) != str(getattr(current, field))
        )
        if changed_fields:
            change_type = "PDF geändert" if "PDF-Inhalt" in changed_fields else "Angaben geändert"
            changes.append(
                Change(
                    change_type=change_type,
                    identity=identity,
                    language=current.language,
                    category=current.category,
                    title=current.title,
                    pdf_url=current.pdf_url,
                    changed_fields=changed_fields,
                    downloadable=True,
                )
            )

    for identity, previous in previous_by_identity.items():
        if identity in current_by_identity:
            continue
        changes.append(
            Change(
                change_type="Entfernt",
                identity=identity,
                language=str(previous.get("language", "")),
                category=str(previous.get("category", "")),
                title=str(previous.get("title", "Unbekanntes Dokument")),
                pdf_url="",
                changed_fields=("Dokument entfernt",),
                downloadable=False,
            )
        )

    return sorted(
        changes,
        key=lambda item: (
            LANGUAGE_ORDER.get(item.language, 99),
            CATEGORY_ORDER.get(item.category, 99),
            item.title.casefold(),
            item.change_type,
        ),
    )


def change_to_dict(change: Change) -> dict[str, Any]:
    return {
        "change_type": change.change_type,
        "identity": change.identity,
        "language": change.language,
        "category": change.category,
        "title": change.title,
        "pdf_url": change.pdf_url,
        "changed_fields": list(change.changed_fields),
        "downloadable": change.downloadable,
    }


def change_from_dict(value: dict[str, Any]) -> Change:
    return Change(
        change_type=str(value.get("change_type", "Änderung")),
        identity=str(value.get("identity", "")),
        language=str(value.get("language", "")),
        category=str(value.get("category", "")),
        title=str(value.get("title", "Unbekanntes Dokument")),
        pdf_url=str(value.get("pdf_url", "")),
        changed_fields=tuple(str(item) for item in value.get("changed_fields", [])),
        downloadable=bool(value.get("downloadable", False)),
    )


def render_html(
    publications: list[Publication],
    sources: list[Source],
    displayed_changes: list[Change],
    *,
    baseline_exists: bool,
    has_current_changes: bool,
    has_download_package: bool,
) -> str:
    rows: list[str] = []
    for item in publications:
        displayed_date = item.displayed_date or "—"
        link_date = item.link_date or "—"
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.language.upper())}</td>"
            f"<td>{html.escape(item.category)}</td>"
            f'<td><a href="{html.escape(item.pdf_url, quote=True)}">{html.escape(item.title)}</a></td>'
            f"<td>{html.escape(displayed_date)}</td>"
            f"<td>{html.escape(link_date)}</td>"
            f"<td><code>{html.escape(item.pdf_id)}</code></td>"
            f"<td class=\"number\">{item.size_bytes:,}</td>"
            f"<td><code>{html.escape(item.sha256)}</code></td>"
            "</tr>"
        )

    source_links = " · ".join(
        f'<a href="{html.escape(source.url, quote=True)}">{html.escape(source.language.upper())}</a>'
        for source in sources
    )

    if not baseline_exists:
        changes_section = (
            "<h2>Letzte Änderungen</h2>"
            "<p>Der Erstbestand wurde aufgenommen. Änderungen werden ab dem nächsten Lauf ausgewiesen.</p>"
        )
    elif not displayed_changes:
        changes_section = (
            "<h2>Letzte Änderungen</h2>"
            "<p>Gegenüber dem gespeicherten Erstbestand wurden noch keine Änderungen festgestellt.</p>"
        )
    else:
        lead = (
            "Beim aktuellen Vergleich wurden folgende Änderungen festgestellt:"
            if has_current_changes
            else "Seit der letzten veröffentlichten Änderung gab es keine weitere Änderung. "
            "Zuletzt festgestellt wurden:"
        )
        change_rows: list[str] = []
        for change in displayed_changes:
            if change.pdf_url:
                title = (
                    f'<a href="{html.escape(change.pdf_url, quote=True)}">'
                    f"{html.escape(change.title)}</a>"
                )
            else:
                title = html.escape(change.title)
            change_rows.append(
                "<tr>"
                f"<td>{html.escape(change.change_type)}</td>"
                f"<td>{html.escape(change.language.upper())}</td>"
                f"<td>{html.escape(change.category)}</td>"
                f"<td>{title}</td>"
                f"<td>{html.escape(', '.join(change.changed_fields))}</td>"
                "</tr>"
            )
        package_link = (
            '<p><strong><a href="letzte-aenderungen.zip">Neue und geänderte PDFs herunterladen</a></strong></p>'
            if has_download_package
            else ""
        )
        changes_section = "".join(
            [
                "<h2>Letzte Änderungen</h2>",
                f"<p>{html.escape(lead)}</p>",
                package_link,
                '<table class="changes"><thead><tr><th>Art</th><th>Sprache</th>'
                "<th>Bereich</th><th>Dokument</th><th>Betroffen</th></tr></thead><tbody>",
                *change_rows,
                "</tbody></table>",
            ]
        )
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="de">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>PDF-Monitor arbeit.swiss</title>",
            "<style>",
            "body{font-family:Arial,sans-serif;margin:20px;color:#111}",
            "h1{font-size:1.45rem;margin:0 0 12px}",
            "p{line-height:1.4;max-width:1100px}",
            "h2{font-size:1.15rem;margin-top:24px}",
            "table{border-collapse:collapse;width:100%;font-size:14px}",
            "th,td{border:1px solid #777;padding:5px;vertical-align:top;text-align:left}",
            "th{background:#f2f2f2}",
            "code{font-size:12px;overflow-wrap:anywhere}",
            ".number{text-align:right;white-space:nowrap}",
            "</style>",
            "</head>",
            "<body>",
            "<main>",
            "<h1>Weisungen und Audit Letters auf arbeit.swiss</h1>",
            f"<p><strong>Einträge:</strong> {len(publications)} · "
            '<a href="arbeit-swiss-pdfs.csv">CSV herunterladen</a></p>',
            "<p>Überwacht werden die PDF-Inhalte, ihre Adressen und die auf arbeit.swiss "
            "angezeigten Datumsangaben. Der SHA-256-Fingerabdruck ändert sich auch dann, "
            "wenn eine PDF unter derselben Adresse still ersetzt wird.</p>",
            f"<p><strong>Quellseiten:</strong> {source_links}</p>",
            changes_section,
            "<h2>Aktueller Gesamtbestand</h2>",
            "<table>",
            "<thead><tr><th>Sprache</th><th>Bereich</th><th>Dokument</th>"
            "<th>Seitendatum</th><th>Link-Datum</th><th>PDF-ID</th>"
            "<th>Bytes</th><th>SHA-256</th></tr></thead>",
            "<tbody>",
            *rows,
            "</tbody>",
            "</table>",
            "</main>",
            "</body>",
            "</html>",
            "",
        ]
    )


def render_csv(publications: list[Publication]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "Sprache",
            "Bereich",
            "Dokument",
            "Seitendatum",
            "Link-Datum",
            "Downloadname",
            "PDF-ID",
            "Bytes",
            "SHA-256",
            "PDF-URL",
        ]
    )
    for item in publications:
        writer.writerow(
            [
                item.language,
                item.category,
                item.title,
                item.displayed_date,
                item.link_date,
                item.download_name,
                item.pdf_id,
                item.size_bytes,
                item.sha256,
                item.pdf_url,
            ]
        )
    return output.getvalue()


def render_manifest(publications: list[Publication]) -> str:
    values = [
        {
            "identity": publication_identity(item),
            "language": item.language,
            "category": item.category,
            "title": item.title,
            "displayed_date": item.displayed_date,
            "link_date": item.link_date,
            "download_name": item.download_name,
            "pdf_id": item.pdf_id,
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
            "pdf_url": item.pdf_url,
        }
        for item in publications
    ]
    return json.dumps(values, ensure_ascii=False, indent=2) + "\n"


def render_changes(changes: list[Change]) -> str:
    return json.dumps(
        [change_to_dict(change) for change in changes], ensure_ascii=False, indent=2
    ) + "\n"


def load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Ungültiges JSON in {path}: {exc}") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise MonitorError(f"In {path} wird eine JSON-Liste mit Objekten erwartet.")
    return value


def safe_archive_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", normalize_text(value)).strip(" .")
    return cleaned or fallback


def write_changes_zip(
    path: Path,
    changes: list[Change],
    publications: list[Publication],
    downloaded_paths: dict[str, Path],
) -> bool:
    current_by_identity = {publication_identity(item): item for item in publications}
    downloadable = [
        (change, current_by_identity.get(change.identity))
        for change in changes
        if change.downloadable and change.identity in current_by_identity
    ]
    downloadable = [(change, item) for change, item in downloadable if item is not None]
    if not downloadable:
        path.unlink(missing_ok=True)
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            used_names: set[str] = set()
            for change, item in downloadable:
                assert item is not None
                filename = safe_archive_name(
                    item.download_name,
                    f"{safe_archive_name(item.title, item.pdf_id)}.pdf",
                )
                if not filename.casefold().endswith(".pdf"):
                    filename += ".pdf"
                category = safe_archive_name(item.category, "Dokumente")
                archive_name = f"{item.language}/{category}/{filename}"
                if archive_name.casefold() in used_names:
                    stem = Path(filename).stem
                    archive_name = f"{item.language}/{category}/{stem}_{item.pdf_id[:8]}.pdf"
                used_names.add(archive_name.casefold())
                archive.write(downloaded_paths[item.pdf_url], archive_name)

            change_csv = io.StringIO(newline="")
            writer = csv.writer(change_csv, lineterminator="\n")
            writer.writerow(["Art", "Sprache", "Bereich", "Dokument", "Betroffen", "PDF-URL"])
            for change in changes:
                writer.writerow(
                    [
                        change.change_type,
                        change.language,
                        change.category,
                        change.title,
                        ", ".join(change.changed_fields),
                        change.pdf_url,
                    ]
                )
            archive.writestr("aenderungen.csv", change_csv.getvalue().encode("utf-8-sig"))
        os.replace(temporary, path)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def load_config(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MonitorError(f"Konfiguration nicht gefunden: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Ungültiges JSON in {path}: {exc}") from exc


def load_sources(config: dict[str, Any]) -> list[Source]:
    sources: list[Source] = []
    for value in config.get("sources", []):
        try:
            sources.append(
                Source(
                    language=value["language"],
                    url=value["url"],
                    directives_heading=value["directives_heading"],
                    audit_heading=value["audit_heading"],
                    end_heading=value["end_heading"],
                )
            )
        except (KeyError, TypeError) as exc:
            raise MonitorError(f"Ungültiger Quelleneintrag in der Konfiguration: {value}") from exc
    if not sources:
        raise MonitorError("In der Konfiguration sind keine Quellseiten enthalten.")
    return sources


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--next-state-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        sources = load_sources(config)
        output_dir = args.output_dir or SCRIPT_DIR / config.get("output_dir", "site")
        state_dir = args.state_dir or SCRIPT_DIR / config.get("state_dir", "state")
        next_state_dir = args.next_state_dir or SCRIPT_DIR / config.get(
            "next_state_dir", "state-next"
        )
        user_agent = str(config.get("user_agent", "lexALV-arbeit-swiss-PDF-monitor/1.0"))
        timeout_seconds = int(config.get("timeout_seconds", 120))
        max_attempts = int(config.get("max_attempts", 4))
        max_workers = int(config.get("max_workers", 3))
        max_pdf_bytes = int(config.get("max_pdf_megabytes", 250)) * 1024 * 1024

        previous_manifest_path = state_dir / "manifest.json"
        baseline_exists = previous_manifest_path.exists()
        previous_manifest = load_json_list(previous_manifest_path)
        previous_changes = [
            change_from_dict(item) for item in load_json_list(state_dir / "last_changes.json")
        ]

        publications: list[Publication] = []
        for source in sources:
            print(f"Quellseite abrufen: {source.language.upper()}", flush=True)
            page_html = fetch_html(
                source,
                user_agent=user_agent,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
            publications.extend(parse_publications(source, page_html))

        validate_publications(publications, {source.language for source in sources})
        with tempfile.TemporaryDirectory(prefix="arbeit-swiss-pdf-monitor-") as temporary_name:
            publications, downloaded_paths = enrich_publications(
                publications,
                user_agent=user_agent,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                max_pdf_bytes=max_pdf_bytes,
                max_workers=max_workers,
                temporary_dir=Path(temporary_name),
            )

            current_changes = (
                compare_publications(publications, previous_manifest)
                if baseline_exists
                else []
            )
            displayed_changes = current_changes or previous_changes
            has_download_package = write_changes_zip(
                output_dir / "letzte-aenderungen.zip",
                displayed_changes,
                publications,
                downloaded_paths,
            )

            documents = {
                output_dir / "index.html": render_html(
                    publications,
                    sources,
                    displayed_changes,
                    baseline_exists=baseline_exists,
                    has_current_changes=bool(current_changes),
                    has_download_package=has_download_package,
                ),
                output_dir / "arbeit-swiss-pdfs.csv": render_csv(publications),
                output_dir / "manifest.json": render_manifest(publications),
                output_dir / "changes.json": render_changes(displayed_changes),
                next_state_dir / "manifest.json": render_manifest(publications),
                next_state_dir / "last_changes.json": render_changes(displayed_changes),
                next_state_dir / "workflow-output.txt": (
                    f"baseline_exists={'true' if baseline_exists else 'false'}\n"
                    f"has_changes={'true' if current_changes else 'false'}\n"
                ),
            }
            for path, content in documents.items():
                atomic_write_text(path, content)

        total_bytes = sum(item.size_bytes for item in publications)
        print(
            f"{len(publications)} PDFs geprüft ({total_bytes:,} Bytes); "
            f"{len(current_changes)} aktuelle Änderung(en); "
            f"Ausgabe nach {output_dir} geschrieben.",
            flush=True,
        )
        return 0
    except MonitorError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
