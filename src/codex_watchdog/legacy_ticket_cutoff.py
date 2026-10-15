"""Apply schema2 logical ticket eviction during explicit cold preparation.

Ordinary callers use indexed current points. Only a caller which already owns
the full cold source snapshot supplies rows and original insertion order here.
No history is read, permission added, native work changed or receipt completed.
"""
from copy import deepcopy
import json
import uuid

from .recent_state import epoch

PROVIDERS = frozenset(('slack', 'lark', 'onebot'))


def cutoff_order(rows, order):
    """Never synthesize insertion rank for an unapplied legacy cutoff."""
    if order is None:
        for row in rows:
            provider = row['namespace'].split('/', 1)[0]
            if (provider in PROVIDERS and row['namespace'] == provider+'/reply-ticket-policy-v1'
                    and row['kind'] == 'provider_floors'
                    and json.loads(row['value']).get('kind') == 'legacy_ticket_cutoff'):
                raise ValueError('legacy_ticket_cutoff_order_required')
        return sorted(tuple(row[k] for k in ('namespace', 'kind', 'key')) for row in rows)
    return order


def apply_legacy_ticket_cutoffs(rows, order, *, source_schema):
    order = cutoff_order(rows, order)
    copied = deepcopy(rows)
    by_id = {tuple(row[k] for k in ('namespace', 'kind', 'key')): row for row in copied}
    ordered = [tuple(identity) for identity in order]
    if len(by_id) != len(copied) or len(set(ordered)) != len(ordered) or set(ordered) != set(by_id):
        raise ValueError('legacy_ticket_cutoff_order_invalid')
    rank = {identity: index for index, identity in enumerate(ordered)}
    cutoffs = {}
    for identity, row in by_id.items():
        provider = row['namespace'].split('/', 1)[0]
        if (provider not in PROVIDERS or row['namespace'] != provider+'/reply-ticket-policy-v1'
                or row['kind'] != 'provider_floors'):
            continue
        body = json.loads(row['value'])
        thread = row.get('thread_id')
        try:
            valid_thread = isinstance(thread, str) and str(uuid.UUID(thread)) == thread
        except (ValueError, AttributeError):
            valid_thread = False
        if (not isinstance(body, dict) or type(body.get('schema_version')) is not int
                or body['schema_version'] != 1 or not valid_thread or row['key'] != thread
                or body.get('thread_id') != thread):
            raise ValueError('legacy_ticket_cutoff_invalid')
        kind = body.get('kind')
        if kind == 'legacy_ticket_cutoff_applied' and source_schema == 3:
            continue  # Its original anchor may already have expired normally.
        if kind != 'legacy_ticket_cutoff':
            raise ValueError('legacy_ticket_cutoff_invalid')
        anchor_id = (body.get('ticket_namespace'), 'threads', body.get('ticket_key'))
        anchor = by_id.get(anchor_id)
        if (anchor is None or anchor['thread_id'] is None
                or str(uuid.UUID(anchor['thread_id'])) != thread
                or anchor['namespace'].split('/', 1)[0] != provider
                or epoch(body.get('ticket_created_at')) is None
                or anchor['created_at'] != body['ticket_created_at']):
            raise ValueError('legacy_ticket_cutoff_anchor_invalid')
        cutoffs[provider, thread] = (anchor['created_at'], rank[anchor_id])
        # A typed applied marker preserves unknown fields and current audit
        # identity while removing any future dependency on expired history.
        row['value'] = json.dumps(dict(body, kind='legacy_ticket_cutoff_applied'), sort_keys=True)
    for identity, row in by_id.items():
        if row['kind'] != 'threads' or not row['active'] or row['thread_id'] is None:
            continue
        key = (row['namespace'].split('/', 1)[0], str(uuid.UUID(row['thread_id'])))
        cutoff = cutoffs.get(key)
        if cutoff is not None:
            if not isinstance(row['created_at'], str) or epoch(row['created_at']) is None:
                raise ValueError('legacy_ticket_cutoff_ticket_invalid')
            if (row['created_at'], rank[identity]) < cutoff:
                row['active'] = 0
    return copied
