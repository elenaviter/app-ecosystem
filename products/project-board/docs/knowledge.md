---
id: project-board-knowledge
title: Knowledge on the Board
summary: Search maintained knowledge, open pages, browse folders and explore related notes within an enabled project corpus.
tags: [project-board, knowledge, search, graph]
see_also:
  - ./README.md
  - ./concepts.md
  - ./operations-and-rules.md
---

# Knowledge on the Board

The project's Knowledge panel lets its members search maintained notes, open
their pages, browse folders and explore a graph of related notes. A project
offers only the corpora its administrator has enabled. Project Board and
KDCube knowledge are separate choices: switching the corpus starts a fresh
view, and results and graphs belong to the selected corpus.

Search returns up to 20 matches with a title, summary and snippet. Open a
match to read its page and follow its Related links. Related shows outgoing
links from the page; a reader may have those links even when graph browsing
is unavailable. Links keep the selected corpus. A link to a note that is
missing or not shared with you shows the same unavailable-note state.

The folder browser shows up to 50 immediate children at a time. Next loads
the next page; opening a folder starts its first page. If the folder changes
while you are paging, the browser restarts it so an old cursor cannot mix
two versions of the folder. Empty folders and folders with no notes shared
with you reveal no private names or counts.

Graph exploration starts from one note. Its counter is the number of
distinct visible neighbors, excluding the selected note itself. The view
holds up to 35 notes and 200 links, including notes added by expansions.
When more notes or links exist, the view says it was limited. Relation and
direction filters select which links to explore. A graph-permission refusal
leaves search, pages and allowed Related links available.

Long pages can be shortened to fit the reply; a truncation notice makes that
visible. A malformed answer is rejected as a whole. The panel shows a plain
state for unavailable knowledge, denied access, a missing note, an invalid
answer or an expired folder cursor. It never displays server exceptions,
credentials or physical source paths.

## Who can read

Every current person on the project can use its enabled knowledge: owner,
admin or member, including existing read-only membership roles. The Board
checks that membership on every request. Removing a person ends subsequent
reads. A worker's Card or an agent's project attendance does not grant access
to this browser API.

Membership grants the project entry point. Each knowledge service also
applies its configured audience and tool policy. Search results, folder
names, note bodies, links and counts include only notes that audience can
read. Knowledge on the Board contains maintained notes, with logical
references; project journals and raw repository source are outside it.

## Enabling a project

Knowledge starts disabled. The deployment administrator binds the approved
project to its corpora, labels and default choice, then enables each target
service with an explicit existing knowledge principal. Both settings are
required. The corpus chooser exposes labels and logical keys; people never
need to choose a service URL, credential or audience principal.

The application interface documentation describes these deployment
properties. Enabling knowledge follows the project's normal reviewed
release process. This guide describes the feature; its presence in the
documentation does not mean a particular deployment has enabled it.
