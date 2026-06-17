from pathlib import Path

from eclipse.ingest.registry import Registry, hash_file


def test_hash_file_stable_and_content_addressed(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    a.write_bytes(b"hello")
    b = tmp_path / "b.bin"
    b.write_bytes(b"hello")
    c = tmp_path / "c.bin"
    c.write_bytes(b"different")
    assert hash_file(a) == hash_file(b)
    assert hash_file(a) != hash_file(c)


def test_registry_record_and_dedupe(tmp_path: Path) -> None:
    with Registry(tmp_path / "r.sqlite") as reg:
        assert not reg.is_processed("h1")
        reg.record("h1", "rec.m4a", "vault/acme/x.md")
        assert reg.is_processed("h1")
        assert reg.count() == 1
        entry = reg.get("h1")
        assert entry is not None
        assert entry.source_name == "rec.m4a"


def test_error_status_does_not_count_as_processed(tmp_path: Path) -> None:
    with Registry(tmp_path / "r.sqlite") as reg:
        reg.record("h2", "bad.m4a", None, status="error")
        assert not reg.is_processed("h2")
        assert reg.count() == 0
