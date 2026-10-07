"""W578: an account disconnect's fence against a concurrent new binding of that account.

A disconnect inside one Card transaction stages every Card that binds a
connected account (``account_scope[provider][account_id]``) and deletes the
account after COMMIT. A Card that gains the binding between the listing and the
decision would survive the disconnect and revive on reconnect, since account
ids are deterministic. No per-Card read can fence that: the new binding lands
on a Card that was not read. So the account itself is fenced, with the
``catalog_reserved`` ordering (both sides write first, then read the other's
mark), in the Card store root:

    account-fences/<key>/fence.json                {transaction_id}
    account-fences/<key>/pending/<mark id>.json    {kind, subject_hash, access_id, transaction_id}
    account-fences/by-transaction/<sha256(tx)>.json {transaction_id, accounts}

with ``<key>`` = sha256(provider NUL account_id).

- A writer that ADDS the account to a Card writes its mark first, then reads
  the fence. A fence of another transaction that is not terminal (fresh or
  prepared alike) refuses it (``card_account_reserved``, retryable) after it
  removed its mark. A writer never judges a fence stale.
- The disconnect writes its per-transaction index, then the fence, then reads
  the marks. A live mark refuses it (``card_account_binding_in_progress``,
  retryable) after it released its fence. Only then does it list the Cards.

In any interleaving at least one side sees the other. Liveness is never a
matter of age:

- a ``direct`` mark is written and removed inside its Card's mutation section,
  so it is live while that section is held. The disconnect probes the section
  with the composition's own mutation lock (``probe``): acquired means the
  writer is gone and the mark is removed; busy means it refuses;
- a ``staged`` mark lives until its own transaction is terminal (a decided
  receipt or an ABORT tombstone), then it is removed;
- a fence lives until it is released: after its transaction's COMMIT AND the
  account deletion effect were applied and the group finished (a committed
  decision alone does not end it, or a binding could land before the account
  is deleted), or on its ABORT. A fence of an aborted transaction (receipt or
  tombstone) no longer blocks, and the presumed-abort path releases a fence a
  crashed, never-prepared disconnect left behind, found through its index.

The lock is the store's own mutation lock, so this fence reaches exactly as far
as every Card write does (the store's declared ``lifecycle_lock_scope``).
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping

from ..durable_io import read_json_or_none, write_json_atomic
from .model import CardAuthority

FENCES_DIRNAME = "account-fences"
_BY_TRANSACTION = "by-transaction"

# probe(subject_hash, access_id) -> True when that Card's mutation section is held right now
SectionProbe = Callable[[str, str], Awaitable[bool]]


class AccountFenceRefused(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def account_key(provider_id: str, account_id: str) -> str:
    return hashlib.sha256(f"{provider_id}\0{account_id}".encode("utf-8")).hexdigest()


def bound_accounts(authority: CardAuthority | None) -> set[tuple[str, str]]:
    """Every (provider, account) the Card's ``account_scope`` binds."""
    if authority is None:
        return set()
    return {(str(provider), str(account)) for provider, accounts in dict(authority.account_scope or {}).items()
            for account in dict(accounts or {})}


def added_accounts(current: CardAuthority | None, candidate: CardAuthority) -> set[tuple[str, str]]:
    """The accounts ``candidate`` binds that ``current`` does not."""
    return bound_accounts(candidate) - bound_accounts(current)


def _dir(store: Any, provider_id: str, account_id: str):
    return store.root / FENCES_DIRNAME / account_key(provider_id, account_id)


def _index_path(store: Any, transaction_id: str):
    name = hashlib.sha256(str(transaction_id).encode("utf-8")).hexdigest()
    return store.root / FENCES_DIRNAME / _BY_TRANSACTION / f"{name}.json"


