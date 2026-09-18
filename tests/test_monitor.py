import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.source = monitor.Source(
            language="it",
            url="https://www.arbeit.swiss/it/centro-dinformazione/pubblicazioni",
            directives_heading="Direttive / Prassi LADI / Linee guida",
            audit_heading="Audit Letter",
            end_heading="Rapporti e studi",
        )

    def test_parser_selects_only_requested_sections(self):
        page = """
        <h2><span>Attualità</span></h2>
        <a class="u-base-download-item" href="/ignore.pdf" download="Ignora.pdf">
          <h5 class="u-base-download-item__title">Ignorare</h5>
        </a>
        <h2><span>Direttive / Prassi LADI / Linee guida</span></h2>
        <a class="u-base-download-item" href="/api/media/fileservice/files/2026/09/15/abc.pdf"
           download="Direttiva LADI ILR.pdf">
          <h5 class="u-base-download-item__title">Direttiva LADI ILR</h5>
          <span class="u-base-download-item__created">25.06.2026</span>
        </a>
        <h2><span>Audit Letter</span></h2>
        <a class="u-base-download-item" href="/api/media/fileservice/files/2020/06/02/def.pdf"
           download="Audit Letter.pdf">
          <h5 class="u-base-download-item__title">Audit Letter - Inhaltsverzeichnis</h5>
          <span class="u-base-download-item__created">02.06.2020</span>
        </a>
        <h2><span>Rapporti e studi</span></h2>
        <a class="u-base-download-item" href="/ignore-2.pdf" download="Ignora.pdf">
          <h5 class="u-base-download-item__title">Ignorare anche</h5>
        </a>
        """
        items = monitor.parse_publications(self.source, page)
        self.assertEqual(2, len(items))
        self.assertEqual("Weisungen", items[0].category)
        self.assertEqual("2026-09-15", items[0].link_date)
        self.assertEqual("abc", items[0].pdf_id)
        self.assertEqual("Audit Letter", items[1].category)

    def test_hash_pdf_chunks_validates_and_hashes_pdf(self):
        content = b"%PDF-1.7\nexample"
        digest, size = monitor.hash_pdf_chunks([content[:4], content[4:]], 1000)
        self.assertEqual(hashlib.sha256(content).hexdigest(), digest)
        self.assertEqual(len(content), size)
        with self.assertRaises(monitor.MonitorError):
            monitor.hash_pdf_chunks([b"not a PDF"], 1000)

    def test_rendering_is_deterministic_and_escapes_values(self):
        item = monitor.Publication(
            language="it",
            category="Weisungen",
            title="A < B & C",
            displayed_date="25.06.2026",
            download_name="test.pdf",
            pdf_url="https://www.arbeit.swiss/a.pdf?x=1&y=2",
            link_date="2026-09-15",
            pdf_id="id-1",
            size_bytes=123,
            sha256="a" * 64,
        )
        first = monitor.render_html(
            [item], [self.source], [], baseline_exists=True,
            has_current_changes=False, has_download_package=False
        )
        second = monitor.render_html(
            [item], [self.source], [], baseline_exists=True,
            has_current_changes=False, has_download_package=False
        )
        self.assertEqual(first, second)
        self.assertIn("A &lt; B &amp; C", first)
        self.assertIn("x=1&amp;y=2", first)
        self.assertIn("a" * 64, first)

    def test_validate_rejects_duplicate_url(self):
        base = monitor.Publication(
            language="de",
            category="Weisungen",
            title="Dokument 1",
            displayed_date="",
            download_name="eins.pdf",
            pdf_url="https://www.arbeit.swiss/a.pdf",
            link_date="",
            pdf_id="a",
        )
        duplicate = monitor.replace(base, title="Dokument 2", download_name="zwei.pdf")
        with self.assertRaises(monitor.MonitorError):
            monitor.validate_publications([base, duplicate], {"de"})

    def test_compare_detects_new_changed_and_removed_documents(self):
        unchanged = monitor.Publication(
            language="de",
            category="Weisungen",
            title="Unverändert",
            displayed_date="01.01.2026",
            download_name="unveraendert.pdf",
            pdf_url="https://www.arbeit.swiss/unveraendert.pdf",
            link_date="2026-01-01",
            pdf_id="unveraendert",
            size_bytes=10,
            sha256="a" * 64,
        )
        changed = monitor.replace(
            unchanged,
            title="Geändert",
            download_name="geaendert.pdf",
            pdf_url="https://www.arbeit.swiss/geaendert-neu.pdf",
            pdf_id="geaendert-neu",
            sha256="b" * 64,
        )
        new = monitor.replace(
            unchanged,
            title="Neu",
            download_name="neu.pdf",
            pdf_url="https://www.arbeit.swiss/neu.pdf",
            pdf_id="neu",
            sha256="c" * 64,
        )
        previous = [
            {
                **json.loads(monitor.render_manifest([unchanged]))[0],
            },
            {
                **json.loads(monitor.render_manifest([changed]))[0],
                "pdf_url": "https://www.arbeit.swiss/geaendert-alt.pdf",
                "sha256": "d" * 64,
            },
            {
                **json.loads(monitor.render_manifest([
                    monitor.replace(
                        unchanged,
                        title="Entfernt",
                        download_name="entfernt.pdf",
                        pdf_url="https://www.arbeit.swiss/entfernt.pdf",
                        pdf_id="entfernt",
                    )
                ]))[0],
            },
        ]
        changes = monitor.compare_publications([unchanged, changed, new], previous)
        self.assertEqual({"PDF geändert", "Neu", "Entfernt"}, {item.change_type for item in changes})
        changed_result = next(item for item in changes if item.change_type == "PDF geändert")
        self.assertIn("PDF-Inhalt", changed_result.changed_fields)
        self.assertIn("PDF-Adresse", changed_result.changed_fields)

    def test_rendering_shows_latest_changes_and_package(self):
        item = monitor.Publication(
            language="de",
            category="Weisungen",
            title="Neue Weisung",
            displayed_date="01.01.2026",
            download_name="neu.pdf",
            pdf_url="https://www.arbeit.swiss/neu.pdf",
            link_date="2026-01-01",
            pdf_id="neu",
            size_bytes=10,
            sha256="a" * 64,
        )
        change = monitor.Change(
            change_type="Neu",
            identity=monitor.publication_identity(item),
            language="de",
            category="Weisungen",
            title="Neue Weisung",
            pdf_url=item.pdf_url,
            changed_fields=("Neues Dokument",),
            downloadable=True,
        )
        output = monitor.render_html(
            [item],
            [self.source],
            [change],
            baseline_exists=True,
            has_current_changes=True,
            has_download_package=True,
        )
        self.assertIn("Letzte Änderungen", output)
        self.assertIn("letzte-aenderungen.zip", output)
        self.assertIn("Neue Weisung", output)

    def test_change_package_contains_only_downloadable_current_pdf(self):
        item = monitor.Publication(
            language="de",
            category="Weisungen",
            title="Neue Weisung",
            displayed_date="01.01.2026",
            download_name="Neue Weisung.pdf",
            pdf_url="https://www.arbeit.swiss/neu.pdf",
            link_date="2026-01-01",
            pdf_id="neu",
            size_bytes=15,
            sha256="a" * 64,
        )
        change = monitor.Change(
            change_type="Neu",
            identity=monitor.publication_identity(item),
            language="de",
            category="Weisungen",
            title=item.title,
            pdf_url=item.pdf_url,
            changed_fields=("Neues Dokument",),
            downloadable=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "source.pdf"
            pdf.write_bytes(b"%PDF-1.7\ntest")
            archive_path = root / "changes.zip"
            created = monitor.write_changes_zip(
                archive_path, [change], [item], {item.pdf_url: pdf}
            )
            self.assertTrue(created)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(
                    ["de/Weisungen/Neue Weisung.pdf", "aenderungen.csv"],
                    archive.namelist(),
                )


if __name__ == "__main__":
    unittest.main()
