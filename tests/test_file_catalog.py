from pathlib import Path

import pytest

from wyzer.files import FileCatalog


def test_catalog_searches_metadata_and_safe_text_content(tmp_path: Path) -> None:
    root = tmp_path / "drive"
    root.mkdir()
    document = root / "project-notes.txt"
    document.write_text("The telescope calibration happens on Friday.", encoding="utf-8")
    catalog = FileCatalog(tmp_path / "index.sqlite3")

    stats = catalog.refresh([root])

    assert stats.files == 1
    assert catalog.search("project notes", content=False)[0].path == str(document.resolve())
    assert catalog.search("telescope calibration")[0].name == "project-notes.txt"


def test_quick_refresh_is_bounded_and_does_not_read_content(tmp_path: Path) -> None:
    root = tmp_path / "Documents"
    root.mkdir()
    document = root / "project-notes.txt"
    document.write_text("The telescope calibration happens on Friday.", encoding="utf-8")
    catalog = FileCatalog(tmp_path / "index.sqlite3")

    stats = catalog.quick_refresh([root], timeout_seconds=5)

    assert stats.complete is True
    assert stats.content_files == 0
    assert catalog.search("project notes", content=False)[0].path == str(document.resolve())
    assert catalog.search("telescope calibration") == []


def test_refresh_only_prunes_entries_below_completely_scanned_roots(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "one.txt").write_text("one", encoding="utf-8")
    retained = second / "retain-me.txt"
    retained.write_text("two", encoding="utf-8")
    catalog = FileCatalog(tmp_path / "index.sqlite3")
    catalog.refresh([first, second], include_content=False)

    retained.unlink()
    catalog.refresh([first], include_content=False)

    assert catalog.search("retain-me", content=False)[0].path == str(retained.resolve())


def test_incomplete_quick_refresh_preserves_unobserved_entries(tmp_path: Path) -> None:
    root = tmp_path / "Documents"
    root.mkdir()
    stale = root / "keep-until-complete.txt"
    stale.write_text("old", encoding="utf-8")
    catalog = FileCatalog(tmp_path / "index.sqlite3")
    catalog.refresh([root], include_content=False)
    stale.unlink()

    stats = catalog.quick_refresh([root], timeout_seconds=0)

    assert stats.complete is False
    assert catalog.search("keep-until-complete", content=False)


def test_catalog_recovers_from_approximate_compound_project_query(tmp_path: Path) -> None:
    root = tmp_path / "drive"
    project = root / "PriusSolarController"
    project.mkdir(parents=True)
    source = project / "main.cpp"
    source.write_text("int main() {}", encoding="utf-8")
    catalog = FileCatalog(tmp_path / "index.sqlite3")
    catalog.refresh([root], include_content=False)

    matches = catalog.search("priussolarcharger project", content=False)

    assert matches
    assert "PriusSolarController" in matches[0].path


def test_catalog_excludes_credentials_and_system_directories(tmp_path: Path) -> None:
    root = tmp_path / "drive"
    (root / ".ssh").mkdir(parents=True)
    (root / ".ssh/id_rsa").write_text("secret", encoding="utf-8")
    (root / "Windows").mkdir()
    (root / "Windows/system.txt").write_text("system", encoding="utf-8")
    catalog = FileCatalog(tmp_path / "index.sqlite3")

    stats = catalog.refresh([root])

    assert stats.files == 0
    assert catalog.search("secret") == []


def test_catalog_excludes_codex_internal_and_temporary_trees(tmp_path: Path) -> None:
    root = tmp_path / "drive"
    internal = root / ".codex" / ".tmp" / "plugins"
    internal.mkdir(parents=True)
    (internal / "solar-project.md").write_text("solar project", encoding="utf-8")
    catalog = FileCatalog(tmp_path / "index.sqlite3")

    catalog.refresh([root])

    assert catalog.search("solar project") == []


def test_catalog_ranks_named_documents_project_above_incidental_content(tmp_path: Path) -> None:
    root = tmp_path / "drive"
    project = root / "Users" / "me" / "Documents" / "PriusSolarController"
    project.mkdir(parents=True)
    (project / "platformio.ini").write_text("; solar controller project", encoding="utf-8")
    decoy = root / "tools" / "skills"
    decoy.mkdir(parents=True)
    (decoy / "notes.md").write_text("solar project instructions", encoding="utf-8")
    catalog = FileCatalog(tmp_path / "index.sqlite3")
    catalog.refresh([root])

    matches = catalog.search("solar project")

    assert matches
    assert "PriusSolarController" in matches[0].path


def test_bounded_reader_rejects_sensitive_files(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=secret", encoding="utf-8")
    catalog = FileCatalog(tmp_path / "index.sqlite3")
    with pytest.raises(PermissionError):
        catalog.read_text(secret)
