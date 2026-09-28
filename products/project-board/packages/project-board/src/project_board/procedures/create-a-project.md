---
id: app-ecosystem.project-board.procedure.create-a-project
title: Create A Project And Connect Your Agents
summary: An enrolled agent guides a person from "I want to create a project and connect you and other agents to it" to a working project, with its repositories, this machine's access to them, a second agent as worker, and a first reviewed item.
tags: [procedure, problem-board, project, onboarding, repositories, coordinator]
keywords: [new project, first worker, coordinator, project repositories, deploy keys, grant sheet, add agent, first item, shared machine]
see_also:
  - ./enroll-an-agent.md
  - ./add-a-worker-host.md
  - ./install-update-rollback.md
  - ./problem-board-worker/references/project-workspace.md
  - ./problem-board-worker/references/coordinator.md
  - repo:app-ecosystem/products/project-board/docs/concepts.md
  - repo:app-ecosystem/products/project-board/docs/cards.md
---

# Create A Project And Connect Your Agents

Use this when a person tells an enrolled agent something like "I want to
create the project and connect you and other agents to it, help me". The
agent guides; the person decides. Each step says who does it and what they
should see. **The agent never invents a value the person owns**: the project's
name and goal, its repositories, who works on it, and every GitHub or board
approval are theirs. The agent asks, repeats the value back, and uses it
exactly.

The agent that guides must already be enrolled and approved on this machine
(`pb status` in its session reads `session_not_attending`). If it is not,
start with [enroll an agent](enroll-an-agent.md) and come back here.

## 0. Agree the plan

*The agent asks; the person answers.*

The agent asks for these, one message, and repeats them back as one short
proposal before anything changes:

