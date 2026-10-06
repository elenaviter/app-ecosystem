Part of [collaboration](../collaboration.md).

## Rule 12. Cards are edited only in Connection Hub

An application links to a Card in Connection Hub, optionally with a
preselection or a focus for the purpose the link serves. It never builds its
own Card editor, and it never keeps a stored copy of the decision that it
writes back over the Card. Connection Hub holds the decision. Grouping of
operations (for Problem Board: Review, Work, Plan, People) is declared with
the operations in the service catalog, so Connection Hub groups them for every
client. Why: a second editor in the board kept its own copy and overwrote
edits made in Connection Hub, and every client would have had to build its
own grouping. The operator's ruling (2026-09-26): "it must be cards in the
connection hub ... please do not build new interface for this, this is simply
wrong! and does not scale."
