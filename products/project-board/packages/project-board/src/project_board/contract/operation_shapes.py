"""The shape of every canonical Problem Board operation (W404).

One catalog serves every surface: the Named Services schema publishes it, and
``pb coordinate`` prints it and checks a call against it before the call
leaves the machine. For each operation it names the object the operation acts
on (``object_ref``) and the payload fields it reads, each with a one-line
description. ``PROBLEM_BOARD_OPERATION_REQUIRED`` lists the fields the service
refuses a call without, and a field described "(use this or <other>)" is one
of a selector pair, exactly one of which a call carries.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Mapping

from .errors import DomainError
from .plan_nodes import parse_plan_node_ref
from .work_lifecycle import CANONICAL_WORK_STATUSES, canonical_work_status
from .worker_operation_contract import PROBLEM_BOARD_OPERATIONS

# The item fields plan.item.update changes, each with its type, as the
# service's item patch accepts them. The service reads this same table.
# ``review`` is nested: it is an object with its own fields, never a dotted
# name such as ``review.look_at``. Lifecycle status changes through
# work.status.set, review and cancellation, and the fields below in
# PLAN_ITEM_IMMUTABLE_FIELDS through their own operations.
PLAN_ITEM_CHANGE_FIELDS: dict[str, Any] = {
    "title": "string",
    "description": "string",
    "acceptance": "list of strings",
    "tags": "list of strings",
    "keywords": "list of strings",
    "depends_on": "list of canonical plan-node URIs",
    "attachment_refs": "list of attachment refs; a new one must be a staged upload",
    "result": "string",
    "result_ref": "string",
    "blocked_reason": "string",
    "cancel_reason": "string",
    "review": {
        "look_at": "string: the concrete steps the reviewer performs",
        "could_not_verify": "string: what stays unverified; write None when nothing does",
    },
    "review_requirement": {"kind": "qualified | operator"},
    "cancelled_at": "timestamp",
    "cancelled_by": "principal",
}
# Identity, authored position and the assignee: each has its own operation.
PLAN_ITEM_IMMUTABLE_FIELDS: tuple[str, ...] = ("item_id", "item_ref", "item_key", "ordinal", "assignee")

PROBLEM_BOARD_OPERATION_SHAPES: dict[str, dict[str, Any]] = {   'project.register': {   'description': 'Register a logical project ref and its signed-in '
                                           "owner; optionally bind the owner's canonical "
                                           'Git-backed journal home.',
                            'object_ref': 'work:project:<project_id>',
                            'payload': {   'title': 'string',
                                           'status': 'planning|active|paused|completed',
                                           'journal_home_ref': 'repo:<alias>/<relative-path> '
                                                               '(optional)',
                                           'project_artifact_ref': 'repo:<alias>/<relative-path> '
                                                                   '(optional)'}},
    'project.set_journal_home': {   'description': "Create or version-update the signed-in user's "
                                                   'portable journal-home binding. Journal files '
                                                   "and their index remain on the user's local "
                                                   'machines and Git remotes.',
                                    'object_ref': 'work:project:<project_id>',
                                    'payload': {   'journal_home_ref': 'repo:<alias>/<relative-path>',
                                                   'project_artifact_ref': 'repo:<alias>/<relative-path> '
                                                                           '(optional)',
                                                   'expected_revision': 'integer; 0 creates the '
                                                                        'first binding'}},
    'project.set_repositories': {   'description': "Replace the project's repository preset under "
                                                   "its current revision. The journal home's alias "
                                                   'must be present with the journal role.',
                                    'object_ref': 'work:project:<project_id>',
                                    'payload': {   'repositories': '[{alias, url, role: '
                                                                   'work|journal|artifact, '
                                                                   'branch?, path?}]',
                                                   'expected_revision': 'integer; 0 writes the '
                                                                        'first preset'}},
    'project.set_commit_identity': {   'description': 'Set the email every agent of the project '
                                                      "commits with, as '<agent alias> <email>'. "
                                                      'It is part of the repository preset and '
                                                      'advances its revision. Empty clears it.',
                                       'object_ref': 'work:project:<project_id>',
                                       'payload': {   'commit_identity_email': 'one email address '
                                                                               'of the account to '
                                                                               'credit, or empty '
                                                                               'to clear',
                                                      'expected_revision': 'integer; the '
                                                                           "repository preset's "
                                                                           'current revision'}},
    'project.set_files': {   'description': "Set where the project's files live (W370): an ordered "
                                            'list of repository alias and path. Each entry may '
                                            'carry a purpose (instructions, facts, environment; '
                                            'each at most once) and a one-line description. The '
                                            'board keeps the list, never the content.',
                             'object_ref': 'work:project:<project_id>',
                             'payload': {   'files': '[{alias, path, purpose?: '
                                                     'instructions|facts|environment, '
                                                     'description?}]',
                                            'expected_revision': "integer; the file list's current "
                                                                 'revision (files_revision)'}},
    'project.files.edit': {   'description': "Check that this agent may edit the project's files "
                                             'in its repositories. It is refused without the Card '
                                             'operation; the edit itself is a commit in the '
                                             "agent's clone.",
                              'object_ref': 'work:project:<project_id>',
                              'payload': {}},
    'project.github.use': {   'description': 'Check that this agent may use GitHub on the '
                                             "project's repositories (W371), and list the card's "
                                             'GitHub repositories. It is refused without the Card '
                                             'operation; the token itself comes from Connection '
                                             'Hub, which asks the board again for the exact '
                                             'repository.',
                              'object_ref': 'work:project:<project_id>',
                              'payload': {}},
    'project.plan.index': {   'description': 'Open any generation-pinned direct page of compact '
                                             'plan nodes in authored order, with complete-plan '
                                             'dependency facts and facets.',
                              'object_ref': 'work:project:<project_id>',
                              'payload': {   'limit': 'integer 1..200 (optional)',
                                             'page': 'one-based direct page number (optional; '
                                                     'defaults to 1)',
                                             'generation_token': 'page-1 generation fence; '
                                                                 'required for direct pages after '
                                                                 'page 1',
                                             'cursor': 'compatibility locator carrying generation '
                                                       'and next page (optional)',
                                             'status': 'string or string[] (optional)',
                                             'assignee': 'string or string[]; __unassigned selects '
                                                         'unassigned work (optional)',
                                             'depends_on': 'canonical '
                                                           'work:plan:node:<timestamp>:<key>:<semantic-name> '
                                                           'URI or URI[] (optional)',
                                             'item_ref': 'exact canonical '
                                                         'work:plan:node:<timestamp>:<key>:<semantic-name> '
                                                         'URI or URI[] (optional)',
                                             'item_key': 'exact project-scoped item key or key[]; '
                                                         'case-insensitive (optional)',
                                             'query': 'item key, title, or ref text (optional)'}},
    'project.plan.item': {   'description': 'Read one complete authoritative plan item and the '
                                            'files it names by canonical URI or project-scoped key.',
                             'object_ref': 'work:project:<project_id>',
                             'payload': {   'work_ref': 'canonical work:plan:node URI (use this or '
                                                        'item_key)',
                                            'item_key': 'case-insensitive project-scoped item key '
                                                        '(use this or work_ref)'}},
    'work.attachment.link': {
        'description': 'W485: one signed download link for one file attached to one work item, '
                       'bound to the caller; reads never carry links.',
        'object_ref': 'work:project:<project_id>',
        'payload': {
            'work_ref': 'canonical work:plan:node URI (use this or item_key)',
            'item_key': 'case-insensitive project-scoped item key (use this or work_ref)',
            'file_ref': 'a file_ref the item names',
        },
    },
    'project.plan.history': {
        'description': 'Read one generation-pinned newest-first action-history page for one work item.',
        'object_ref': 'work:project:<project_id>',
        'payload': {
            'work_ref': 'canonical work:plan:node URI (use this or item_key)',
            'item_key': 'case-insensitive project-scoped item key (use this or work_ref)',
            'cursor': 'opaque generation-bound continuation cursor (optional)',
            'limit': 'integer 1..50 (optional, default 20)',
            'generation_token': 'explicit history generation fence (optional)',
        },
    },
    'project.plan.resolve': {   'description': 'Resolve a bounded explicit set of current '
                                               'plan-item references and project-scoped keys in '
                                               'one indexed PostgreSQL query, naming every absent '
                                               'selector.',
                                'object_ref': 'work:project:<project_id>',
                                'payload': {   'work_refs': 'array of canonical work:plan:node '
                                                            'URIs (optional)',
                                               'item_keys': 'array of case-insensitive '
                                                            'project-scoped item keys (optional; '
                                                            'combined maximum 100 selectors)'}},
    'project.plan.search': {   'description': 'Page the complete plan ranking selected by status, '
                                              'assignee, creation time, update time, lifecycle, '
                                              'and operator-set lexical, semantic, and recency '
                                              'weights.',
                               'object_ref': 'work:project:<project_id>',
                               'payload': {   'query': 'search text; optional when recency has a '
                                                       'positive weight',
                                              'status': 'string or string[]; several statuses '
                                                        'select their union (optional)',
                                              'assignee': 'string or string[]; __unassigned '
                                                          'selects unassigned work (optional)',
                                              'lifecycle': 'current|finished|cancelled or an '
                                                           'array; current and finished by default',
                                              'created_from': 'inclusive ISO-8601 creation lower '
                                                              'bound (optional)',
                                              'created_to': 'inclusive ISO-8601 creation upper '
                                                            'bound (optional)',
                                              'updated_from': 'inclusive ISO-8601 update lower '
                                                              'bound (optional)',
                                              'updated_to': 'inclusive ISO-8601 update upper bound '
                                                            '(optional)',
                                              'date_from': 'compatibility alias for updated_from',
                                              'date_to': 'compatibility alias for updated_to',
                                              'weights': {   'semantic': 'number 0..2',
                                                             'lexical': 'number 0..2',
                                                             'recency': 'number 0..2'},
                                              'limit': 'integer 1..200 (optional)',
                                              'page': 'one-based direct page number (optional; '
                                                      'defaults to 1)',
                                              'snapshot_id': 'immutable ranking returned by page '
                                                             '1; required for direct pages after '
                                                             'page 1',
                                              'cursor': 'compatibility locator carrying snapshot '
                                                        'and next offset (optional)'}},
    'project.plan.import': {   'description': 'Validate a complete plan package and atomically '
                                              'replace this PostgreSQL plan.',
                               'object_ref': 'work:project:<project_id>',
                               'payload': {   'source_project_ref': 'source work:project URI',
                                              'package': 'complete problem-board.plan-index.v2 '
                                                         'document',
                                              'reference_mapping': 'optional legacy URI to final '
                                                                   'imported URI map',
                                              'assignment_outcomes': 'optional terminal assignment '
                                                                     'reports refused only because '
                                                                     'the referenced plan item was '
                                                                     'not yet in PostgreSQL',
                                              'expected_plan_revision': 'non-negative plan '
                                                                        'revision read before '
                                                                        'import',
                                              'idempotency_key': 'stable retry key'}},
    'project.references.preview': {   'description': 'Preview one project-wide reference migration '
                                                     'without changing stored state.',
                                      'object_ref': 'work:project:<project_id>',
                                      'payload': {   'reference_mapping': 'optional legacy URI to '
                                                                          'current canonical URI '
                                                                          'map'}},
    'project.references.migrate': {   'description': 'Apply the exact project-wide reference '
                                                     'migration returned by preview.',
                                      'object_ref': 'work:project:<project_id>',
                                      'payload': {   'expected_plan_revision': 'plan revision '
                                                                               'returned by the '
                                                                               'reviewed preview',
                                                     'expected_migration_hash': 'migration hash '
                                                                                'returned by the '
                                                                                'reviewed preview',
                                                     'reference_mapping': 'legacy URI to current '
                                                                          'canonical URI map '
                                                                          'returned by the '
                                                                          'reviewed preview',
                                                     'idempotency_key': 'stable retry key'}},
    'plan.item.create': {   'description': 'Create one plan item; assignment is a separate atomic '
                                           'ownership operation.',
                            'object_ref': 'work:project:<project_id>',
                            'payload': {   'item': 'complete plan-item source',
                                           'idempotency_key': 'stable retry key'}},
    'plan.item.update': {   'description': 'Update one plan item under its current revision; new '
                                           'attachment_refs must be staged uploads.',
                            'object_ref': 'work:project:<project_id>',
                            'payload': {   'work_ref': 'canonical plan-node URI',
                                           'expected_revision': 'positive integer',
                                           'changes': PLAN_ITEM_CHANGE_FIELDS,
                                           'idempotency_key': 'stable retry key'}},
    'work.assignee.set': {
        'description': 'Set or clear the current assignee in any valid status; status stays unchanged and existing Card/repository scope checks still apply.',
        'object_ref': 'work:project:<project_id>',
        'payload': {
            'work_ref': 'canonical plan-node URI',
            'assignee': 'stable worker name or operator:<user id>; empty string clears',
            'expected_revision': 'positive integer',
            'expected_ownership_version': 'integer; zero when never assigned',
            'reason': 'optional durable reason',
            'idempotency_key': 'stable retry key',
        },
    },
    'work.item.save': {
        'description': 'Save status only, assignee only, or both atomically. Omitted fields stay unchanged; empty assignee clears. Supplied steps need their existing permissions, never a separate composite grant.',
        'object_ref': 'work:project:<project_id>',
        'payload': {
            'work_ref': 'canonical plan-node URI',
            'status': 'optional: ' + ' | '.join(CANONICAL_WORK_STATUSES),
            'assignee': 'optional stable worker name or operator:<user id>; empty string clears',
            'expected_revision': 'positive integer',
            'expected_ownership_version': 'integer, required when assignee is supplied; zero when never assigned',
            'reason': 'optional reason; required for cancelled',
            'review': {'look_at': 'required when entering review', 'could_not_verify': "required when entering review; 'None' if nothing"},
            'tags': 'optional string[]', 'keywords': 'optional string[]',
            'idempotency_key': 'stable retry key',
        },
    },
    'work.status.set': {   'description': 'Set a canonical status; the assignee and the assignment '
                                          'stay as they are, including an empty assignee in Working. '
                                          'Entering review requires review.look_at and '
                                          "review.could_not_verify; state 'None' explicitly when "
                                          'there are no verification gaps.',
                           'object_ref': 'work:project:<project_id>',
                           'payload': {   'work_ref': 'canonical plan-node URI',
                                          'status': ' | '.join(CANONICAL_WORK_STATUSES),
                                          'reason': 'required for cancelled',
                                          'review': {   'look_at': 'required when entering review',
                                                        'could_not_verify': 'required when '
                                                                            'entering review; '
                                                                            "'None' if nothing"},
                                          'expected_revision': 'positive integer',
                                          'idempotency_key': 'stable retry key'}},
    'review.accept': {   'description': 'Accept the submitted result and evidence for work in '
                                        'review.',
                         'object_ref': 'work:project:<project_id>',
                         'payload': {   'work_ref': 'canonical plan-node URI',
                                        'expected_revision': 'positive integer',
                                        'reason': 'optional review note',
                                        'evidence': 'reference URI[]',
                                        'idempotency_key': 'stable retry key'}},
    'review.return': {   'description': 'Return reviewed work to working for rework with a durable '
                                        'reason. The assignee and the assignment stay as they are.',
                         'object_ref': 'work:project:<project_id>',
                         'payload': {   'work_ref': 'canonical plan-node URI',
                                        'expected_revision': 'positive integer',
                                        'reason': 'required return reason',
                                        'evidence': 'reference URI[]',
                                        'idempotency_key': 'stable retry key'}},
    'review.cancel': {   'description': 'Cancel reviewed work with a durable reason and evidence '
                                        'record.',
                         'object_ref': 'work:project:<project_id>',
                         'payload': {   'work_ref': 'canonical plan-node URI',
                                        'expected_revision': 'positive integer',
                                        'reason': 'required cancellation reason',
                                        'evidence': 'reference URI[]',
                                        'idempotency_key': 'stable retry key'}},
    'review.assign': {   'description': 'Name who reviews an item in review (W326): a linked agent '
                                        "other than the one who did the work, or 'operator' (a "
                                        'project admin) once the work is merged and deployed. The '
                                        'reviewer becomes the item\'s assignee, its current owner, in '
                                        'the same write. The assignment history keeps the last '
                                        'worker as worked_by. integration is required only when a '
                                        'person reviews.',
                         'object_ref': 'work:project:<project_id>',
                         'payload': {   'work_ref': 'canonical plan-node URI',
                                        'reviewer': 'stable worker name | operator | '
                                                    'operator:<user id>',
                                        'integration': {   'merged': 'merge commit[] (required for '
                                                                     'a person)',
                                                           'deploy': "'<window>: <check>' (or "
                                                                     'nothing_to_deploy)',
                                                           'nothing_to_deploy': 'true when the '
                                                                                'change needs no '
                                                                                'deploy'}}},
    'project.people.invite': {   'description': 'Invite an existing KDCube user to the project by '
                                                'email with a role; their Control Card starts with '
                                                "that role's preselection and is edited in "
                                                'Connection Hub.',
                                 'object_ref': 'work:project:<project_id>',
                                 'payload': {   'email': 'the address of their KDCube account',
                                                'role': 'admin | member (a preset)',
                                                'operations': 'refused '
                                                              '(work_invitation_operations_in_connection_hub): '
                                                              'an invitation takes a role only'}},
    'project.people.set_role': {   'description': 'Make a person on the project a project admin or '
                                                  'a member. A role changes no Card; the Control '
                                                  'Card is edited in Connection Hub.',
                                   'object_ref': 'work:project:<project_id>',
                                   'payload': {   'principal_key': "the person's principal key "
                                                                   'from the people list',
                                                  'role': 'admin | member'}},
    'project.people.card.update': {   'description': 'Retired: answers '
                                                     'work_control_card_edit_in_connection_hub '
                                                     "(410). A person's Control Card is edited in "
                                                     'Connection Hub.',
                                      'object_ref': 'work:project:<project_id>',
                                      'payload': {   'principal_key': "the person's principal key "
                                                                      'from the people list',
                                                     'operations': 'operation names'}},
    'project.role.get': {   'description': "Read an optional project role (knowledge-keeper): its state, holder and the holder's availability.",
                   'object_ref': 'work:project:<project_id>',
                   'payload': {'role': 'knowledge-keeper'}},
    'project.role.manage': {   'description': "Declare or undeclare an optional project role, or name or clear its holder, under the role's revision; a person only.",
                   'object_ref': 'work:project:<project_id>',
                   'payload': {'role': 'knowledge-keeper', 'expected_revision': 'the role revision you read', 'declared': 'true or false: declare or undeclare (use this or worker_name)', 'worker_name': 'the stable name of an attending agent, or empty to clear (use this or declared)'}},
    'project.role.handover.decide': {   'description': 'The agent holding an optional role records a hand-over as incorporated (with result_ref), declined or needs_evidence (with reason).',
                   'object_ref': 'work:project:<project_id>',
                   'payload': {'handover_ref': "the hand-over's ref from project.role.get", 'decision': 'incorporated, declined or needs_evidence', 'reason': 'why (declined or needs_evidence)', 'result_ref': 'the published result (incorporated)', 'expected_revision': 'the hand-over revision you read'}},
    'project.role.declare': {   'description': 'Alias of project.role.manage that declares or undeclares the role.',
                   'object_ref': 'work:project:<project_id>',
                   'payload': {'role': 'knowledge-keeper', 'expected_revision': 'the role revision you read', 'declared': 'true or false'}},
    'project.role.assign': {   'description': 'Alias of project.role.manage that names or clears the holder.',
                   'object_ref': 'work:project:<project_id>',
                   'payload': {'role': 'knowledge-keeper', 'expected_revision': 'the role revision you read', 'worker_name': 'the stable name of an attending agent, or empty to clear'}},
    'project.coordinator.get': {   'description': 'Read who holds the coordinator role now, the '
                                                  'home coordinator, and whether the home '
                                                  'coordinator is available.',
                                   'object_ref': 'work:project:<project_id>',
                                   'payload': {}},
    'project.coordinator.hand_over': {   'description': 'Hand the acting coordinator role to one '
                                                        'attending agent; it gains the coordinator '
                                                        'label, the home coordinator keeps its '
                                                        'own, and a previous acting holder that is '
                                                        'not the home is lowered to worker.',
                                         'object_ref': 'work:project:<project_id>',
                                         'payload': {   'worker_name': 'the stable name of an '
                                                                       'attending agent',
                                                        'expected_revision': 'the holder revision '
                                                                             'you read (0 before '
                                                                             'the first hand-over)',
                                                        'expected_until': 'optional ISO-8601 time '
                                                                          'the operator expects '
                                                                          'the role back; a '
                                                                          'reminder, never '
                                                                          'automatic',
                                                        'make_home': 'optional true: make the '
                                                                     'acting holder (worker_name) '
                                                                     'the permanent coordinator; '
                                                                     'the home moves to it in one '
                                                                     'revision-fenced write'}},
    'project.coordinator.return': {   'description': 'Return the acting coordinator role to the '
                                                     'home coordinator; labels stay as they are.',
                                      'object_ref': 'work:project:<project_id>',
                                      'payload': {   'expected_revision': 'the holder revision you '
                                                                          'read'}},
    'project.coordinator.note.write': {   'description': 'The agent holding the coordinator role '
                                                         'writes its part of the next handover '
                                                         'note: every section required, none when '
                                                         'there is nothing, refs only.',
                                          'object_ref': 'work:project:<project_id>',
                                          'payload': {   'sections': 'an object with '
                                                                     'runtime_windows, '
                                                                     'merge_queue, operator_waits, '
                                                                     'promised_notifications, '
                                                                     'blocked_on, research_owners, '
                                                                     'onboarding_checks, '
                                                                     'integrators'}},
    'project.announcement.publish': {   'description': 'Publish the project announcement the board '
                                                       'shows: the newest replaces the one before, '
                                                       'and it stops showing when it expires.',
                                        'object_ref': 'work:project:<project_id>',
                                        'payload': {   'kind': 'status, progress, blocker, notice or '
                                                               'window',
                                                       'text': 'one or two plain sentences, at most '
                                                               '600 characters',
                                                       'window_state': 'for kind window: opened, '
                                                                       'delayed or all_clear',
                                                       'planned_end': 'for an opened or delayed '
                                                                      'window: ISO-8601 time it is '
                                                                      'planned to end',
                                                       'expires_in_minutes': 'optional: how long '
                                                                             'it shows, 5 to 2880; '
                                                                             'each kind has a default',
                                                       'detail_ref': 'optional: a plan item or '
                                                                     'journal entry ref with the '
                                                                     'detail',
                                                       'idempotency_key': 'a stable key for this '
                                                                          'announcement'}},
    'project.coordinator.make': {   'description': 'Make an attending agent the acting '
                                                   'coordinator: its Card to the coordinator '
                                                   'profile, then the role; both receipts, and the '
                                                   'half that failed.',
                                    'object_ref': 'work:project:<project_id>',
                                    'payload': {   'worker_name': 'the stable name of an attending '
                                                                  'agent',
                                                   'expected_revision': 'the holder revision you '
                                                                        'read',
                                                   'expected_until': 'optional ISO-8601 time the '
                                                                     'role is expected back',
                                                   'expected_card_revision': 'optional Card '
                                                                             'revision you read',
                                                   'only': 'optional: card, role or '
                                                           'previous_holder, to repeat only the '
                                                           'half that failed'}},
    'project.coordinator.make_worker': {   'description': 'Make the acting coordinator (or a '
                                                          'former home) a worker again: the role '
                                                          'back to the home coordinator and its '
                                                          'coordinator label dropped, then its '
                                                          'Card to the default worker profile; '
                                                          'both receipts.',
                                           'object_ref': 'work:project:<project_id>',
                                           'payload': {   'worker_name': 'the stable name of the '
                                                                         'acting agent',
                                                          'expected_revision': 'the holder '
                                                                               'revision you read',
                                                          'expected_card_revision': 'optional Card '
                                                                                    'revision you '
                                                                                    'read',
                                                          'only': 'optional: card or role, to '
                                                                  'repeat only the half that '
                                                                  'failed'}},
    'project.coordinator.set_away': {   'description': 'Mark the home coordinator away or back; '
                                                       'away reads as unavailable whatever its '
                                                       'session reports.',
                                        'object_ref': 'work:project:<project_id>',
                                        'payload': {   'away': 'true or false',
                                                       'expected_revision': 'the holder revision '
                                                                            'you read'}},
    'work.accept': {   'description': 'Compatibility alias for review.accept during the lifecycle '
                                      'migration.',
                       'object_ref': 'work:project:<project_id>',
                       'payload': {   'work_ref': 'canonical plan-node URI',
                                      'expected_revision': 'positive integer',
                                      'idempotency_key': 'stable retry key'}},
    'plan.item.delete': {   'description': 'Delete one unassigned leaf item under its current '
                                           'revision.',
                            'object_ref': 'work:project:<project_id>',
                            'payload': {   'work_ref': 'canonical plan-node URI',
                                           'expected_revision': 'positive integer',
                                           'idempotency_key': 'stable retry key'}},
    'plan.note.append': {   'description': "Append one note and advance its item's revision "
                                           'atomically.',
                            'object_ref': 'work:project:<project_id>',
                            'payload': {   'work_ref': 'canonical plan-node URI',
                                           'text': 'note text',
                                           'expected_revision': 'positive integer',
                                           'idempotency_key': 'stable retry key'}},
    'plan.notes.list': {   'description': 'Page the authoritative notes attached to one plan item.',
                           'object_ref': 'work:project:<project_id>',
                           'payload': {   'work_ref': 'canonical plan-node URI (use this or '
                                                      'item_key)',
                                          'item_key': 'case-insensitive project-scoped item key '
                                                      '(use this or work_ref)',
                                          'cursor': 'opaque continuation cursor',
                                          'limit': 'integer 1..100'}},
    'project.plan.embedding_status': {   'description': 'Read missing and stale plan-search '
                                                        'embeddings without invoking a model or '
                                                        'changing indexed state.',
                                         'object_ref': 'work:project:<project_id>',
                                         'payload': {   'limit': 'integer 1..200 (optional)',
                                                        'cursor': 'opaque next_cursor from the '
                                                                  'previous page (optional)'}},
    'project.control.get': {   'description': "Read the project's linked Connection Hub Control "
                                              'Card, catalog state, project properties, and '
                                              'participant links.',
                               'object_ref': 'work:project:<project_id>',
                               'payload': {}},
    'project.control.initialize': {   'description': "Create the project's credentialless "
                                                     'Connection Hub Card and attach it to current '
                                                     'participants. The coordinator Card may '
                                                     'supply the initially checked access.',
                                      'object_ref': 'work:project:<project_id>',
                                      'payload': {   'coordinator_worker_name': 'linked '
                                                                                'coordinator '
                                                                                'worker name',
                                                     'composition_mode': 'and|or',
                                                     'properties': {   'coordination': {   'version_control': {   'model': 'shared-main|isolated-branch',
                                                                                                                  'reason': 'operator '
                                                                                                                            'explanation'}}}}},
    'project.control.update': {   'description': "Update the project's version-control choice or "
                                                 'AND/OR access rule on its current Connection Hub '
                                                 'Card revision. Manage concrete access in '
                                                 'Connection Hub.',
                                  'object_ref': 'work:project:<project_id>',
                                  'payload': {   'expected_revision': 'integer',
                                                 'changes': {   'composition_mode': 'and|or',
                                                                'properties': {   'coordination': {   'version_control': {   'model': 'shared-main|isolated-branch',
                                                                                                                             'reason': 'operator '
                                                                                                                                       'explanation'}}}}}},
    'journal.view.request': {   'description': 'Ask a linked LOCAL relay for one expiring, '
                                               'filtered and ranked journal catalog page or a '
                                               'complete Markdown entry.',
                                'object_ref': 'work:project:<project_id>',
                                'payload': {   'worker_name': 'string',
                                               'mode': 'catalog|document',
                                               'repository_journal_ref': 'repo:<alias>/<relative-path> '
                                                                         'for document',
                                               'query': 'optional catalog query',
                                               'cursor': 'next_cursor from the preceding catalog '
                                                         'page',
                                               'author': 'optional worker name',
                                               'date_from': 'optional YYYY-MM-DD',
                                               'date_to': 'optional YYYY-MM-DD',
                                               'statuses': [   'in-progress|completed|blocked|decision|handoff'],
                                               'weights': {   'semantic': 0,
                                                              'lexical': 'number 0..2',
                                                              'recency': 'number 0..2'},
                                               'limit': '1..100',
                                               'ttl_seconds': '60..3600'}},
    'journal.view.publish': {   'description': "Fulfil the caller relay's pending journal view "
                                               'with one catalog page or complete Markdown body.',
                                'object_ref': 'work:journal_view:<created-at>:<view-id>:<semantic-name>',
                                'payload': {   'entries': 'sanitized catalog rows',
                                               'next_cursor': 'cursor for the next catalog page',
                                               'title': 'string',
                                               'content': 'complete Markdown',
                                               'content_hash': 'sha256',
                                               'source_commit': 'optional 40-hex LOCAL source commit',
                                               'snapshot_status': {   'schema': 'problem-board.journal-snapshot-status.v1',
                                                                      'index_state': 'ready|ready_with_issues|partial|stale|not_built|unavailable|unknown',
                                                                      'local_freshness': 'current_local_source|unverified',
                                                                      'indexed_at': 'ISO-8601 with timezone or empty',
                                                                      'indexed_entries': 'integer 0..1000000',
                                                                      'issue_count': 'integer 0..1000000',
                                                                      'excluded_count': 'integer 0..1000000',
                                                                      'issue_codes': ['compatibility_warning|index_issue'],
                                                                      'exclusion_codes': ['excluded_entry'],
                                                                      'clone_state': 'current|ahead|behind|diverged|no_upstream|missing|unknown',
                                                                      'compared_commit': '40-hex last-fetched comparison commit or empty',
                                                                      'behind': 'integer 0..1000000',
                                                                      'origin_freshness': 'last_fetched_ref_only|unverified'},
                                               'repository_journal_ref': 'requested portable ref '
                                                                         'for document mode'}},
    'journal.view.fail': {   'description': "Settle the caller relay's pending journal view with a "
                                            'bounded failure.',
                             'object_ref': 'work:journal_view:<created-at>:<view-id>:<semantic-name>',
                             'payload': {'error_code': 'string', 'error_summary': 'string'}},
    'project.file.edit.result': {   'description': 'Report how a project-file edit made on the '
                                                   "board landed in the coordinator's clone "
                                                   '(W370); accepted once.',
                                    'object_ref': 'work:file_edit:<requested-at>:<edit-id>:<semantic-name>',
                                    'payload': {   'edit_ref': 'the same ref',
                                                   'outcome': 'committed|pr_opened|branch_pushed|unchanged|refused',
                                                   'commit': '40-hex commit',
                                                   'pr_url': 'https pull request or compare link',
                                                   'reason': 'string',
                                                   'reason_code': 'string'}},
    'journal.view.close': {   'description': 'Erase an expiring journal snapshot after the '
                                             'requesting user closes it.',
                              'object_ref': 'work:journal_view:<created-at>:<view-id>:<semantic-name>',
                              'payload': {'project_ref': 'work:project:<project_id>'}},
    'project.link_worker': {   'description': 'Link an idle published worker to this project; '
                                              'unlink it from any current project first.',
                               'object_ref': 'work:project:<project_id>',
                               'payload': {   'worker_name': 'string',
                                              'role': 'worker|coordinator|relay'}},
    'project.unlink_worker': {   'description': "End one worker's current project attendance while "
                                                'retaining its identity, history, and place in the '
                                                "operator's pool.",
                                 'object_ref': 'work:project:<project_id>',
                                 'payload': {   'worker_name': 'string',
                                                'delete_conversations': 'boolean, optional; the '
                                                                        "agent's owner only: also "
                                                                        "delete this project's "
                                                                        'messages from the '
                                                                        'conversation store'}},
    'worker.publish': {   'description': "Register the caller's principal-bound coding-agent "
                                         'session. runtime_kind plus the native resumable session '
                                         'id determines the stable worker address; alias is '
                                         'optional display metadata.',
                          'object_ref': 'work:worker:self',
                          'payload': {   'runtime_kind': 'codex|claude-code|resident|relay',
                                         'runtime_session_id': 'native resumable session id',
                                         'worker_alias': 'optional mutable alias',
                                         'runtime_account': 'token-free account identity when '
                                                            'runtime_account_evidence.state is '
                                                            'reported',
                                         'runtime_account_evidence': '{state: '
                                                                     'reported|stale|missing, '
                                                                     'source: host-report, '
                                                                     'observed_at}',
                                         'capabilities': ['string'],
                                         'host_id': 'stable logical id',
                                         'host_label': 'operator-readable name',
                                         'host_kind': 'local|hosted|remote',
                                         'relay_id': 'string',
                                         'reconcile_ceiling_seconds': 'integer',
                                         'relay_degraded_intervals': [   {   'code': 'retryable '
                                                                                     'relay error',
                                                                             'started_at': 'ISO-8601',
                                                                             'ended_at': 'ISO-8601'}]}},
    'worker.rename': {   'description': "Change a worker's display alias without changing its "
                                        'runtime/session identity, credential binding, project '
                                        'attendance, or assignment ownership.',
                         'object_ref': 'work:worker:<runtime-kind>:<native-session-id> or '
                                       'work:worker:self',
                         'payload': {'worker_alias': 'string'}},
    'worker.estimate': {   'description': 'Record until when (UTC) this worker expects to finish '
                                          'what it is on, with a one-line note, or clear it with '
                                          'an empty busy_until. The board shows it and marks it '
                                          'overdue once the time has passed.',
                           'object_ref': 'work:worker:self or '
                                         'work:worker:<runtime-kind>:<native-session-id>',
                           'payload': {   'busy_until': 'ISO-8601 UTC or empty to clear',
                                          'note': 'one line'}},
    'worker.heartbeat': {   'description': "Refresh relay presence and return the worker's "
                                           'optional current project. When a project is supplied, '
                                           "also report that session's LOCAL inbox evidence and "
                                           'return the portable journal binding.',
                            'object_ref': 'work:worker:self',
                            'payload': {   'availability': 'string',
                                           'project_ref': 'string',
                                           'work_ref': 'string',
                                           'agent_sessions': 'bounded session check-in rows from '
                                                             'the LOCAL field',
                                           'runtime_account': 'token-free account identity when '
                                                              'runtime_account_evidence.state is '
                                                              'reported',
                                           'runtime_account_evidence': '{state: '
                                                                       'reported|stale|missing, '
                                                                       'source: host-report, '
                                                                       'observed_at}; an absent '
                                                                       'key leaves stored evidence '
                                                                       'unchanged',
                                           'worker_info': "{text}: the worker's one-line info "
                                                          'note, at most 200 characters; empty '
                                                          'text clears it, an absent key leaves '
                                                          'it'}},
    'worker.evict': {   'description': 'Move a worker to reversible limbo. The Connection Hub '
                                       'credential remains a separate authority decision.',
                        'object_ref': 'work:worker:<runtime-kind>:<native-session-id>',
                        'payload': {   'project_ref': 'string',
                                       'worker_name': 'string',
                                       'reason': 'string'}},
    'worker.restore': {   'description': 'Return a worker registration from limbo to the pool.',
                          'object_ref': 'work:worker:<runtime-kind>:<native-session-id>',
                          'payload': {'project_ref': 'string', 'worker_name': 'string'}},
    'worker.retire': {   'description': 'Permanently close one exact coding-agent session in '
                                        'Problem Board while retaining historical attribution, and '
                                        'revoke the Connection Hub Card that session was enrolled '
                                        'with. A failed revocation is recorded on the worker and '
                                        'retried by retiring it again.',
                         'object_ref': 'work:worker:<runtime-kind>:<native-session-id>',
                         'payload': {   'worker_name': 'string',
                                        'confirm_worker_name': 'same stable worker name',
                                        'reason': 'string',
                                        'delete_conversations': 'boolean, optional; also delete '
                                                                'the whole conversation with this '
                                                                'session from the conversation '
                                                                'store'}},
    'control.enqueue': {   'description': 'Queue a bounded request, ping, stop, resume, '
                                          'materialize, or replan command for one linked worker. '
                                          'Use assignment.assign for ownership-changing work.',
                           'object_ref': 'work:project:<project_id>',
                           'payload': {   'worker_name': 'string',
                                          'kind': 'string',
                                          'subject': 'string',
                                          'work_ref': 'string',
                                          'command': 'object',
                                          'idempotency_key': 'string'}},
    'control.discard': {   'description': 'Discard selected messages previously sent by this '
                                          'operator. Pending messages are suppressed; messages '
                                          'already received produce a soft-discard notice.',
                           'object_ref': 'work:worker:<runtime-kind>:<native-session-id>',
                           'payload': {   'command_refs': [   'work:control:<created-at>:<command-id>:<semantic-name>'],
                                          'reason': 'optional intent',
                                          'idempotency_key': 'string'}},
    'assignment.assign': {   'description': 'Assign or reassign one work item, advance its '
                                            'ownership version, attach a Git source base, and '
                                            'queue the fenced command for the selected worker.',
                             'object_ref': 'work:project:<project_id>',
                             'payload': {   'work_ref': 'canonical '
                                                        'work:plan:node:<timestamp>:<key>:<semantic-name> '
                                                        'URI',
                                            'worker_name': 'string',
                                            'title': 'string',
                                            'task': 'bounded object',
                                            'expected_ownership_version': 'integer',
                                            'reopen': 'operator-only boolean for review, done, or '
                                                      'cancelled work',
                                            'source_repositories': '[{repository_ref, base_commit, '
                                                                   'branch}] for every repository '
                                                                   'the work touches; an explicit '
                                                                   'empty list says it touches '
                                                                   'none',
                                            'source_repository_ref': 'repo:<alias>/<relative-path> '
                                                                     'with base_commit '
                                                                     '(one-repository form, '
                                                                     'mirrors the first entry)',
                                            'source_base_commit': 'Git object id',
                                            'source_branch': 'optional branch/worktree hint',
                                            'idempotency_key': 'string'}},
    'assignment.return': {   'description': 'Release the active assignment of stalled work when '
                                            'its owner says the work stopped: the inverse of '
                                            'assignment.assign. Retires the assignment as '
                                            'returned, advances its ownership version, records the '
                                            "reason and leaves the item's status as its assignee "
                                            'last reported. Cancelling stays with the operator.',
                             'object_ref': 'work:project:<project_id>',
                             'payload': {   'work_ref': 'canonical '
                                                        'work:plan:node:<timestamp>:<key>:<semantic-name> '
                                                        'URI',
                                            'reason': "the owner's stated reason (required)",
                                            'expected_revision': 'integer item revision',
                                            'idempotency_key': 'string'}},
    'assignment.list': {   'description': "Page one worker's assignments in newest-first order. A "
                                          'worker reads itself; the current active coordinator '
                                          'holder or a signed-in project reader may name another '
                                          'linked worker.',
                           'object_ref': 'work:project:<project_id>',
                           'payload': {   'worker_name': 'linked stable worker name; required for '
                                                         'project readers and coordinator '
                                                         'cross-worker reads, otherwise omitted by '
                                                         'a worker reading itself',
                                          'refs': 'assignment URI, plan-node URI, or an array of '
                                                  'either (optional)',
                                          'status': 'assigned|working|blocked|completed|refused|accepted|returned|cancelled '
                                                    'or an array (optional)',
                                          'visibility': 'current|current_and_settled|all_history '
                                                        'outer scope (optional; omitted keeps the '
                                                        'current-only worker default unless status '
                                                        'is explicit)',
                                          'item_status': 'plan-view work-item status or an array '
                                                         '(optional)',
                                          'query': 'literal case-insensitive match across work '
                                                   'key, title, assignment reference, and work '
                                                   'reference (optional)',
                                          'created_from': 'inclusive ISO-8601 lower bound for '
                                                          'assignment assigned_at (optional)',
                                          'created_to': 'inclusive ISO-8601 upper bound for '
                                                        'assignment assigned_at (optional)',
                                          'updated_from': 'inclusive ISO-8601 lower bound for '
                                                          'assignment updated_at (optional)',
                                          'updated_to': 'inclusive ISO-8601 upper bound for '
                                                        'assignment updated_at (optional)',
                                          'date_from': 'compatibility alias for updated_from '
                                                       '(optional)',
                                          'date_to': 'compatibility alias for updated_to '
                                                     '(optional)',
                                          'match': 'all|any; all by default',
                                          'cursor': 'opaque next_cursor from the previous page '
                                                    '(optional)',
                                          'limit': 'integer 1..200 (optional)'}},
    'workspace.shared_write.publish': {   'description': "Publish or replace this worker's "
                                                         'expiring status for an imminent shared '
                                                         'write or reload-sensitive source state.',
                                          'object_ref': 'work:project:<project_id>',
                                          'payload': {   'kind': 'stage|commit|pull|deploy|reload|relay_restart|source_in_flight|source_restructure',
                                                         'summary': 'what this worker is about to '
                                                                    'change',
                                                         'targets': 'array of repository paths, '
                                                                    'refs, services, or runtime '
                                                                    'surfaces',
                                                         'ttl_seconds': 'integer 30..86400; 900 by '
                                                                        'default'}},
    'workspace.shared_write.list': {   'description': 'Read every unexpired shared-write status '
                                                      'currently published for this project.',
                                       'object_ref': 'work:project:<project_id>',
                                       'payload': {}},
    'workspace.shared_write.clear': {   'description': "Remove this worker's own shared-write "
                                                       'status after the announced work is '
                                                       'finished.',
                                        'object_ref': 'work:project:<project_id>',
                                        'payload': {}},
    'assignment.report': {   'description': 'Append progress or a terminal result for an ownership '
                                            'version the caller held. A completed report entering '
                                            'Review requires review.look_at and '
                                            'review.could_not_verify in the report or on the item; '
                                            "state 'None' explicitly if no gap remains. State plus "
                                            'source_event_ref identifies one immutable report, so '
                                            'an unchanged retry replays and a later stage uses a '
                                            'new source event. Current ownership updates '
                                            'assignment and item state; a superseded report '
                                            'remains visible without changing current state, and '
                                            'an accepted terminal report is final for that '
                                            'ownership version.',
                             'object_ref': 'work:assignment:<created-at>:<assignment-id>:<semantic-name>',
                             'payload': {   'ownership_version': 'integer',
                                            'state': 'working|blocked|completed|refused',
                                            'summary': 'string',
                                            'result_ref': 'commit, PR, artifact, or journal ref',
                                            'source_event_ref': 'stable local event ref',
                                            'review': {   'look_at': 'verification steps for '
                                                                     'completed result',
                                                          'could_not_verify': 'remaining gaps, or '
                                                                              "explicit 'None'"}}},
    'mail.route': {   'description': 'Route one addressed worker message through the broker to '
                                     "another linked worker's host relay. The complete body is "
                                     'retained only until destination-relay delivery.',
                      'object_ref': 'work:project:<project_id>',
                      'payload': {   'recipient': 'worker name, or operator for the project '
                                                  "owner's inbox",
                                     'kind': 'question|reply|handoff|update|result|custom; to the '
                                             'operator: '
                                             'question|blocked|decision|progress|reply|update|result',
                                     'subject': 'string',
                                     'body': 'string',
                                     'payload': 'object',
                                     'source_message_ref': 'work:mail:<created-at>:<message-id>:<semantic-name>',
                                     'work_ref': 'optional canonical plan-node URI',
                                     'correlation_id': 'string',
                                     'reply_to': 'string',
                                     'idempotency_key': 'string'}},
    'mail.reconciliation.list': {   'description': 'Page completed mailbox reconciliation receipts '
                                                   'retained for this project. The read uses '
                                                   'PostgreSQL receipts and never opens private '
                                                   'host mailbox files.',
                                    'object_ref': 'work:project:<project_id>',
                                    'payload': {   'cursor': 'opaque next_cursor from the previous '
                                                             'page (optional)',
                                                   'limit': 'integer 1..200 (optional)'}},
    'mail.reconciliation.read': {   'description': 'Page normalized mailbox, archive, '
                                                   'failure-notice, and report-failure evidence '
                                                   'for one completed reconciliation receipt.',
                                    'object_ref': 'work:project:<project_id>',
                                    'payload': {   'receipt_ref': 'canonical '
                                                                  'work:mail_reconciliation URI',
                                                   'cursor': 'opaque next_cursor from the previous '
                                                             'page (optional)',
                                                   'limit': 'integer 1..200 (optional)'}},
    'mail.reconciliation.publish': {   'description': 'Publish one bounded integrity-bound batch '
                                                      'from a host-local mailbox reconciliation '
                                                      'receipt.',
                                       'object_ref': 'work:project:<project_id>',
                                       'payload': 'problem-board.mailbox-reconciliation-publication.v1'},
    'control.pull': {   'description': 'Lease pending controls addressed to the caller for one '
                                       'project it currently attends.',
                        'object_ref': 'work:worker:self',
                        'payload': {   'project_ref': 'work:project:<project_id>',
                                       'lease_owner': 'string',
                                       'lease_seconds': 'integer',
                                       'limit': 'integer'}},
    'control.acknowledge': {   'description': 'Acknowledge one leased control; KDCube erases its '
                                              'command body and retains the hash and receipt.',
                               'object_ref': 'work:control:<created-at>:<command-id>:<semantic-name>',
                               'payload': {   'lease_id': 'string',
                                              'lease_owner': 'string',
                                              'result_summary': 'string',
                                              'result_ref': 'string',
                                              'result': 'bounded structured host outcome'}},
    'control.refuse': {   'description': 'Refuse one leased control with a bounded reason and '
                                         'retain its receipt.',
                          'object_ref': 'work:control:<created-at>:<command-id>:<semantic-name>',
                          'payload': {   'lease_id': 'string',
                                         'lease_owner': 'string',
                                         'result_summary': 'string'}},
    'control.discard_complete': {   'description': "Report the receiving host's per-message "
                                                   'discard outcomes and optional soft-notice ref.',
                                    'object_ref': 'work:control:<created-at>:<discard-command-id>:<semantic-name>',
                                    'payload': {   'lease_id': 'string',
                                                   'lease_owner': 'string',
                                                   'outcomes': [   {   'command_ref': 'work:control:<created-at>:<command-id>:<semantic-name>',
                                                                       'outcome': 'discarded_before_receipt|already_discarded|already_received|not_present'}],
                                                   'notice_ref': 'optional local mail ref'}},
    'control.worker_settle': {   'description': 'Report how the user-started coding-agent session '
                                                'handled mail already delivered to its local '
                                                'inbox.',
                                 'object_ref': 'work:control:<created-at>:<command-id>:<semantic-name>',
                                 'payload': {   'session_id': 'string',
                                                'outcome': 'acknowledged|refused',
                                                'result_summary': 'string',
                                                'result_ref': 'local message ref'}},
    'plan.nodes.publish': {   'description': 'Atomically publish the supplied plan nodes as '
                                             'compact indexed rows without replacing omitted '
                                             'nodes.',
                              'object_ref': 'work:project:<project_id>',
                              'payload': {'batch': 'problem-board.plan-nodes.v1'}},
    'plan.index.embed': {   'description': 'Explicitly spend on accounted embeddings for '
                                           'plan-index rows whose source-derived search content '
                                           'changed.',
                            'object_ref': 'work:project:<project_id>',
                            'payload': {   'item_refs': 'optional canonical plan-node URI[]; stale '
                                                        'or missing rows by default',
                                           'limit': 'integer 1..200 (optional)'}},
    'event.publish': {   'description': 'Publish one short service event with refs and hashes; a '
                                        "limbo caller's attempt is retained as ignored.",
                         'object_ref': 'work:project:<project_id>',
                         'payload': {   'kind': 'string',
                                        'summary': 'string',
                                        'source_event_ref': 'string',
                                        'work_ref': 'string',
                                        'metadata': 'object',
                                        'content_hash': 'string'}},
    'note.view.publish': {   'description': 'Publish one requested page of work-item notes.',
                             'object_ref': 'work:note_view:<created-at>:<view-id>:<semantic-name>',
                             'payload': {   'items': 'note summary[]',
                                            'item': 'optional complete note',
                                            'next_cursor': 'opaque cursor',
                                            'total': 'integer',
                                            'content_hash': 'string'}},
    'note.view.fail': {   'description': 'Report a bounded failure for a requested note page.',
                          'object_ref': 'work:note_view:<created-at>:<view-id>:<semantic-name>',
                          'payload': {'error_code': 'string', 'error_summary': 'string'}},
    'project.report.publish': {   'description': 'Publish one immutable project progress report '
                                                 'and its evidence refs.',
                                  'object_ref': 'work:report:<created-at>:<report-id>:<semantic-name>',
                                  'payload': {   'document': 'problem-board.project-report.v1',
                                                 'staged_refs': 'staged attachment ref[]'}},
    'project.report.fail': {   'description': 'Report a bounded failure for a requested project '
                                              'report.',
                               'object_ref': 'work:report:<created-at>:<report-id>:<semantic-name>',
                               'payload': {'error_code': 'string', 'error_summary': 'string'}},
    'session.resume.publish': {   'description': 'Publish an expiring local command for resuming '
                                                 'this agent session.',
                                  'object_ref': 'work:session_resume:<created-at>:<view-id>:<semantic-name>',
                                  'payload': {   'runtime_kind': 'codex|claude-code',
                                                 'runtime_session_id': 'native resumable session '
                                                                       'id',
                                                 'command': 'bounded local command',
                                                 'command_hash': 'string'}},
    'session.resume.fail': {   'description': 'Report a bounded failure to prepare a '
                                              'session-resume command.',
                               'object_ref': 'work:session_resume:<created-at>:<view-id>:<semantic-name>',
                               'payload': {'error_code': 'string', 'error_summary': 'string'}},
    'attachment.request_upload': {   'description': 'Reserve a governed upload slot for a '
                                                    'worker-produced file.',
                                     'object_ref': 'work:project:<project_id>',
                                     'payload': {'filename': 'basename'}}}

# The fields the service refuses each call without, as its validation reads
# them. Only fields a refusal is known for are listed: a field missing from
# this table is checked by the service, never guessed here.
PROBLEM_BOARD_OPERATION_REQUIRED: dict[str, tuple[str, ...]] = {
    "project.role.get": ("role",),
    "project.role.manage": ("role", "expected_revision"),
    "project.role.handover.decide": ("handover_ref", "decision", "expected_revision"),
    "project.role.declare": ("role", "declared", "expected_revision"),
    "project.role.assign": ("role", "expected_revision"),
    "work.attachment.link": ("file_ref",),
    "assignment.assign": ("idempotency_key",),
    "assignment.return": ("idempotency_key",),
    "plan.item.create": ("item", "idempotency_key"),
    "plan.item.delete": ("work_ref", "idempotency_key"),
    "plan.item.update": ("work_ref", "expected_revision", "changes", "idempotency_key"),
    "plan.note.append": ("work_ref", "text", "expected_revision", "idempotency_key"),
    "project.announcement.publish": ("kind", "text", "idempotency_key"),
    "review.accept": ("work_ref", "expected_revision", "idempotency_key"),
    "review.return": ("work_ref", "expected_revision", "reason", "idempotency_key"),
    "review.cancel": ("work_ref", "expected_revision", "reason", "idempotency_key"),
    "project.plan.import": ("idempotency_key",),
    "project.references.migrate": ("idempotency_key",),
    "work.accept": ("idempotency_key",),
    "work.status.set": ("work_ref", "status", "expected_revision", "idempotency_key"),
    "work.assignee.set": ("work_ref", "assignee", "expected_revision", "expected_ownership_version", "idempotency_key"),
    "work.item.save": ("work_ref", "expected_revision", "idempotency_key"),
}

# The payload field each object kind is sometimes mistaken for: a call that
# puts it in the payload instead of passing the object as --object-ref.
_OBJECT_FIELD_BY_KIND = {
    "work:project:": "project_ref",
    "work:assignment:": "assignment_ref",
    "work:worker:": "worker_ref",
}

_ONE_OF = re.compile(r"\(use this or ([a-z_]+)\)")


def operation_shape(operation: str) -> dict[str, Any]:
    """A copy of one operation's catalog entry, or an empty dict."""

    shape = PROBLEM_BOARD_OPERATION_SHAPES.get(str(operation or ""))
    return copy.deepcopy(shape) if shape else {}


