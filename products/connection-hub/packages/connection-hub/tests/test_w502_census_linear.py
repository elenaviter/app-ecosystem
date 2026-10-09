"""W502 census linearization (Main's conditions, 2026-10-09 02:42Z): early 413 and bounded concurrent reads.

Measured cause of the superlinear read: the Hub read every person of a chunk before refusing it 413
for size, and the client halved and re-read, so 502 persons cost 2002 person reads (4x) and 102 cost
204 (2x). Now the Hub keeps the answer's exact canonical size as a running total and answers the same
413 as soon as the persons alone exceed the bound, and reads persons PERSON_READ_CONCURRENCY at a time
in request order. A successful answer is byte-identical to the serial one.
"""

from __future__ import annotations

import asyncio

import pytest

from connection_hub.delegated_credentials.cards import census_read
from service_foundation.coordination.durable_wire import canonical_json_bytes
from test_card_census_read import ADMIN, OTHER, _request, _verified, _world

TOO_LARGE = {"kind": "refused", "code": "card_census_too_large", "status": 413}


def _persons(count):
    return sorted({ADMIN, OTHER} | {f"user:p{index:04d}" for index in range(max(count - 2, 0))})[:count]


def _counting(monkeypatch):
    calls = []
    original = census_read.CardCensusReadOperation._person

    async def person(self, scope, subject):
        calls.append(subject)
        return await original(self, scope, subject)

    monkeypatch.setattr(census_read.CardCensusReadOperation, "_person", person)
    return calls


async def _answer(operation, persons, *, include_catalog=True):
    request = _request(persons, include_catalog=include_catalog)
    response = await operation.answer(request)
    return _verified(response, request), response


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [3, 33, 102, 502])
async def test_concurrent_reads_answer_byte_identically_to_serial_reads(tmp_path, monkeypatch, count):
    operation, *_ = await _world(tmp_path)
    monkeypatch.setattr(census_read, "MAX_ANSWER_BYTES", 64 * 1024 * 1024)  # compare whole answers at every size
    persons = _persons(min(count, census_read.MAX_PERSONS))
    monkeypatch.setattr(census_read, "PERSON_READ_CONCURRENCY", 1)
    serial, _ = await _answer(operation, persons)
    monkeypatch.setattr(census_read, "PERSON_READ_CONCURRENCY", 16)
    concurrent, _ = await _answer(operation, persons)
    assert serial["kind"] == "census" and [entry["person"] for entry in serial["persons"]] == sorted(persons)
    assert canonical_json_bytes(concurrent) == canonical_json_bytes(serial)


@pytest.mark.asyncio
async def test_results_keep_request_order_whatever_the_completion_order(tmp_path, monkeypatch):
    operation, *_ = await _world(tmp_path)
    original = census_read.CardCensusReadOperation._person
    persons = _persons(20)

    async def late_first(self, scope, subject):
        await asyncio.sleep(0.002 * (len(persons) - persons.index(subject)))  # earlier persons finish last
        return await original(self, scope, subject)

    monkeypatch.setattr(census_read.CardCensusReadOperation, "_person", late_first)
    result, _ = await _answer(operation, persons)
    assert [entry["person"] for entry in result["persons"]] == persons


@pytest.mark.asyncio
@pytest.mark.parametrize("include_catalog", [False, True])
async def test_the_bound_is_exact_one_byte_under_fits_one_byte_over_refuses(tmp_path, monkeypatch, include_catalog):
    operation, *_ = await _world(tmp_path)
    persons = _persons(40)
    monkeypatch.setattr(census_read, "MAX_ANSWER_BYTES", 64 * 1024 * 1024)
    result, response = await _answer(operation, persons, include_catalog=include_catalog)
    unsigned = {key: value for key, value in response["census_answer"].items() if key != "receipt_proof"}
    exact = len(canonical_json_bytes(unsigned))
    monkeypatch.setattr(census_read, "MAX_ANSWER_BYTES", exact)
    fitting, _ = await _answer(operation, persons, include_catalog=include_catalog)
    assert fitting == result  # the answer at exactly the bound is the same census, never refused
    monkeypatch.setattr(census_read, "MAX_ANSWER_BYTES", exact - 1)
    refused, _ = await _answer(operation, persons, include_catalog=include_catalog)
    assert refused == TOO_LARGE


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 16])
async def test_an_answer_that_cannot_fit_is_refused_before_every_person_is_read(tmp_path, monkeypatch, concurrency):
    operation, *_ = await _world(tmp_path)
    monkeypatch.setattr(census_read, "MAX_ANSWER_BYTES", 2048)
    monkeypatch.setattr(census_read, "PERSON_READ_CONCURRENCY", concurrency)
    calls = _counting(monkeypatch)
    persons = _persons(120)
    result, _ = await _answer(operation, persons)
    assert result == TOO_LARGE  # the same refusal the final check signs
    assert len(calls) < len(persons) and len(calls) <= max(concurrency, 16) + 16
    assert calls == persons[:len(calls)]  # read in request order, from the start


@pytest.mark.asyncio
async def test_identities_are_checked_for_every_person_before_any_card_is_read(tmp_path, monkeypatch):
    """A person whose identity refuses (here the LAST one) refuses 400 with no Card read at all."""
    operation, *_ = await _world(tmp_path)
    calls = _counting(monkeypatch)
    persons = _persons(40)
    original = census_read.CardCensusReadOperation._identity

    def identity(scope, person):
        if person == persons[-1]:
            raise census_read._Refused("card_census_person_invalid", 400)
        return original(scope, person)

    monkeypatch.setattr(census_read.CardCensusReadOperation, "_identity", staticmethod(identity))
    result, _ = await _answer(operation, persons)
    assert result == {"kind": "refused", "code": "card_census_person_invalid", "status": 400}
    assert calls == []


@pytest.mark.asyncio
async def test_the_first_failing_person_in_request_order_decides_the_refusal(tmp_path, monkeypatch):
    operation, *_ = await _world(tmp_path)
    original = census_read.CardCensusReadOperation._person
    persons = _persons(40)
    first, later = persons[5], persons[9]  # both in the first window of 16

    async def failing(self, scope, subject):
        if subject == later:
            raise census_read._Refused("card_census_later_failure", 409)
        if subject == first:
            await asyncio.sleep(0.01)  # fails LAST in time, FIRST in request order
            raise RuntimeError("store unavailable")
        return await original(self, scope, subject)

    monkeypatch.setattr(census_read.CardCensusReadOperation, "_person", failing)
    result, _ = await _answer(operation, persons)
    assert result == {"kind": "refused", "code": "card_participant_unavailable", "status": 503}
