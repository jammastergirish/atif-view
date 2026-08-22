"""The library holds what you decided about a transcript. Losing it silently is
the worst failure here, so these lean on durability and on never destroying an
annotation by accident."""

import json

import pytest

from atif_view import library


@pytest.fixture
def store(tmp_path):
    return tmp_path / "library.json"


def test_absent_library_reads_as_empty(store):
    assert library.load(store) == {}
    assert library.get("nothing", store)["title"] == ""


def test_corrupt_library_reads_as_empty_rather_than_raising(store):
    """A damaged byte should not stop the viewer from starting."""
    store.write_text("{ this is not json")
    assert library.load(store) == {}


def test_library_of_the_wrong_shape_reads_as_empty(store):
    store.write_text(json.dumps({"entries": "not a dict"}))
    assert library.load(store) == {}


def test_update_then_load_round_trips(store):
    library.update("k1", store, title="SOC2 web app", collection="Redwood/SOC2")
    record = library.get("k1", store)
    assert record["title"] == "SOC2 web app"
    assert record["collection"] == "Redwood/SOC2"
    assert record["tags"] == []


def test_update_merges_rather_than_replaces(store):
    library.update("k1", store, title="First", tags=["security"])
    library.update("k1", store, collection="Redwood")
    record = library.get("k1", store)
    assert record["title"] == "First"  # untouched by the second write
    assert record["tags"] == ["security"]
    assert record["collection"] == "Redwood"


def test_unknown_fields_are_ignored(store):
    library.update("k1", store, title="Keep", nonsense="drop me")
    assert "nonsense" not in library.get("k1", store)


def test_tags_are_normalised_and_deduped(store):
    library.update("k1", store, tags=["Security", " security ", "", "Needs-Review", 7])
    assert library.get("k1", store)["tags"] == ["security", "needs-review"]


def test_collection_segments_are_cleaned(store):
    library.update("k1", store, collection=" /Redwood//  SOC2 / ")
    assert library.get("k1", store)["collection"] == "Redwood/SOC2"


def test_clearing_every_field_forgets_the_entry(store):
    library.update("k1", store, title="Temporary")
    library.update("k1", store, title="")
    assert library.load(store) == {}


def test_remove_reports_whether_there_was_anything(store):
    library.update("k1", store, title="Here")
    assert library.remove("k1", store) is True
    assert library.remove("k1", store) is False


def test_update_requires_a_key(store):
    with pytest.raises(ValueError):
        library.update("", store, title="orphan")


def test_collections_include_implied_parents(store):
    """A tree with a hole in it is worse than no tree."""
    library.update("k1", store, collection="Redwood/SOC2/Evidence")
    assert library.collections(store) == [
        "Redwood",
        "Redwood/SOC2",
        "Redwood/SOC2/Evidence",
    ]


def test_tags_are_counted_most_used_first(store):
    library.update("k1", store, tags=["security", "shipped"])
    library.update("k2", store, tags=["security"])
    assert library.tags(store) == [("security", 2), ("shipped", 1)]


def test_save_is_atomic_and_leaves_no_temp_files(store):
    library.update("k1", store, title="One")
    library.update("k2", store, title="Two")
    assert store.is_file()
    assert [p.name for p in store.parent.iterdir()] == [store.name]


def test_a_failed_write_leaves_the_previous_version_intact(store, monkeypatch):
    """Writing in place would truncate; the replace either lands or does not."""
    library.update("k1", store, title="Original")
    before = store.read_text()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(library.store.os, "replace", boom)
    with pytest.raises(OSError):
        library.update("k1", store, title="Doomed")

    assert store.read_text() == before
    assert [p.name for p in store.parent.iterdir()] == [store.name]


def test_decorate_folds_annotations_into_index_rows(store):
    library.update("k1", store, title="Named", tags=["security"])
    rows = [{"key": "k1", "path": "/a"}, {"key": "k2", "path": "/b"}]
    decorated = library.decorate(rows, store)
    assert decorated[0]["title"] == "Named"
    assert decorated[0]["tags"] == ["security"]
    # An unannotated row still gets defaults, so the client branches on nothing.
    assert decorated[1]["title"] == "" and decorated[1]["starred"] is False


def test_the_real_library_is_never_touched_by_a_redirected_call(tmp_path, monkeypatch):
    """A frozen default argument once let tests write to the user's own store.
    Redirecting the module path must actually redirect."""
    redirected = tmp_path / "elsewhere.json"
    monkeypatch.setattr(library, "LIBRARY_PATH", redirected)

    library.update("k1", title="Only here")

    assert redirected.is_file()
    assert library.get("k1")["title"] == "Only here"


def test_starred_steps_round_trip(store):
    library.update("k1", store, starred_steps=["3", "12", "sub-1"])
    assert library.get("k1", store)["starred_steps"] == ["3", "12", "sub-1"]


def test_starred_steps_are_deduped_and_stringified(store):
    library.update("k1", store, starred_steps=[3, "3", 12, "", "  ", 12])
    assert library.get("k1", store)["starred_steps"] == ["3", "12"]


def test_starring_only_steps_is_enough_to_keep_the_entry(store):
    """A transcript with a starred step but no name must not be forgotten."""
    library.update("k1", store, starred_steps=["7"])
    assert library.load(store)["k1"]["starred_steps"] == ["7"]


def test_unstarring_every_step_forgets_an_otherwise_empty_entry(store):
    library.update("k1", store, starred_steps=["7"])
    library.update("k1", store, starred_steps=[])
    assert library.load(store) == {}