def operation_object_patterns(operation: str) -> tuple[str, ...]:
    """Each object-ref form the operation accepts, as the catalog writes it."""

    raw = str(operation_shape(operation).get("object_ref") or "")
    return tuple(part.strip() for part in raw.split(" or ") if part.strip())


def _pattern_prefix(pattern: str) -> str:
    return pattern.split("<", 1)[0]


def operation_one_of(operation: str) -> tuple[tuple[str, ...], ...]:
    """The selector pairs a call carries exactly one of."""

    payload = operation_shape(operation).get("payload")
    if not isinstance(payload, Mapping):
        return ()
    groups: list[tuple[str, ...]] = []
    for field, description in payload.items():
        match = _ONE_OF.search(str(description))
        if not match:
            continue
        group = tuple(sorted({str(field), match.group(1)}))
        if group not in groups:
            groups.append(group)
    return tuple(groups)


# Nested objects an operation refuses as null, as its service reads them:
# plan.item.update refuses changes.review that is not an object
# (work_item_review_invalid). A nested object not listed here may be null,
# as review_requirement (read as the default) and work.status.set review
# (optional) are.
_STRICT_OBJECTS: dict[str, frozenset[str]] = {
    "plan.item.update": frozenset({"changes", "changes.review"}),
}


# Payloads whose example needs real types: an integer revision and a nested
# object, not a string placeholder for every field. Each is a valid call once
# the angle-bracket values are filled in.
_EXAMPLE_PAYLOADS: dict[str, dict[str, Any]] = {
    "plan.item.update": {
        "work_ref": "<work_ref>",
        "expected_revision": 12,
        "changes": {
            "review": {
                "look_at": "<the steps the reviewer performs>",
                "could_not_verify": "None",
            }
        },
        "idempotency_key": "<idempotency_key>",
    },
    "work.status.set": {
        "work_ref": "<work_ref>",
        "status": "working",
        "expected_revision": 12,
        "idempotency_key": "<idempotency_key>",
    },
    "work.item.save": {
        "work_ref": "<work_ref>", "status": "done", "assignee": "<worker_name>",
        "expected_revision": 12, "expected_ownership_version": 2,
        "idempotency_key": "<idempotency_key>",
    },
    "work.assignee.set": {
        "work_ref": "<work_ref>", "assignee": "<worker_name>",
        "expected_revision": 12, "expected_ownership_version": 2,
        "idempotency_key": "<idempotency_key>",
    },
    "review.assign": {
        "work_ref": "<work_ref>",
        "reviewer": "operator",
        "integration": {"merged": ["<merge commit>"], "deploy": "<window>: <check>"},
    },
}


