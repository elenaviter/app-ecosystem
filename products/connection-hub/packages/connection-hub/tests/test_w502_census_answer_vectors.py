"""Pin the shared census contract to the actual Hub producer's fixture bytes.

The producer fixture includes its current transmitted Card shape. A change
to that shape must update the shared vectors too, not just self-consistency.
Fixture secrets are public synthetic test values, never credentials.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from service_foundation.coordination.participant_answer import (
    AnswerContract, request_digest, sign_participant_answer, verify_participant_answer,
)
from connection_hub.delegated_credentials.cards.census_read import (
    ANSWER_SCHEMA, DIRECTION, census_answer_signature, census_request_digest,
)

HERE = Path(__file__).resolve()
HUB_PATH = HERE.parent / "fixtures" / "w502_census_answer_vectors.json"
SHARED_PATH = HERE.parents[5] / "packages" / "service-foundation" / "tests" / "fixtures" / "census_answer_vectors.json"
FIXTURE = json.loads(HUB_PATH.read_bytes())
CONFIG = {key: value for key, value in FIXTURE["config"].items() if key != "request_schema"}


def test_shared_census_vectors_are_byte_identical_to_current_hub_vectors():
    assert SHARED_PATH.read_bytes() == HUB_PATH.read_bytes()
    assert (CONFIG["schema"], CONFIG["direction"]) == (ANSWER_SCHEMA, DIRECTION)


@pytest.mark.parametrize("vector", FIXTURE["vectors"], ids=lambda value: value["name"])
def test_current_hub_vectors_match_the_shared_census_contract(vector):
    contract = AnswerContract.CENSUS
    assert request_digest(vector["request"], contract=contract) == census_request_digest(vector["request"])
    assert census_request_digest(vector["request"]) == vector["request_digest"]
    unsigned = dict(vector["answer"])
    proof = unsigned.pop("receipt_proof")
    assert sign_participant_answer(unsigned, schema=CONFIG["schema"], secret=CONFIG["secret"],
        signer_id=CONFIG["signer_id"], timestamp=proof["timestamp"], contract=contract) == proof
    assert census_answer_signature(unsigned, secret=CONFIG["secret"], signer_id=CONFIG["signer_id"],
        timestamp=proof["timestamp"]) == proof["signature"]
    assert verify_participant_answer(vector["answer"], request=vector["request"],
        contract=contract, **CONFIG) == vector["answer"]["result"]
