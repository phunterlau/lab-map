# Milestone 5 design: LLM graph-diff extraction

**Status:** design, not yet implemented. Grounded in a real evaluation
pass (below), not designed in the abstract.

## Where this came from

The user's framing: "usually important decision happens when agent says
something to user and user answers something, and makes a few choices...
easy to forget the other [options]... sometimes user digs further with
agent on a particular point... not yet branches... users can start a new
task." That's a real taxonomy of conversational "moments," and it's
exactly what an LLM extractor needs to recognize to do Milestone 5 well.

To ground it in something more than plausible-sounding categories, a
`fork` agent did a full read-only review of the two real KGW/BPE-watermark
research rollout files (~170MB, 231 user turns + hundreds of assistant
turns across two physical files spanning a month) and cross-checked them
against the 13-node graph manually built earlier this session
(`local/kgw-example/build.py`). Findings, now applied to that graph
(17 nodes as of this doc):

- 11 of 13 existing nodes' byte citations checked out cleanly against real
  text. Two were wrong: one node's citation was chronologically backwards
  (pointed at a reflection turn instead of the actual attempt+result), and
  one causal edge connected two experiments that turned out to be
  unrelated but chronologically adjacent -- a real precision failure, not
  a style nit.
- Recall was worse than precision: the graph covered roughly the first
  half of the research by volume and almost none of the second physical
  file, missing the single most scientifically significant result in the
  whole transcript (the first developmental-test outputs to cross the
  detection threshold), a full parallel research branch the researcher
  explicitly asked to have modeled *as* a branch, a clean rejected
  dead-end experiment, and an explicit "park this, it's not paying off"
  deferral -- exactly the `revisit_condition` type existing in the
  ontology since the start but never actually used until this review
  forced it.
- The build plan calls "Rejected Alternative Recall" the metric that
  matters most (section 23) because forgetting dead branches is the
  original problem. This review is a direct proof of that: rejections
  were the easiest thing to lose.

Citations below are `FILE @ byte_start: what happened`, paraphrased, not
quoted -- matching this session's standing rule that real transcript
content doesn't get reproduced verbatim in anything git-tracked.

## The moment taxonomy

Seven patterns, each with a real example from the review. This is the
extraction rubric the LLM prompt should encode directly, not a generic
"extract decisions" instruction.

### 1. OFFER_AND_PICK (true alternatives)

Agent proposes a small set of **mutually exclusive** approaches; user
selects one. The non-picked ones get `option` nodes with status `dormant`
(never `rejected` -- nothing was said against them, they just weren't
picked) and no `CHOSEN_OVER` edge is asserted unless the user explicitly
ruled the others out.

Real example: `FILE1 @ 39258013`: an external consultant proposed a named
next-phase plan combining several techniques; the user proceeded with it
as a whole rather than picking sub-parts, so this graph currently models
it as one `option` node rather than decomposing -- a real design tension,
noted in "open questions" below.

### 2. SEQUENCE_PICK (ordered work items -- NOT a fork)

Surface form identical to #1 (agent lists several named items, user
starts with one), semantics completely different: the items are
**sequential milestones/phases**, not competing alternatives. Picking
#1 orders the work; it doesn't reject #2 and #3. These should get
`option` nodes with status `open`/`candidate` (queued, not dormant-as-in-
deprioritized) and no `CHOSEN_OVER` edge at all -- asserting one would
misrepresent "we'll get to it" as "we ruled it out."

This is the exact case the user's own framing described ("agent suggests
3 milestones, user picks milestone 1... easy to forget the other 2") and
it is the single most important distinction for the extractor to get
right, because getting it wrong in either direction is bad: flattening
SEQUENCE_PICK into OFFER_AND_PICK wrongly marks queued work as rejected;
flattening OFFER_AND_PICK into SEQUENCE_PICK loses genuine rejections
(exactly Rejected Alternative Recall failing).

Discriminator for the prompt: does the assistant's framing use ordering
language ("phase," "milestone," "first," "then") and does later text
return to the un-picked items as still-planned work? Or does it use
either/or language ("Option A vs Option B," "instead of," "rather than")
and do the un-picked items never come back up unless explicitly revisited?

### 3. DEEP_DIVE (elaboration -- not a branch)

User asks for more detail, runs more experiments, or iterates on **one
already-named** option/decision across several turns. Must not spawn a
new option/decision node.

Discriminator, sharper than "same topic": **does this turn name an
alternative that doesn't already exist in the graph neighborhood passed
into the prompt** (build plan section 14, input A)? Elaboration adds
detail to something already there; branching names a second thing that
wasn't there before. This is checkable mechanically against the
neighborhood context the extractor already receives, and it doubles as
the duplicate-suppression check section 15 already wants -- one
comparison, two jobs.

### 4. NEW_GOAL (new top-level thread)

User states an objective that doesn't reference the current active
goal/thread. New `goal` node, not force-nested under whatever was active.

Partly structural rather than semantic: a `SessionStart` hook event, an
explicit plan-mode entry, or (in Codex) a `task_started` `event_msg` whose
following user turn shares no named entities with the current graph
neighborhood are all cheap signals usable before an LLM call.