def _example_value(field: str, description: Any) -> Any:
    """A placeholder with the field's type: an object stays an object."""

    if isinstance(description, Mapping):
        return {
            str(name): _example_value(str(name), child)
            for name, child in description.items()
            if "optional" not in str(child)
        }
    text = str(description)
    if "integer" in text:
        return 1
    if "[]" in text or text.startswith("list of"):
        return [f"<{field}>"]
    if text.startswith("true when"):
        return True
    return f"<{field}>"


def operation_example(operation: str) -> str:
    """A copyable ``pb coordinate`` command for the operation."""

    patterns = operation_object_patterns(operation)
    object_ref = patterns[0] if patterns else ""
    if object_ref.startswith("work:project:"):
        object_ref = "<project-ref>"
    payload = operation_shape(operation).get("payload")
    described = payload if isinstance(payload, Mapping) else {}
    required = PROBLEM_BOARD_OPERATION_REQUIRED.get(operation, ())
    example: dict[str, Any] = copy.deepcopy(_EXAMPLE_PAYLOADS.get(operation, {}))
    for group in operation_one_of(operation):
        chosen = "item_key" if "item_key" in group else group[0]
        example.setdefault(chosen, f"<{chosen}>")
    for field in required:
        example.setdefault(field, _example_value(field, described.get(field)))
    if not example:
        for field, description in described.items():
            if "optional" not in str(description):
                example[str(field)] = _example_value(str(field), description)
    command = f"pb coordinate {operation}"
    if object_ref:
        command += f" --object-ref {object_ref}"
    command += " --payload-json '" + json.dumps(example, separators=(",", ":")) + "'"
    return command


