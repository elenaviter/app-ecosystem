"""W967: one retirement publication per host and required sender, whichever reporter reconciles it.

The publication id names the retired worker and the member proofs, never the reporter, so every
reporter on a host computes the same id. The board admits a routed (control) original from any
attending worker, and a host-local original only from its author (D7). So a control batch has one
row per host, written by whichever reporter reaches it first, and a host-local batch is written only
by its author's own reporter.
"""

from __future__ import annotations

from types import SimpleNamespace

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

