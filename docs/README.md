# car-pi documentation

**For:** anyone looking for the document that answers their question.
**You need:** nothing.
**Time:** a minute to find the right page.

This is the complete list. Every document is here.

## Start using it

| Document | Answers |
|---|---|
| [try it without a car](try-it-without-a-car.md) | Can I see this work in five minutes, with no car and no hardware? |
| [inspect a car](inspect-a-car.md) | I am standing at a car. What do I do, in what order? |
| [what it can find](what-it-can-find.md) | What will this tell me that a cheap dongle will not? |
| [troubleshooting](troubleshooting.md) | It did not work. What do I check? |

## Hardware

| Document | Answers |
|---|---|
| [what to buy](what-to-buy.md) | Do I buy the ready-made HAT, or build one? |
| [build the CAN interface](build-the-can-interface.md) | How do I build the MCP2515 board? |
| [bring up a new board](bring-up-a-new-board.md) | How do I prove it works before a car sees it? |
| [build the field unit](build-the-field-unit.md) | How do I turn a Raspberry Pi into the portable unit? |

## Going further

| Document | Answers |
|---|---|
| [older VW and Audi cars](older-vw-audi.md) | My car is a 2001–2010 VW or Audi. What is different? |
| [coding](coding.md) | I want to change a module setting. What are the rules? |
| [limits and safety](limits-and-safety.md) | What is unfinished, what can it never do, and am I allowed? |

## Reference

| Document | Answers |
|---|---|
| [command reference](commands.md) | What is the exact command for this? |
| [glossary](glossary.md) | What does DTC, PID or DID mean? |
| [definition files](definition-files.md) | How do I write a PID, a rule, or a vehicle file? |
| [contribute vehicle data](contribute-vehicle-data.md) | How do I turn my car into a definition somebody can trust? |

## Not in this folder

- [CONTRIBUTING.md](../CONTRIBUTING.md) — setting up, the `./dev` commands, and the
  invariants a pull request must not break.
- [README.md](../README.md) — what car-pi is, and the six-way router into these documents.

## Deliberately not written

Kept as a record, so these do not get added by reflex. Each was considered and rejected.

| Not written | Why not |
|---|---|
| An FAQ | Where facts go to escape ownership. A question with no home means a document is missing, not that an FAQ is |
| A protocols explainer | A textbook, not a task. The standards citations already live in the code |
| An architecture tour | Nobody arrives at a pre-alpha tool asking for one. The source tree is in CONTRIBUTING.md |
| A phone-interface page | The interface has no options. Its facts belong to the three documents that already cover them |
| A separate security page | It would split "there is no password" from "coding must not ship without one", which are the same argument |
| A roadmap or status page | Honesty is better served by "this has never run on a real vehicle" sitting next to the feature |

If you want to add a document, check it against this table first. If it belongs anyway, add it
here and to the list above.

## Next

- New here? → [try it without a car](try-it-without-a-car.md)
- Want to help? → [CONTRIBUTING.md](../CONTRIBUTING.md)