def operation_contract(operation: str) -> dict[str, Any]:
    """Everything a caller needs to shape one call, from the catalog."""

    shape = operation_shape(operation)
    if not shape:
        return {}
    return {
        "operation": operation,
        "description": str(shape.get("description") or ""),
        "object_ref": list(operation_object_patterns(operation)),
        "payload": shape.get("payload"),
        "required": list(PROBLEM_BOARD_OPERATION_REQUIRED.get(operation, ())),
        "one_of": [list(group) for group in operation_one_of(operation)],
        "example": operation_example(operation),
    }


def operation_call_problems(
    operation: str, object_ref: str, payload: Mapping[str, Any]
) -> list[dict[str, str]]:
    """What is wrong with a call's shape, before it is sent.

    Each problem names its field and says what to do instead. An empty list
    means the call has the catalog's shape; the service still decides the
    rest.
    """

    problems: list[dict[str, str]] = []
    patterns = operation_object_patterns(operation)
    clean_ref = str(object_ref or "").strip()
    if patterns and not clean_ref:
        misplaced = ""
        for pattern in patterns:
            field = _OBJECT_FIELD_BY_KIND.get(_pattern_prefix(pattern), "")
            if field and field in payload:
                misplaced = field
        if misplaced:
            problems.append({
                "field": misplaced,
                "problem": "misplaced",
                "message": (
                    f"{misplaced} belongs in --object-ref, not in the payload: "
                    f"pass --object-ref {payload.get(misplaced)} and leave it out of the payload."
                ),
            })
        else:
            problems.append({
                "field": "object_ref",
                "problem": "missing",
                "message": "Pass the object with --object-ref " + " or ".join(patterns) + ".",
            })
    elif patterns and not any(
        clean_ref == pattern if "<" not in pattern else clean_ref.startswith(_pattern_prefix(pattern))
        for pattern in patterns
    ):
        problems.append({
            "field": "object_ref",
            "problem": "wrong_kind",
            "message": f"--object-ref {clean_ref} is not " + " or ".join(patterns) + ".",
        })
    for group in operation_one_of(operation):
        present = [field for field in group if payload.get(field) not in (None, "", [])]
        if len(present) != 1:
            problems.append({
                "field": " | ".join(group),
                "problem": "one_of",
                "message": "Carry exactly one of " + " and ".join(group) + " in the payload.",
            })
    for field in PROBLEM_BOARD_OPERATION_REQUIRED.get(operation, ()):
        if operation == "work.assignee.set" and field == "assignee" and field in payload and payload[field] == "":
            continue  # Explicit clear is present, not missing.
        if payload.get(field) in (None, "", [], {}):
            problems.append({
                "field": field,
                "problem": "missing",
                "message": f"The payload needs {field}.",
            })
    described = operation_shape(operation).get("payload")
    if isinstance(described, Mapping):
        problems.extend(
            _nested_problems(
                payload, described, path="", strict=_STRICT_OBJECTS.get(operation, frozenset())
            )
        )
    if operation in {"work.item.save", "work.assignee.set"}:
        if payload.get("work_ref"):
            try:
                parse_plan_node_ref(payload["work_ref"])
            except DomainError:
                problems.append({"field": "work_ref", "problem": "wrong_kind", "message": "Use the canonical plan-node URI returned by a current item read."})
        if operation == "work.item.save" and "status" not in payload and "assignee" not in payload:
            problems.append({"field": "status | assignee", "problem": "missing", "message": "Supply status, assignee, or both."})
        if "assignee" in payload and "expected_ownership_version" not in payload:
            problems.append({"field": "expected_ownership_version", "problem": "missing", "message": "A supplied assignee needs expected_ownership_version; zero when never assigned."})
        for field in ("status", "assignee"):
            if field in payload and not isinstance(payload[field], str):
                problems.append({"field": field, "problem": "type", "message": f"{field} must be a string; null is not an omission."})
        for field, minimum in (("expected_revision", 1), ("expected_ownership_version", 0)):
            if field in payload and (type(payload[field]) is not int or payload[field] < minimum):
                problems.append({"field": field, "problem": "type", "message": f"{field} must be an integer at least {minimum}."})
    if (operation == "work.status.set" and payload.get("status") not in (None, "")) or (operation == "work.item.save" and "status" in payload):
        status = canonical_work_status(payload.get("status"))
        if status not in CANONICAL_WORK_STATUSES:
            problems.append({
                "field": "status",
                "problem": "invalid",
                "message": (
                    f"status {payload.get('status')!r} is not one of "
                    + ", ".join(CANONICAL_WORK_STATUSES) + "."
                ),
            })
    return problems