def _remove(path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


async def transaction_terminal(store: Any, transaction_id: str) -> bool:
    """A decided receipt or an ABORT tombstone; a prepared, unstaged or absent one is not terminal."""
    from .transaction_store import read_receipt, tombstone_path

    if await read_json_or_none(tombstone_path(store, transaction_id)) is not None:
        return True
    receipt = await read_receipt(store, transaction_id)
    return receipt is not None and receipt.get("state") not in ("prepared", "unstaged")


async def transaction_aborted(store: Any, transaction_id: str) -> bool:
    """An ABORT is recorded (tombstone or aborted receipt): nothing of that transaction can still land."""
    from .transaction_store import read_receipt, tombstone_path

    if await read_json_or_none(tombstone_path(store, transaction_id)) is not None:
        return True
    receipt = await read_receipt(store, transaction_id)
    return receipt is not None and receipt.get("state") == "aborted"


async def _blocking_fence(store: Any, provider_id: str, account_id: str, *, own: str) -> str:
    """The transaction id of another transaction's fence on this account that still blocks ("" when none).

    Present means blocking, whether its transaction is fresh, prepared or
    committed with its effects not yet applied; only a recorded ABORT ends it
    without a release.
    """
    raw = await read_json_or_none(_dir(store, provider_id, account_id) / "fence.json")
    if not isinstance(raw, Mapping) or type(raw.get("transaction_id")) is not str:
        return "unreadable" if raw is not None else ""
    holder = raw["transaction_id"]
    if holder == own or await transaction_aborted(store, holder):
        return ""
    return holder


async def mark_binding(store: Any, accounts: Iterable[tuple[str, str]], *, mark_id: str, kind: str,
                       subject_hash: str, access_id: str, transaction_id: str = "") -> None:
    """Write this writer's mark on every account it adds, then refuse any non-terminal fence of another transaction.

    ``kind`` is ``direct`` (removed before the Card's section is left) or
    ``staged`` (removed when ``transaction_id`` is decided). On refusal every
    mark this call wrote is removed.
    """
    chosen = sorted(set(accounts))
    written = []
    try:
        for provider_id, account_id in chosen:
            path = _dir(store, provider_id, account_id) / "pending" / f"{mark_id}.json"
            await write_json_atomic(path, {"kind": kind, "subject_hash": subject_hash, "access_id": access_id,
                                           "transaction_id": transaction_id})
            written.append(path)
        for provider_id, account_id in chosen:
            if await _blocking_fence(store, provider_id, account_id, own=transaction_id):
                raise AccountFenceRefused("card_account_reserved")
    except BaseException:
        for path in written:
            _remove(path)
        raise


def clear_binding(store: Any, accounts: Iterable[tuple[str, str]], *, mark_id: str) -> None:
    for provider_id, account_id in set(accounts):
        _remove(_dir(store, provider_id, account_id) / "pending" / f"{mark_id}.json")


@asynccontextmanager
async def binding_mark(store: Any, current: CardAuthority | None, candidate: CardAuthority, *,
                       subject_hash: str) -> AsyncIterator[None]:
    """A direct write's mark, held around its durable commit inside the Card's section."""
    accounts = added_accounts(current, candidate)
    if not accounts:
        yield
        return
    mark_id = "direct-" + uuid.uuid4().hex
    await mark_binding(store, accounts, mark_id=mark_id, kind="direct", subject_hash=subject_hash,
                       access_id=candidate.access_id)
    try:
        yield
    finally:
        clear_binding(store, accounts, mark_id=mark_id)


async def _live_marks(store: Any, provider_id: str, account_id: str, *, probe: SectionProbe) -> bool:
    """Whether any mark on this account is live; a mark whose writer is provably gone is removed."""
    pending = _dir(store, provider_id, account_id) / "pending"
    if not pending.is_dir():
        return False
    live = False
    for path in sorted(pending.glob("*.json")):
        mark = await read_json_or_none(path)
        if not isinstance(mark, Mapping):
            continue  # removed meanwhile
        if mark.get("kind") == "staged" and type(mark.get("transaction_id")) is str and mark["transaction_id"]:
            if await transaction_terminal(store, mark["transaction_id"]):
                _remove(path)
            else:
                live = True
        elif mark.get("kind") == "direct" and type(mark.get("subject_hash")) is str \
                and type(mark.get("access_id")) is str:
            if await probe(mark["subject_hash"], mark["access_id"]):
                live = True
            elif await read_json_or_none(path) is not None:
                # The section was free: no writer holds it, so this mark outlived its writer.
                _remove(path)
        else:
            live = True  # an unreadable mark is never assumed dead
    return live


async def reserve_accounts(store: Any, accounts: Iterable[tuple[str, str]], *, transaction_id: str,
                           probe: SectionProbe) -> None:
    """Fence every account for this transaction, then refuse any live mark of a binding in progress."""
    chosen = sorted(set(accounts))
    for provider_id, account_id in chosen:
        if await _blocking_fence(store, provider_id, account_id, own=transaction_id):
            raise AccountFenceRefused("card_account_reserved")
    # The index first: a crash after any fence write leaves it findable by the presumed abort.
    await write_json_atomic(_index_path(store, transaction_id),
                            {"transaction_id": transaction_id, "accounts": [list(item) for item in chosen]})
    for provider_id, account_id in chosen:
        await write_json_atomic(_dir(store, provider_id, account_id) / "fence.json",
                                {"transaction_id": transaction_id})
    try:
        for provider_id, account_id in chosen:
            if await _blocking_fence(store, provider_id, account_id, own=transaction_id):
                raise AccountFenceRefused("card_account_reserved")  # another disconnect raced the write
            if await _live_marks(store, provider_id, account_id, probe=probe):
                raise AccountFenceRefused("card_account_binding_in_progress")
    except BaseException:
        await release_transaction(store, transaction_id)
        raise


async def release_transaction(store: Any, transaction_id: str) -> None:
    """Remove only this transaction's fences and index; a fence of another transaction is never cleared."""
    index = await read_json_or_none(_index_path(store, transaction_id))
    if not isinstance(index, Mapping) or index.get("transaction_id") != transaction_id:
        return
    for item in index.get("accounts") or ():
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        path = _dir(store, str(item[0]), str(item[1])) / "fence.json"
        raw = await read_json_or_none(path)
        if isinstance(raw, Mapping) and raw.get("transaction_id") == transaction_id:
            _remove(path)
    _remove(_index_path(store, transaction_id))


__all__ = ["AccountFenceRefused", "FENCES_DIRNAME", "SectionProbe", "account_key", "added_accounts",
           "binding_mark", "bound_accounts", "clear_binding", "mark_binding", "release_transaction",
           "reserve_accounts", "transaction_aborted", "transaction_terminal"]
