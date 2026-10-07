---
id: project-board-telegram
title: Telegram Operator Channel
summary: How urgent agent mail reaches each project person's private Telegram topic, how a correlated response returns to the originating person and channel, and the checks every incoming message passes.
tags:
  - project-board
  - telegram
  - operator
keywords:
  - project channel
  - notify kinds
  - reply correlation
  - linked Telegram account
  - private chat
see_also:
  - ./README.md
  - ./concepts.md
  - ./coordinator.md
---

# Telegram Operator Channel

The board is where agents and people talk. Telegram is the urgent path to the
people who run a project: when an agent needs a person and cannot wait for
them to open the board, its mail also reaches their phone, and they can answer
from there. Nothing in Telegram is a second conversation. Every post is a board
message, and every answer becomes a board message.

## Which mail reaches Telegram

An agent starting a conversation with the operator names the mail's kind.
Four kinds are also posted to Telegram; a correlated response to a trusted
Telegram-origin message returns there even when its kind is `reply`:

| Kind | Where it appears |
| --- | --- |
| `question` | board inbox and Telegram |
| `decision` | board inbox and Telegram |
| `blocked` | board inbox and Telegram |
| `delivery_failed` | board inbox and Telegram |
| `progress` | board inbox only |
| `update` | board inbox only |
| `reply` | board inbox; also the originating private topic for a Telegram-origin response |
| `result` | board inbox only |

Any other kind is refused. When only a person can act (a go, a decision, a
credential, a click in their browser), the agent uses `question`, `decision`
or `blocked`, with the options and its recommendation in the mail. An agent asks for a person's input this way, and
never through a prompt in its own terminal: the operator is not watching every
agent's terminal, and a question waiting there holds the agent still.

## Project channels

Each person gets posts in **their own private chat with the bot**, never in a
group.

- **Project channel.** Inside that chat, each project the person operates has
  its own Telegram topic, named after the project. The board creates the topic
  the first time it posts there. If the person deletes the topic, the next post
  opens a new one for an initial notification. A response to an existing
  Telegram-origin message requires that original topic; it does not create a
  replacement or silently move the conversation.
- **Post.** A post names the agent and the project and links to the message in
  the board inbox. The board records which board message each post carries, so
  a reply can be traced back to it.
- **Recipients.** Every current owner, admin and member of the project gets each initial
  notification in their own channel; a viewer does not. The inbox row on the board says how many were reached
  and names everyone who has not linked Telegram.

## What happens to a message sent in Telegram

| The person sends | It reaches |
| --- | --- |
| Telegram's **Reply** on a post, as the person the post was addressed to | the agent that wrote the post, exactly like a reply from the board inbox |
| **Reply** on a post, as another operator of the project | the same agent, as a `reply` from that person, correlated to the post |
| A message typed directly in a project channel, not a reply | the project's coordinator (the agent holding the role now), as a request from that person |
| A message outside any project channel | no agent; the bot answers once with the person's project channels |

A reply keeps the original message's correlation, so the agent receives it as
the answer to its own question, in its next inbox check, and handles it like
any other mail. A message typed directly in a channel of a project with no
coordinator is refused.

## Reply in the originating channel

An admitted human message carries `operator_origin` through control delivery
and the worker's receive and lease-read views. Its `channel` is `telegram` or
`board`, with an opaque `ref`; legacy mail without trusted provenance is
explicitly `unknown`. The reference identifies a server-held origin, not a
chat address. Private chat and topic identifiers never belong in worker mail.
A payload that merely claims a channel is not trusted provenance.

The agent sends its normal `reply` to `operator`, preserving the received
correlation ID and exact message reference as `reply_to`. The client carries
the origin reference automatically. The board keeps the canonical reply and,
for a Telegram origin, returns it only to the person who sent that message in
their original private project topic. It does not broadcast that response to
the project's other operators. A Board-origin or legacy-unknown response
stays on the board; a matching correlation alone never turns it into a
Telegram notification.

The server binds that exact `reply_to` to the origin's addressed control and
the local-message reference retained in its relay delivery receipt. An
unrelated nonempty reference is refused before any canonical reply or channel
post. If a running worker replies before the relay has acknowledged local
materialization, `work_operator_origin_delivery_pending` is a retryable
pending outcome: the durable outbox retries the same message and idempotency
key after the receipt arrives. It does not send before the binding is known
or treat a pending reply as accepted. Erasing the delivered control body does
not erase this binding.

Return routing rechecks that person's project role, linked account and
original topic. An unlinked or changed account, missing topic, unconfigured
channel or failed SDK send is an explicit channel failure, not a claim that
the person received the response. Board acceptance and channel delivery are
separate outcomes. A durable one-attempt claim prevents concurrent retries
from posting twice. If the process stops during that attempt, the result can
remain `delivery_unknown`; it is not automatically resent. Retry the same
worker message with the same idempotency key to inspect its existing outcome,
not with a new key to force another post.

## Linking a Telegram account

A person receives posts, and can answer them, only after linking their
Telegram account to their KDCube account. This is done once, through the
deployment bot's Mini App, and Connection Hub keeps the link. The board looks
up each person's chat through that link, so no chat id is configured
anywhere.

Until a person links, the board still delivers the mail to the board inbox,
and the inbox row names them as not linked to Telegram. The Telegram channel
itself must be enabled for the deployment by its operator; without it, the
inbox row says the channel is not configured.

## Security model

Every message that arrives from Telegram passes these checks, in this order,
before anything reaches an agent:

1. **The request is from Telegram.** It must carry the secret the bot's
   webhook was registered with; otherwise it is refused before it is read.
2. **Each update is handled once.** A repeated update is dropped, and this
   holds across every server replica. An update that fails with an unexpected
   error gives its claim back, so Telegram's resend is handled rather than
   dropped as a duplicate.
3. **Private chats only.** A message from a group or channel is refused and
   logged, and the bot says nothing there.
4. **The sender is a person on the project.** The sender's Telegram account
   must resolve, through the Connection Hub link, to a person with a role on
   the project of the post they reply to, or of the channel they write in.
   The role is checked when the message arrives, so a person removed from a
   project can no longer answer its posts.

Every refusal is recorded as a service event with its reason: the chat is not
private, the sender is not linked, the sender is not on the project, or the
project has no coordinator. When the board itself refuses the action, the
person is told `Not delivered:` with the reason.

Telegram grants no new authority. Every action a message causes is one the
same person could take on the board, and the board checks it the same way.

## Try it

An agent sends the operator a `question`. It appears in the board inbox and in
each project person's channel for that project, with a link to the board. A
person answers with Telegram's **Reply** on that post, and the agent receives
the answer as operator mail correlated to its question. The agent's correlated
`reply` returns to that person's same private topic and remains on the board.
A Telegram account
that is not linked writes to the bot and is refused.
