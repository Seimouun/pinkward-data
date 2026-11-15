import pytest

from leaguestats.store import Store


@pytest.fixture
def store():
    store = Store(":memory:")
    yield store
    store.close()
