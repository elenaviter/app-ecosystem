"""W502: the Hub's participant answers reproduce the shared vectors byte for byte (AE #608, EMain 19:27).

``packages/service-foundation/tests/fixtures/participant_answer_vectors.json``
is the one set of bytes both the Hub producer and Problem Board's verifier
load. The Hub's request digest and answer proof must equal every vector's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from service_foundation.coordination.participant_answer import verify_participant_answer

from connection_hub.delegated_credentials.cards.participant_operation import (
    ANSWER_SCHEMA, DIRECTION, answer_signature, request_digest,
)

SHARED = json.loads((Path(__file__).resolve().parents[5] / "packages" / "service-foundation" / "tests" / "fixtures"
                     / "participant_answer_vectors.json").read_text())
CONFIG = SHARED["config"]


def test_the_shared_config_is_this_producers_schema_and_direction():
    assert (CONFIG["schema"], CONFIG["direction"]) == (ANSWER_SCHEMA, DIRECTION)


@pytest.mark.parametrize("vector", SHARED["vectors"], ids=[vector["name"] for vector in SHARED["vectors"]])
def test_the_hub_reproduces_each_shared_vector(vector):
    assert request_digest(vector["request"]) == vector["request_digest"]
    answer = dict(vector["answer"])
    proof = answer.pop("receipt_proof")
    assert answer_signature(answer, secret=CONFIG["secret"], service_id=proof["service_id"],
                            timestamp=proof["timestamp"]) == proof["signature"]
    verified = verify_participant_answer(vector["answer"], schema=CONFIG["schema"], secret=CONFIG["secret"],
                                         signer_id=CONFIG["signer_id"], audience=CONFIG["audience"],
                                         direction=CONFIG["direction"], request=vector["request"], now=CONFIG["now"])
    assert verified == vector["answer"]["result"]
