from uuid import uuid4

from wyzer.events import EventLedger
from wyzer.models import EventKind, EventRecord


def test_ledger_is_bounded() -> None:
    ledger = EventLedger(capacity=2)
    action_id = uuid4()
    for index in range(3):
        ledger.append(
            EventRecord(
                kind=EventKind.REQUEST_RECEIVED,
                action_id=action_id,
                details={"index": index},
            )
        )
    assert len(ledger) == 2
    assert [event.details["index"] for event in ledger.recent()] == [1, 2]


def test_ledger_filters_by_kind() -> None:
    ledger = EventLedger()
    action_id = uuid4()
    ledger.append(EventRecord(kind=EventKind.REQUEST_RECEIVED, action_id=action_id))
    ledger.append(EventRecord(kind=EventKind.TOOL_STARTED, action_id=action_id))
    assert len(ledger.recent(kind=EventKind.TOOL_STARTED)) == 1