def _nested_problems(
    value: Mapping[str, Any],
    described: Mapping[str, Any],
    *,
    path: str,
    strict: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Dotted names where the catalog has a nested object, and wrong types.

    ``changes["review.look_at"]`` reaches the service as an unknown field
    named ``review.look_at``: the catalog's ``review`` is an object, so the
    call is refused here with the nested form written out. A nested object
    the operation requires to be an object (``strict``) refuses null too.
    The call's payload is only read, never changed.
    """

    problems: list[dict[str, str]] = []
    dotted: dict[str, Any] = {}
    for key, child in value.items():
        name = str(key)
        head = name.split(".", 1)[0]
        if "." in name and isinstance(described.get(head), Mapping):
            dotted[name] = child
            continue
        shape = described.get(name)
        if not isinstance(shape, Mapping):
            continue
        where = f"{path}{name}"
        if child is None and where not in strict:
            continue
        if not isinstance(child, Mapping):
            problems.append({
                "field": where,
                "problem": "type",
                "message": f"{where} is an object with the fields "
                + ", ".join(sorted(shape)) + (". Null is not accepted." if child is None else "."),
            })
        else:
            problems.extend(_nested_problems(child, shape, path=f"{where}.", strict=strict))
    if dotted:
        heads = sorted({name.split(".", 1)[0] for name in dotted})
        corrected = _undotted(value, heads)
        nested = {head: corrected[head] for head in heads}
        where = path.rstrip(".") or "the payload"
        problems.append({
            "field": ", ".join(f"{path}{name}" for name in sorted(dotted)),
            "problem": "dotted",
            "message": (
                f"{', '.join(heads)} in {where} is a nested object, not dotted names: "
                f"write {json.dumps(_skeleton(nested), separators=(',', ':'))}."
            ),
            # The same fields with the call's own values, the known nested
            # heads written as objects: the part of the payload to resend.
            "corrected": json.dumps(corrected, separators=(",", ":"), ensure_ascii=False),
        })
    return problems


def _undotted(value: Mapping[str, Any], heads: list[str]) -> dict[str, Any]:
    """A copy with each dotted name under a known nested head written as an object.

    Only names whose head is one of ``heads`` (catalog nested objects) are
    nested. Every other field keeps its exact name and value, a dotted name
    the catalog does not know included: that one is the service's to judge.
    The input is not changed.
    """

    known = set(heads)
    result: dict[str, Any] = {}
    for key, child in value.items():
        name = str(key)
        parts = name.split(".")
        if len(parts) == 1 or parts[0] not in known:
            if isinstance(result.get(name), dict) and isinstance(child, Mapping):
                # Dotted names under this head came first: merge the object
                # beneath them at every depth, so no sibling is lost and the
                # result does not depend on key order.
                result[name] = _merged(copy.deepcopy(dict(child)), result[name])
            else:
                result[name] = copy.deepcopy(child)
            continue
        target = result.setdefault(parts[0], {})
        if not isinstance(target, dict):
            target = copy.deepcopy(dict(target)) if isinstance(target, Mapping) else {}
            result[parts[0]] = target
        for part in parts[1:-1]:
            existing = target.get(part)
            if not isinstance(existing, dict):
                existing = copy.deepcopy(dict(existing)) if isinstance(existing, Mapping) else {}
                target[part] = existing
            target = existing
        target[parts[-1]] = copy.deepcopy(child)
    return result


def _merged(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """``base`` with ``overlay`` merged in at every depth, overlay winning a leaf.

    The overlay holds the dotted names, so a dotted leaf wins over the same
    leaf in the nested object whichever came first in the call.
    """

    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            base[key] = _merged(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def _skeleton(value: Any) -> Any:
    """The corrected shape with each leaf shown as its field name."""

    if isinstance(value, Mapping):
        return {key: (_skeleton(child) if isinstance(child, Mapping) else f"<{key}>") for key, child in value.items()}
    return value


def _require_complete_catalog() -> None:
    missing = sorted(set(PROBLEM_BOARD_OPERATIONS) - set(PROBLEM_BOARD_OPERATION_SHAPES))
    extra = sorted(set(PROBLEM_BOARD_OPERATION_SHAPES) - set(PROBLEM_BOARD_OPERATIONS))
    if missing or extra:
        raise RuntimeError(f"operation shapes out of step with the operation catalog: missing={missing} extra={extra}")


_require_complete_catalog()

__all__ = [
    "PLAN_ITEM_CHANGE_FIELDS",
    "PLAN_ITEM_IMMUTABLE_FIELDS",
    "PROBLEM_BOARD_OPERATION_REQUIRED",
    "PROBLEM_BOARD_OPERATION_SHAPES",
    "operation_call_problems",
    "operation_contract",
    "operation_example",
    "operation_object_patterns",
    "operation_one_of",
    "operation_shape",
]
