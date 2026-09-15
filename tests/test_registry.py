from standup.sources import SOURCES


def test_registry_order_and_unique_names() -> None:
    names = [s.name for s in SOURCES]
    assert names == ["reminders", "github", "ado", "linear"]
    assert len(set(names)) == len(names)
