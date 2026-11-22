"""Tests for SQLite store state, tallies, tray, and missed_unsubscribes."""

from pathlib import Path

from nollpost.store import TriageStore


def test_store_tallies_and_sender_memory(tmp_path: Path):
    store = TriageStore(tmp_path / "state.db")
    assert store.get_stats()["total_unsubscribed"] == 0

    first = store.record_unsubscribed("news@coop.se", "Coop", "coop.se")
    assert first is True
    second = store.record_unsubscribed("news@coop.se", "Coop", "coop.se")
    assert second is False

    stats = store.get_stats()
    assert stats["total_unsubscribed"] == 2
    assert stats["unique_senders_unsubscribed"] == 1

    store.record_sender_preference("hello@fav.se", "Fav", "archive", keep_subscribed=True)
    mem = store.get_sender_memory("hello@fav.se")
    assert mem is not None
    assert mem.keep_subscribed is True
    assert mem.preferred_action == "archive"

    # Explicit keep preference after prior unsub clears unsubscribed_at so it won't shadow the preference
    store.record_sender_preference("news@coop.se", "Coop", "mark_read", keep_subscribed=True)
    coop_mem = store.get_sender_memory("news@coop.se")
    assert coop_mem is not None
    assert coop_mem.keep_subscribed is True
    assert coop_mem.preferred_action == "mark_read"
    assert coop_mem.unsubscribed_at is None


def test_opened_tray_and_missed_dedup(tmp_path: Path):
    store = TriageStore(tmp_path / "state.db")
    store.add_opened_tray_item(
        message_uid="iCloud:INBOX:55552",
        account="iCloud",
        mailbox="INBOX",
        internal_id="55552",
        rfc_message_id="inv-55552",
        subject="Faktura 5819",
        sender="YANIVA",
        opened_target="/tmp/faktura.pdf",
    )
    assert len(store.list_active_tray()) == 1
    store.mark_tray_swept(["iCloud:INBOX:55552"])
    assert len(store.list_active_tray()) == 0

    id1 = store.enqueue_missed_unsubscribe(
        account="iCloud",
        mailbox="INBOX",
        internal_id="1",
        rfc_message_id="rfc-1",
        sender="A <a@x.se>",
        sender_domain="x.se",
        subject="Sub",
        stage="extract",
        failure_reason="missing",
        candidate_links=[{"href": "https://x.se", "text": "Home"}],
        headers_snapshot="From: a@x.se",
        footer_html_snapshot="<p>footer</p>",
    )
    id2 = store.enqueue_missed_unsubscribe(
        account="iCloud",
        mailbox="INBOX",
        internal_id="1",
        rfc_message_id="rfc-1",
        sender="A <a@x.se>",
        sender_domain="x.se",
        subject="Sub",
        stage="playwright",
        failure_reason="button missing",
        candidate_links=[],
        headers_snapshot="From: a@x.se",
        footer_html_snapshot="<p>footer</p>",
    )
    assert id1 == id2
    missed = store.list_missed_unsubscribes()
    assert len(missed) == 1
    assert missed[0]["stage"] == "playwright"
    store.resolve_missed_unsubscribe(id1, "fixed")
    assert len(store.list_missed_unsubscribes()) == 0
