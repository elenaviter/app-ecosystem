"""W967: one retirement publication per host and required sender, whichever reporter reconciles it.

The publication id names the retired worker and the member proofs, never the reporter, so every
reporter on a host computes the same id. The board admits a routed (control) original from any
attending worker, and a host-local original only from its author (D7). So a control batch has one
row per host, written by whichever reporter reaches it first, and a host-local batch is written only
by its author's own reporter.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from project_board.client import local_store
from project_board.client.mail_delivery import retirement_delivery_publication
from project_board.client.outbox_store import OutboxStore

PROJECT = 'work:project:alpha'


def _field(tmp_path):
    field = SimpleNamespace(control=tmp_path / 'control')
    field._outbox = OutboxStore(field.control)
    return field


def _control_original(ref='work:mail:routed'):
    return {'message_ref': ref, 'recipient': 'codex-retired', 'kind': 'question', 'subject': 'Private',
            'body': 'Retained original body', 'sender': 'codex-author',
            'payload': {'command_ref': 'work:control:' + ref[-6:], 'source_message_ref': ref,
                        'retirement_command': {'mail': {'subject': 'Private'}}}}


def _local_original(author, ref):
    return {'message_ref': ref, 'recipient': 'codex-retired', 'kind': 'question', 'subject': 'Private',
            'body': 'Retained original body', 'sender': author,
            'sender_identity': {'kind': 'worker', 'worker_name': author}}


def _publish(field, reporter, message, extra=()):
    return retirement_delivery_publication(field, project_ref=PROJECT, reporter_worker_name=reporter,
                                           retired_worker_name='codex-retired', message=message,
                                           additional_messages=list(extra))


def _bind(message, result):
    # As reconcile_project_mailboxes stores it on the pending original.
    message['retirement_publication'] = {'outbox_id': result['outbox_id'], 'proof_hash': result['proof_hash'],
                                         'publication_worker_name': result['publication_worker_name']}


def _folders(field, outbox_id=None):
    return sorted(path.parent.parent.name for path in field._outbox.in_flight('pending')
                  if outbox_id is None or path.stem == outbox_id)


def test_a_control_batch_has_one_row_per_host_whichever_reporter_comes_next(tmp_path):
    """Acceptance [2]: two reporters on one host; the outbox id is in one folder only."""
    field = _field(tmp_path)
    original = _control_original()
    first = _publish(field, 'codex-reporter-a', original)
    assert first['delivery_status'] == 'pending' and first['publication_worker_name'] == 'codex-reporter-a'
    _bind(original, first)
    second = _publish(field, 'codex-reporter-b', original)
    assert second['outbox_id'] == first['outbox_id']
    assert second['publication_worker_name'] == 'codex-reporter-a'
    assert _folders(field, first['outbox_id']) == ['codex-reporter-a'], "one row per host, never a copy per reporter"


def test_an_unbound_control_original_reaches_the_row_another_reporter_wrote(tmp_path):
    field = _field(tmp_path)
    first = _publish(field, 'codex-reporter-a', _control_original())
    again = _publish(field, 'codex-reporter-b', _control_original())
    assert again['outbox_id'] == first['outbox_id'] and _folders(field) == ['codex-reporter-a']


def test_a_host_local_original_is_written_once_and_only_by_its_author(tmp_path):
    """The host-local equivalent of acceptance [2]: the author writes once, no other reporter writes a copy."""
    field = _field(tmp_path)
    original = _local_original('codex-author', 'work:mail:local')
    other = _publish(field, 'codex-reporter-c', original)
    assert other['delivery_status'] == 'pending' and other['reason'] == 'canonical_author_publishes'
    assert 'outbox_id' not in other and _folders(field) == []
    own = _publish(field, 'codex-author', original)
    _bind(original, own)
    assert own['publication_worker_name'] == 'codex-author'
    assert _publish(field, 'codex-reporter-c', original)['reason'] == 'canonical_author_publishes'
    assert _folders(field) == ['codex-author']


def test_the_author_is_matched_exactly_as_the_board_matches_it(tmp_path):
    """N1: the board compares sender_worker_name with the caller's worker name exactly, so does the client."""
    field = _field(tmp_path)
    result = _publish(field, 'codex-author', _local_original('Codex-Author', 'work:mail:case'))
    assert result['reason'] == 'canonical_author_publishes' and _folders(field) == []


