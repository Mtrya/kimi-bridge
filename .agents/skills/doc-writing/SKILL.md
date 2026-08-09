---
name: doc-writing
description: Write or edit project documentation — READMEs, install guides, command/config references, onboarding walkthroughs — in a substantive, high-density, natural style. Use when creating or revising any Markdown documentation, especially when a behavior change touches several documents at once.
---

# Doc Writing

Documentation is written for a reader executing a task, not for an auditor grading caution. Every sentence must carry information the reader can act on at the point where they read it.

## Rules

- **Every fact has one home.** State each fact once, in its authoritative document. Elsewhere, point to it or omit it. Never copy a policy sentence across files "for completeness" — repetition across documents guarantees future contradiction.
- **Default path first, exceptions briefly.** Lead with what the reader does. Attach an exception in one short clause, only at the point where the reader actually branches. Never stack three conditionals in one sentence.
- **Do not document non-events.** A tool not doing something it was never expected to do ("this command does not start the server", "no backup is created") is worth stating only when the reader would plausibly assume otherwise at that decision point. Catalogs of things that don't happen are noise.
- **Do not surface implementation details as caveats.** "Existing entries are preserved" is what a merge means. "The value below is a placeholder" is what angle brackets mean. If the reader cannot make a different decision because of the sentence, delete it.
- **State behavior declaratively.** The code guarantees it or it doesn't. No "tends to", "should generally", "supports the cautious conclusion that". Hedge only genuine uncertainty, and say what resolves it.
- **Keep real caveats — once, at the decision point.** Actionable warnings (a credential flow that does not publish the app, a bot that allows only one poller) earn their place. State them plainly, where the reader is about to act.
- **Density is not deletion.** When removing bloat, check the diff for substance that was carried along with it — operational guidance, capability summaries, real limitations — and keep it.

## Smell test

Read the paragraph aloud. If it sounds like a liability waiver, a grant proposal's limitations section, or a lab report about an ordinary lunch, rewrite it. `reference.md` shows concrete before/after pairs and the diagnosis for each.

## Cross-language writing

If the conversation language differs from the document's target language (e.g. the conversation is in Chinese but the document is English), delegate the prose to a subagent whose prompt is written entirely in the target language, and have it think and write in that language. Idiomatic prose comes from composing in the target language, not from translating sentences in your head.

The delegating agent still owns accuracy. Hand the subagent:

- the verified fact map — what the behavior is, checked against the code, not assumed;
- the single-home map — which document owns each fact and what other documents may say;
- the style rules above;
- the file list it may touch, and a ban on git mutations.

Then review the returned diff yourself: check no fact was lost, no claim drifted from the code, and no bloat survived.
