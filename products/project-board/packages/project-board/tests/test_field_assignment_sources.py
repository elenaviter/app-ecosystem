"""The field records every repository an assignment binds (W278).

The notice carries ``assignment.sources`` beside the scalar ``source``; a
service that predates the list sends the scalar alone, which reads as one entry.
"""

from __future__ import annotations

from project_board.client.store import _assignment_sources


def test_the_list_is_recorded_normalized_and_the_scalar_mirrors_its_first_entry():
    assignment = {
        "source": {"repository_ref": "repo:kdcube-ai-app/app", "base_commit": "947238921", "branch": "work/w212"},
        "sources": [
            {"repository_ref": "repo:kdcube-ai-app/app", "base_commit": "947238921", "branch": "work/w212"},
            {"repository_ref": "repo:app-ecosystem/products", "base_commit": "83F6D21AB", "branch": ""},
        ],
    }
    sources = _assignment_sources(assignment, repository_ref="repo:kdcube-ai-app/app")
    assert [entry["repository_ref"] for entry in sources] == ["repo:kdcube-ai-app/app", "repo:app-ecosystem/products"]
    assert sources[1]["base_commit"] == "83f6d21ab"


def test_a_notice_without_the_list_reads_its_scalar_source_as_one_entry_and_nothing_as_none():
    scalar_only = {"source": {"repository_ref": "repo:applications/playground", "base_commit": "f7329086c", "branch": "work/w1"}}
    assert _assignment_sources(scalar_only, repository_ref="repo:applications/playground") == [
        {"repository_ref": "repo:applications/playground", "base_commit": "f7329086c", "branch": "work/w1"}
    ]
    assert _assignment_sources({"source": {}}, repository_ref="") == []
    assert _assignment_sources({"sources": [{"repository_ref": ""}, "junk"]}, repository_ref="") == []