def test_a_batch_holds_one_required_sender(tmp_path):
    """Two authors' host-local originals and a control original: each sender's batch is its own row."""
    field = _field(tmp_path)
    a, b = _local_original('codex-author-a', 'work:mail:a'), _local_original('codex-author-b', 'work:mail:b')
    routed = _control_original()
    control = _publish(field, 'codex-reporter-c', routed, [a, b])
    assert [binding['source_message_ref'] for binding in control['publication_bindings']] == ['work:mail:routed']
    own_a = _publish(field, 'codex-author-a', a, [b, routed])
    own_b = _publish(field, 'codex-author-b', b, [a, routed])
    assert [entry['source_message_ref'] for entry in own_a['publication_bindings']] == ['work:mail:a']
    assert [entry['source_message_ref'] for entry in own_b['publication_bindings']] == ['work:mail:b']
    assert len({control['outbox_id'], own_a['outbox_id'], own_b['outbox_id']}) == 3
    assert _folders(field) == ['codex-author-a', 'codex-author-b', 'codex-reporter-c']


def test_a_not_found_lookup_reads_one_agents_history_not_every_agents(tmp_path, monkeypatch):
    """D6: the settled history is read only in the publisher's folder (and the project's), never all nine."""
    field = _field(tmp_path)
    for index in range(9):
        _publish(field, f'codex-agent-{index}', _local_original(f'codex-agent-{index}', f'work:mail:{index}'))
    _publish(field, 'codex-reporter-z', _local_original('codex-reporter-z', 'work:mail:own'))  # its folder exists
    searched = []
    original_find = local_store.PartitionedStore.find

    def spy(self, record_id, *, agents=(), **kwargs):
        searched.append(tuple(agents))
        return original_find(self, record_id, agents=agents, **kwargs)

    monkeypatch.setattr(local_store.PartitionedStore, 'find', spy)
    _publish(field, 'codex-reporter-z', _control_original('work:mail:fresh1'))
    assert searched and all(set(agents) <= {'codex-reporter-z', '-'} for agents in searched), searched


def test_reconcile_leaves_another_authors_host_local_original_and_the_author_writes_it_once(tmp_path):
    """Through reconcile_project_mailboxes: B1 leaves A1's original untouched, A1 writes once, A2 adds no copy."""
    from test_w943_mailbox_reconcile_per_owner import A1, A2, B1, GONE_RETIRED, _beat, _mail_for_gone, _team
    from test_w943_mailbox_reconcile_per_owner import field as field_fixture

    field = field_fixture.__wrapped__(tmp_path)  # the W943 two-owner field, built directly
    _mail_for_gone(field)

    def rows():
        return sorted((path.parent.parent.name, path.stem) for path in field._outbox.in_flight('pending')
                      if path.stem.startswith('outbox_retirement_'))

    def originals():
        return [row for _path, row in field._mail_history().pending(project_id='project-one', family='mail-undeliverable')]

    _beat(field, B1, _team(GONE_RETIRED))
    assert rows() == [], "a reporter other than the author writes no row"
    assert [row.get('failure_notice_state') for row in originals()] == [None]
    _beat(field, A1, _team(GONE_RETIRED))
    [(folder, outbox_id)] = rows()
    assert folder == A1 and outbox_id.startswith('outbox_retirement_')
    [bound] = originals()
    assert bound['retirement_publication']['publication_worker_name'] == A1
    _beat(field, A2, _team(GONE_RETIRED), interval=0)
    _beat(field, B1, _team(GONE_RETIRED), interval=0)
    assert rows() == [(A1, outbox_id)], "no other reporter writes a copy"


# -- the claim runs only when there is something to claim (change 3, D2) -------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

NOW = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)


def _iso(when):
    return when.strftime('%Y-%m-%dT%H:%M:%SZ')