### 5. REJECTION (explicit, with reason)

Something previously `exploring`/`chosen` gets explicitly ruled out, with
a stated reason. Requires the reason -- an extractor should not infer
rejection from silence or from an option simply not coming up again (that
silence is far more often #2/SEQUENCE_PICK than #5).

Real example: `FILE1 @ 79182083`: an experiment's own result (44 rewrites,
42 requiring an unsound justification, watermark still detected in all of
them) is followed one turn later, `FILE1 @ 80628731`, by the assistant
explicitly naming it a dead end and describing what the next approach
does instead. That adjacency -- result, then explicit-dead-end framing --
is a strong structural signal, not just a keyword match.

### 6. REVISIT / PARK (explicit deferral)

Something is explicitly set aside with a stated condition or reason, not
rejected outright. `revisit_condition` node, `REVISIT_WHEN` edge to the
thing being deferred.

Real example: `FILE2 @ 58497810`: a specific sub-approach (sign-estimator
tuning, one component of an otherwise-still-active option) is explicitly
"parked" as not showing gains, while the rest of that option continues.
Note this is *finer-grained* than the option itself -- the extractor needs
to be able to attach a revisit condition to part of an option's scope, not
just the whole node, or it either loses the nuance or wrongly downgrades
the entire option.

### 7. EXPLICIT_PARALLEL_BRANCH (user-initiated fork)

Distinct from #1: the user, not the agent, explicitly asks to explore a
new direction **as a branch**, with its own plan, while keeping the
current line active -- not a choice between alternatives, a deliberate
fork.

Real example: `FILE2 @ 41198814`: the user explicitly asked to explore a
new sub-direction as a branch from the main idea, with its own
milestones/goals plan. This is as close to ground truth for "branch" as
the ontology's `RELATED_TO`/`EXPLORES` edges get -- the extractor should
treat explicit branch/fork language from the user as a strong, almost
deterministic signal, worth its own cheap pre-filter pattern rather than
relying on general semantic judgment.

(SUPERSESSION -- a later "final" decision overturning an earlier one --
appears in the build plan's ontology already and is real in this corpus
too, but didn't surface as its own clean example in the review; treating
it as an outcome of the periodic reconciliation pass below rather than a
per-window moment type, per the build plan's original section 16 design.)

## Pipeline architecture

Two tiers, matching the build plan's existing section 13/14 (incremental)
vs. section 16 (periodic reconciliation) split -- this taxonomy sharpens
what goes in the prompt, it doesn't change the architecture:

```
cheap deterministic pre-filter
  (numbered/bulleted list in an assistant turn;
   explicit branch/park/revisit/instead-of language;
   session/goal-boundary structural signals)
        |
        v  (only flagged windows go further)
sliding window of recent turns + graph neighborhood
  (build plan section 14 inputs A-E)
        |
        v
LLM extraction, prompted with the 7-type taxonomy above,
  explicitly distinguishing OFFER_AND_PICK vs SEQUENCE_PICK,
  and DEEP_DIVE vs NEW_BRANCH via the neighborhood-match test
        |
        v
graph mutations only (not summaries) -- validated against
  the ontology enforcement already built:
  graph.repository.add_node/add_edge require evidence_event_ids
  (MissingProvenanceError) and reject unknown types/statuses
  (InvalidOntologyError) -- this is a real safety net already
  in place, not something Milestone 5 needs to add
        |
        v
periodic reconciliation pass (build plan section 16):
  supersession, dormant-option revival, duplicate merge --
  NOT per-window, deliberately less frequent
```

The cheap pre-filter is not hypothetical: the review fork used exactly
this pattern by hand (keyword + structural triage over 231+ turns down to
~15 worth reading in full) and it worked -- every citation it produced
checked out against real text. That's direct evidence the two-tier
approach is viable before spending an LLM call on every turn.

## Evaluation

The build plan's section 23 evaluation plan already names "Rejected
Alternative Recall" as the metric that matters most. This session's
cross-check *is* a working instance of that evaluation methodology:
build/extract a graph, then independently review the full corpus and
compare. Once Milestone 5 exists, the same process (an independent
review pass against the LLM's output, not just against a human-built one)
is the repeatable eval loop -- no new methodology needed, just automating
the "build" side of what was done by hand this session.

## Open questions (for the user, not resolved here)

- **Decomposition granularity for OFFER_AND_PICK**: `FILE1 @ 39258013`
  modeled an external consultant's multi-part plan as one `option` node.
  Should the extractor decompose a compound proposal into separate
  sub-options, or is one node per proposal the right grain? Affects graph
  size a lot; no clear answer from the review alone.
- **Extraction cadence**: every `Stop` (build plan V1 hook list), or
  batched less often? The pre-filter reduces LLM calls either way, but
  the cadence question is about latency/staleness tradeoffs, not cost.
- **SEQUENCE_PICK node fate**: should un-started sequence items get graph
  nodes at all (status `open`), or is that scope creep toward a task
  tracker rather than a decision graph? Leaning toward "yes, node them" --
  losing them is exactly the failure mode the user described -- but this
  is a real product-scope call.
