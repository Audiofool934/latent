import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import pytest

from latent.search_history import SearchHistory


def history(path: Path, *, library="library", model="fixture", dimensions=4):
    return SearchHistory(path, library_id=library, model_id=model, dimensions=dimensions)


def request(store, encode, *, query="forest", image=None, filters=None, order="closest"):
    return store.resolve(query=query, image=image, image_name="reference.jpg" if image else None,
                         filters=filters or {}, order=order, encode=encode)


def test_simultaneous_and_restarted_searches_reuse_one_query_vector(tmp_path):
    store = history(tmp_path / "history.sqlite")
    calls = 0
    lock = Lock()

    def encode():
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.03)
        return [1, 0, 0, 0]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: request(store, encode), range(4)))
    assert calls == 1
    assert sum(not result[2] for result in results) == 1
    assert len(store.list()) == 1

    def offline():
        raise AssertionError("The paid encoder must not be called")

    reopened = history(store.path)
    entry, vector, cached = request(reopened, offline, filters={"rating_min": 4}, order="variety")
    assert cached and vector.tolist() == [1, 0, 0, 0]
    assert entry["filters"] == {"rating_min": 4} and entry["order"] == "variety"
    assert len(reopened.list()) == 1


def test_image_history_preserves_reference_and_refinements_have_distinct_vectors(tmp_path):
    store = history(tmp_path / "history.sqlite")
    image = b"saved preview fixture"
    entry, _, _ = request(store, lambda: [1, 0, 0, 0], query="", image=image)
    refined, _, cached = request(store, lambda: [0, 1, 0, 0], query="snow", image=image)
    assert not cached and entry["id"] != refined["id"]
    reopened = history(store.path)
    assert reopened.image(entry["id"]) == image
    _, vector = reopened.replay(refined["id"], filters={}, order="least_similar")
    assert vector.tolist() == [0, 1, 0, 0]
    reopened.delete(entry["id"])
    with pytest.raises(KeyError, match="No new API request"):
        reopened.replay(entry["id"], filters={}, order="closest")


def test_history_is_scoped_and_incompatible_models_never_replay_silently(tmp_path):
    store = history(tmp_path / "history.sqlite")
    entry, _, _ = request(store, lambda: [1, 0, 0, 0])
    other = history(store.path, library="other")
    assert other.list() == []
    with pytest.raises(KeyError):
        other.replay(entry["id"], filters={}, order="closest")
    changed = history(store.path, model="updated-model")
    assert not changed.list()[0]["reusable"]
    with pytest.raises(ValueError, match="different model"):
        changed.replay(entry["id"], filters={}, order="closest")


@pytest.mark.parametrize("vector", [[1, 0], [0, 0, 0, 0], [float("nan"), 0, 0, 1]])
def test_invalid_query_vectors_are_never_saved(tmp_path, vector):
    store = history(tmp_path / "history.sqlite")
    with pytest.raises(ValueError):
        request(store, lambda: vector)
    assert store.list() == []
