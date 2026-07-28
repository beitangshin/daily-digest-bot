import pytest

from daily_digest.sourcesio import add_source, load_sources, remove_source, save_sources
from daily_digest.models import Source


def test_load_missing_file_returns_empty_list(tmp_path):
    assert load_sources(tmp_path / "does-not-exist.yaml") == []


def test_add_then_load_roundtrip(tmp_path):
    path = tmp_path / "sources.yaml"
    add_source(path, name="Example", url="https://example.com/feed", type_="rss", category="测试")
    loaded = load_sources(path)
    assert len(loaded) == 1
    assert loaded[0] == Source(name="Example", url="https://example.com/feed", type="rss", category="测试")


def test_add_duplicate_name_raises(tmp_path):
    path = tmp_path / "sources.yaml"
    add_source(path, name="Example", url="https://example.com/feed")
    with pytest.raises(ValueError):
        add_source(path, name="Example", url="https://example.com/other-feed")


def test_remove_source(tmp_path):
    path = tmp_path / "sources.yaml"
    add_source(path, name="A", url="https://a.example/feed")
    add_source(path, name="B", url="https://b.example/feed")
    remaining = remove_source(path, "A")
    assert [s.name for s in remaining] == ["B"]


def test_remove_missing_source_raises(tmp_path):
    path = tmp_path / "sources.yaml"
    save_sources(path, [])
    with pytest.raises(ValueError):
        remove_source(path, "nope")


def test_invalid_type_rejected(tmp_path):
    path = tmp_path / "sources.yaml"
    with pytest.raises(ValueError):
        add_source(path, name="Bad", url="https://example.com", type_="pdf")