def _row(outbox, worker, folder, outbox_id, **fields):
    row = {'schema': 'problem-board.service-outbox.v1', 'outbox_id': outbox_id, 'kind': 'mail.send',
           'worker_name': worker, 'project_ref': PROJECT, 'state': folder, 'created_at': _iso(NOW),
           'retry_count': 0, 'next_attempt_at': '', **fields}
    path = outbox.in_flight_path(row, folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    from project_board.client.io import atomic_write_json
    atomic_write_json(path, row)


def test_has_in_flight_counts_only_what_the_claim_would_act_on(tmp_path):
    outbox = OutboxStore(tmp_path / 'control')
    me, other = 'codex-me', 'codex-other'
    assert outbox.has_in_flight(worker_name=me, now=NOW) is False
    _row(outbox, me, 'pending', 'outbox_later', next_attempt_at=_iso(NOW + timedelta(seconds=30)))
    assert outbox.has_in_flight(worker_name=me, now=NOW) is False, "a pending row not yet due"
    assert outbox.has_in_flight(worker_name=me, now=NOW + timedelta(seconds=31)) is True, "the same row once due"
    _row(outbox, other, 'leased', 'outbox_live', lease={'relay_id': 'relay-other', 'expires_at': _iso(NOW + timedelta(minutes=5))})
    assert outbox.has_in_flight(worker_name=me, now=NOW) is False, "another relay's live lease"
    # D2: this worker's own live lease is false too. The flush that claimed it sends and settles it
    # (a cancelled turn's settle runs as its own task); pull_outbox never re-sends a live lease.
    _row(outbox, me, 'leased', 'outbox_mine', lease={'relay_id': 'relay-me', 'expires_at': _iso(NOW + timedelta(minutes=5))})
    assert outbox.has_in_flight(worker_name=me, now=NOW) is False, "this worker's own live lease"
    assert outbox.has_in_flight(worker_name=me, now=NOW + timedelta(minutes=6)) is True, "an expired lease is recovered"


def test_has_in_flight_counts_a_due_project_level_row_and_a_lease_without_expiry(tmp_path):
    outbox = OutboxStore(tmp_path / 'control')
    _row(outbox, '', 'pending', 'outbox_project')
    assert outbox.has_in_flight(worker_name='codex-me', now=NOW) is True, "a due project-level row"
    other = OutboxStore(tmp_path / 'other')
    _row(other, 'codex-other', 'leased', 'outbox_nolease', lease={'relay_id': 'relay-other'})
    assert other.has_in_flight(worker_name='codex-me', now=NOW) is True, "a lease without expires_at is expired"


# -- an orphaned routed batch is adopted, never copied (change 2, D1, D3, D7) ----------------------

from project_board.client import mail_delivery  # noqa: E402
from project_board.client.io import read_json  # noqa: E402


def _overdue_row(field, publisher, seconds, *, local=False):
    message = _local_original(publisher, 'work:mail:orphan') if local else _control_original('work:mail:orphan')
    result = _publish(field, publisher, message)
    path = next(path for path in field._outbox.in_flight('pending') if path.stem == result['outbox_id'])
    row = read_json(path)
    row.update(retry_count=4, last_error_code='transport_unavailable',
               next_attempt_at=_iso(datetime.now(timezone.utc) - timedelta(seconds=seconds)))
    field._outbox.write_pending(row)
    return message, result, row


def _in_flight(field):
    return sorted((path.parent.name, path.parent.parent.name, path.stem) for folder in ('pending', 'leased')
                  for path in field._outbox.in_flight(folder) if path.stem.startswith('outbox_retirement_'))


def test_an_overdue_routed_batch_moves_to_the_reporter_with_its_history(tmp_path, caplog):
    field = _field(tmp_path)
    message, first, before = _overdue_row(field, 'codex-reporter-a', mail_delivery.ADOPT_AFTER_SECONDS + 1)
    _bind(message, first)
    with caplog.at_level('INFO', logger='project_board.client.mail_delivery'):
        result = _publish(field, 'codex-reporter-b', message)
    assert result['publication_worker_name'] == 'codex-reporter-b'
    assert _in_flight(field) == [('pending', 'codex-reporter-b', first['outbox_id'])], "moved, never copied"
    row = field._outbox.read(first['outbox_id'], worker_name='codex-reporter-b', project_ref=PROJECT)
    assert (row['adopted_from'], row['retry_count'], row['last_error_code'], row['next_attempt_at']) == (
        'codex-reporter-a', 4, 'transport_unavailable', before['next_attempt_at'])
    assert row['adopted_at'] and row['payload'] == before['payload'] and row['content_hash'] == before['content_hash']
    [record] = [record.getMessage() for record in caplog.records if 'adopted' in record.getMessage()]
    assert 'from=codex-reporter-a to=codex-reporter-b' in record and 'Private' not in record


def test_a_batch_due_for_less_than_the_threshold_stays_with_its_publisher(tmp_path):
    field = _field(tmp_path)
    message, first, _ = _overdue_row(field, 'codex-reporter-a', mail_delivery.ADOPT_AFTER_SECONDS - 60)
    _bind(message, first)
    _publish(field, 'codex-reporter-b', message)
    assert _in_flight(field) == [('pending', 'codex-reporter-a', first['outbox_id'])]


def test_a_host_local_batch_is_never_adopted(tmp_path):
    field = _field(tmp_path)
    message, first, _ = _overdue_row(field, 'codex-author', 10 * mail_delivery.ADOPT_AFTER_SECONDS, local=True)
    _bind(message, first)
    assert _publish(field, 'codex-reporter-b', message)['reason'] == 'canonical_author_publishes'
    assert _in_flight(field) == [('pending', 'codex-author', first['outbox_id'])]


def test_adoption_into_a_retired_reporter_is_refused_and_a_retired_publisher_is_a_valid_source(tmp_path):
    field = _field(tmp_path)
    workers = {'codex-reporter-a': {'pool_status': 'retired', 'retired_at': '2026-10-20T10:00:00Z'},
               'codex-reporter-b': {'pool_status': 'retired', 'retired_at': '2026-10-20T10:00:00Z'},
               'codex-reporter-c': {'pool_status': 'active'}}
    field.read_worker = lambda name: workers[name]
    message, first, _ = _overdue_row(field, 'codex-reporter-a', mail_delivery.ADOPT_AFTER_SECONDS + 1)
    _bind(message, first)
    _publish(field, 'codex-reporter-b', message)
    assert _in_flight(field) == [('pending', 'codex-reporter-a', first['outbox_id'])], "a retired target"
    _publish(field, 'codex-reporter-c', message)
    assert _in_flight(field) == [('pending', 'codex-reporter-c', first['outbox_id'])], "a retired source"


def test_adoption_moves_the_row_under_the_host_outbox_lock(tmp_path, monkeypatch):
    """D1: the same lock every claim, lease recovery, release, retry and settle takes."""
    field = _field(tmp_path)
    message, first, _ = _overdue_row(field, 'codex-reporter-a', mail_delivery.ADOPT_AFTER_SECONDS + 1)
    _bind(message, first)
    held = []
    real_lock, real_move = mail_delivery.exclusive_lock, field._outbox.move_in_flight

    def lock(path, *args, **kwargs):
        context = real_lock(path, *args, **kwargs)

        class Held:
            def __enter__(self):
                context.__enter__()
                held.append(path)

            def __exit__(self, *exc):
                held.remove(path)
                return context.__exit__(*exc)
        return Held()

    def move(*args, **kwargs):
        assert held == [field._outbox.lock], held
        return real_move(*args, **kwargs)

    monkeypatch.setattr(mail_delivery, 'exclusive_lock', lock)
    monkeypatch.setattr(field._outbox, 'move_in_flight', move)
    _publish(field, 'codex-reporter-b', message)
    assert _in_flight(field) == [('pending', 'codex-reporter-b', first['outbox_id'])]


@pytest.mark.parametrize('order', ['claim_first', 'adopt_first'])
def test_an_adoption_racing_a_claim_leaves_exactly_one_row(tmp_path, order):
    """D1 on the real store: leased by its publisher, or pending under the adopter; never both, never neither."""
    from test_w943_mailbox_reconcile_per_owner import A1, B1
    from test_w943_mailbox_reconcile_per_owner import field as field_fixture

    field = field_fixture.__wrapped__(tmp_path)
    message, first, before = _overdue_row(field, A1, mail_delivery.ADOPT_AFTER_SECONDS + 1)
    _bind(message, first)
    if order == 'claim_first':
        claimed = field.pull_outbox(relay_id='relay-a1', worker_name=A1, project_ref=PROJECT)
        _publish(field, B1, message)
        assert [row['outbox_id'] for row in claimed] == [first['outbox_id']]
        assert _in_flight(field) == [('leased', A1, first['outbox_id'])]
    else:
        _publish(field, B1, message)
        assert field.pull_outbox(relay_id='relay-a1', worker_name=A1, project_ref=PROJECT) == []
        [sent] = field.pull_outbox(relay_id='relay-b1', worker_name=B1, project_ref=PROJECT)
        assert _in_flight(field) == [('leased', B1, first['outbox_id'])]
        # D4/D7: the publication B's channel sends is byte for byte the one A would have sent.
        assert sent['payload'] == before['payload'] and sent['content_hash'] == before['content_hash']


def test_the_adoption_rule_itself_never_moves_a_host_local_batch(tmp_path):
    """D7 at the rule: a batch with any host-local member stays in its author's folder, however overdue."""
    field = _field(tmp_path)
    _message, first, row = _overdue_row(field, 'codex-author', 10 * mail_delivery.ADOPT_AFTER_SECONDS, local=True)
    path = next(path for path in field._outbox.in_flight('pending') if path.stem == first['outbox_id'])
    kept = mail_delivery._adopt_orphaned(field, field._outbox, path, row, reporter='codex-reporter-b')
    assert kept['worker_name'] == 'codex-author'
    assert _in_flight(field) == [('pending', 'codex-author', first['outbox_id'])]


def test_a_lease_settled_between_the_listing_and_the_read_is_not_work(tmp_path, monkeypatch):
    outbox = OutboxStore(tmp_path / 'control')
    gone = tmp_path / 'control' / 'projects' / 'alpha' / 'outbox' / 'codex-other' / 'leased' / 'outbox_gone.json'
    real = outbox.in_flight
    monkeypatch.setattr(outbox, 'in_flight', lambda folder, **kw: iter([gone]) if folder == 'leased' else real(folder, **kw))
    assert outbox.has_in_flight(worker_name='codex-me', now=NOW) is False


def test_a_stale_binding_after_an_adoption_resends_the_identical_publication(tmp_path):
    """The documented limit: an original still bound to the publisher A, whose row was adopted and
    settled under B, misses A's history and is written once more. It is the same id, payload and
    content hash, so the board replays the same claim (Applications test_worker_archive_postgres
    ``test_retirement_receipts_real_postgres_identity_replay_rollback_and_jsonb``: reporters cannot
    split it) and the same coverage settles the original.
    """
    field = _field(tmp_path)
    message, first, before = _overdue_row(field, 'codex-reporter-a', mail_delivery.ADOPT_AFTER_SECONDS + 1)
    _bind(message, first)
    # A's row left flight under another folder: adopted and settled under B, out of A's history.
    for path in list(field._outbox.in_flight('pending')):
        path.unlink()
    again = _publish(field, 'codex-reporter-c', message)
    assert again['outbox_id'] == first['outbox_id'] and again['delivery_status'] == 'pending'
    row = field._outbox.read(first['outbox_id'], worker_name='codex-reporter-c', project_ref=PROJECT)
    assert row['payload'] == before['payload'] and row['content_hash'] == before['content_hash']
    coverage = {'source_message_ref': 'work:mail:orphan', 'notice_state': 'queued',
                'receipt_ref': 'work:mail_reconciliation:canonical'}
    row.update(state='sent', remote_result={'schema': 'problem-board.retirement-delivery.v1', 'coverage': [coverage]})
    field._outbox.write_pending(row)
    settled = _publish(field, 'codex-reporter-c', message)
    assert settled['delivery_status'] == 'queued' and settled['receipt_ref'] == coverage['receipt_ref']