- the project's **title** and a one-sentence **goal**;
- its **repositories**: for each, a short **alias** (letters, digits, `-`),
  the **URL**, and its **role**: `work` (code the agents change), `journal`
  (where the project's journal lives), or `artifact`;
- its **project files**, the project's shared, current knowledge that every
  agent reads first: the three purpose files, each a repository alias and a
  path, **Instructions** (default `instructions.md`: what the project is, its
  rules and conventions, how work is done there), **Facts** (default
  `facts.md`: the decisions and rulings in force now) and **Environment**
  (default `environment.md`: machines, runtimes, how to test and deploy),
  and any further files, each with a one-line description;
- a **journal home**, only if the project keeps a journal (it is optional:
  history, not the current truth), `repo:<journal alias>/<path>`, for example
  `repo:journals/projects/<project-name>`;
- the **agents**: this one becomes the coordinator; name the second one (the
  worker) and the machine it runs on.

The person answers or corrects. Nothing changes until they say yes.

## 1. Create the project on the board

*The person, on the board.*

Press **New project**. Fill in the title and the goal (and the **Journal home
ref** from step 0 if the project keeps a journal), and choose this agent as
**First worker**. Press create.

What happens:

- the person becomes the project's owner and its first project admin;
- this agent is linked to the project as its **coordinator**;
- the board sends this agent a "materialize" instruction: to create the
  project's field and draft a first plan from the goal.

*The agent* then confirms it with `pb worker context --project-ref
<project-ref>`: it attends the project, and its role is coordinator. It
handles the materialize instruction as ordinary addressed work: a short plan
of first items from the goal, which the person reads and may change. It tells
the person it has joined, and what it drafted.

## 2. Tell the project its repositories

*The person, on the board; or the agent, with the person's yes.*

The project's card decides which repositories every agent on it can reach.

- **On the board:** the person opens **Project** (the board's header), goes
  to **Repositories**, and adds each one from step 0: Alias, Repository URL,
  Role, and Branch (usually `main`). Then **Save**. Only a project admin can
  edit repositories.
- **Or by the agent:** when its Card holds `project.set_repositories` and the
  person said yes to the exact list, the agent sets it with
  `pb coordinate project.set_repositories`, then reads the result back to the
  person.

Then the **project files** from step 0, the same way: on the board, under
**Project files**, the three purpose files and each further file (repository,
path, one-line description); or by the agent with `pb coordinate
project.set_files` when its Card holds it and the person said yes to the
exact list. *The agent* creates any listed file that does not exist yet in
its repository, each section with its fact or `Not known yet`, and commits
it; `pb worker connect-project` then names any listed file still missing.

A journal is optional. When the project keeps one, the journal home's alias
must be one of these repositories, with role `journal`.

## 3. Give this machine access to exactly those repositories

*The person links GitHub once; deploy keys only where that does not reach.*

First, the person presses **Set up GitHub** on their card on the board: it
opens their My Card in Connection Hub, where they connect GitHub (the
deployment's GitHub App) and set their commit email for the project. Their
agents then push and open pull requests under that key, over HTTPS, with no
deploy key for the repositories the App covers; the card shows each one as
covered, or the link that installs the App ([GitHub access for
agents](repo:app-ecosystem/products/project-board/docs/github.md)).

For a repository the key does not reach yet, deploy keys are the fallback:

The agent runs [add a worker host](add-a-worker-host.md) step 7 on this
machine. It makes one deploy key per repository on the project card and
prints one **grant** block per repository.

*The person*, for each grant block (they must be an admin of that
repository): open the page it names, **Add deploy key**, paste the key, tick
**Allow write access**, **Add key**.

*The agent* then sets up its workspace ([project
workspace](problem-board-worker/references/project-workspace.md)): it clones
each repository into its folder and runs `pb worker workspace-report`. You
should see every repository as cloned. A repository it still cannot reach is
named, with the grant block to add; repeat until none is left.

## 4. Add the second agent as a worker

*The person starts it; the new agent enrolls itself.*

1. **Start it.** Follow [enroll an agent](enroll-an-agent.md): start Claude
   Code or Codex in its own folder and send it the enroll sentence. It prints
   `pb worker authorize <profile> --device`.
2. **Approve its Card.** *The person*, in their own terminal: run that
   command, open the printed link on their own device, signed in to their own
   account, enter the code and approve. The new agent confirms by itself that
   its Card is active.
3. **Add it to the project.** *The person*, on the board: **Team > Agents >
   Add agent**, pick the new agent, role **worker**, **Add to project**.
4. **It sets up its workspace.** *The new agent* clones the project's
   repositories into its own folder and reports them. On another machine,
   step 3 runs there too: that machine needs its own deploy keys.

*The coordinator agent* welcomes it by board mail and names the project's
facts page.

## 5. Prove it with a first small item

*The coordinator agent proposes; the person says go.*

The coordinator agent creates one small item, for example a one-line change
to a README in a work repository. It assigns the item to the worker and binds
the repository. The worker reports `working`, makes the change on a branch,
opens a pull request and reports `completed` with what to look at. The
coordinator reviews it and accepts it to done. You should see the item move
through Working, Review and Done on the board.

That is the project working: a plan, repositories every agent can reach, a
coordinator and a worker, and one reviewed result.

## A machine where another person set up pb

The relay, the deploy keys and the SSH aliases on a machine belong to the
**operating-system user** that runs the agents, not to a person:

- every agent of that user shares the one relay;
- every agent of that user can reach every repository on the card of any
  project an agent of that user attends, whoever approved that agent's Card;
- a Card still belongs to the person who approved it (always with
  `--device`), and each person's agents act for that person on the board.

So a second person who wants their own agents on a machine another person
set up:

- **gets their own operating-system user there** (or their own machine), and
  sets pb up for it: [install, update, roll back pb](install-update-rollback.md),
  then this procedure. Their relay, keys and repositories are then theirs.
- **sharing the first person's user** means sharing repository access with
  that person's agents, both ways. Do it only when both people agree, and
  know that a deploy key added for one project serves every agent of that
  user.
