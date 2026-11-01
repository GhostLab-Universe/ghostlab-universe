# UI rethink: the refund demo experience

Decision record, 1 November 2026. Status: **proposal**. This doc and its
companion prototype shell (`web/src/demo-shell.html`) are the whole deliverable
of this track: a decision artifact plus a clickable sketch, not a console
change. No existing route, component, test, API or behavior is modified.

The current console is an honest instrument. It shows campaign status, trace
evidence, Reality Diff, approval and capsule replay with explicit origins, and
it never presents synthetic data as live. That honesty machinery is the
floor this proposal stands on. What the console does not do is tell the
incident story: its five views are organized by system module, so a first-time
viewer must assemble the narrative themselves — commit, lost timeout, naive
retry, double refund, detection, repair — from five peer tabs. This doc
proposes rearranging the demo experience around the operator's incident story
while keeping every honesty rail. Adoption, if accepted, is incremental
migration behind the current views. Never a rewrite (see section 10).

The product proof under discussion is the refund world: a synthetic INR 2,500
order (`get_order`, `issue_refund`, `get_refund_status`) where a refund commits
successfully but the agent receives a timeout; the vulnerable adapter retries
without an idempotency key and refunds twice (baseline 250000 paise, fork
500000 paise); the verifier detects the duplicate; Reality Diff identifies
event 2 as the first divergence; the repaired run refunds 250000 paise once;
and recorded replay reproduces the verdict without model calls. All prototype
content is fixture data shaped like that proof. It is not a live result.

## 1. Operator goals

The demo viewer — judge, developer, reviewer — needs four questions answered in
order. Each maps to one proposed screen (section 5):

1. **What just happened?** A plain-language incident narrative: one order, one
   commit, one lost response, one bad retry, one detection, one repair. The
   viewer should be able to retell it after sixty seconds.
2. **Why should I believe the verdict?** The exact divergence point (event 2),
   the ledger consequence (250000 vs 500000 paise), and the trace evidence
   behind both — with origins, not assertions.
3. **What am I being asked to decide, and what changes if I approve?** The
   repair under review with a before/after consequence preview beside the
   decision, not on another tab.
4. **Can I reproduce this?** Capsule plus replay framed as the proof act: run
   it again, no model call, same verdict.

Non-goal: fleet operation. The demo viewer is not triaging many campaigns, so
this proposal optimizes the single-incident path and deliberately does not
design a campaign list, search, or multi-campaign dashboard. Those remain
console concerns for later, and only if a campaign-list API exists to serve
them (none does today).

## 2. What the console does well today (keep)

- **Honesty rails.** The test-data banner, service-mode notice, "Synthetic
  world: no real people or money" label, "proposal text, untrusted" qualifiers,
  per-section origins, and coverage notices are the product's credibility. The
  proposal keeps all of them, in the same words where possible.
- **Complete proof coverage.** Baseline/fork/repaired runs, diff with first
  divergence, invariant findings, approval with idempotency keys, capsule
  export and replay — the whole refund proof is already viewable. The rethink
  is rearrangement plus a few composed views, not new capability.
- **Accessibility care.** Skip link, focus management on route change,
  `aria-live` status, keyboard-operable controls. The prototype shell copies
  these patterns (skip link, focus-to-heading, `aria-current` steps).
- **Disciplined scope.** The console does not call providers from the browser
  (CSP `connect-src` stays on-origin) and does not claim live inference it
  cannot prove. The shell goes further: its CSP sets `connect-src 'none'`.

## 3. What is hard today (observed problems)

1. **Campaign-first, id-first entry.** The home page opens campaigns by typed
   id; the incident is invisible until the viewer already knows where to look.
   For a demo, the entry point should be the incident, not a lookup form.
2. **Five peer tabs hide the story order.** Campaign / Evidence / Reality Diff /
   Approval / Capsule are system modules. Nothing tells a first-time viewer
   the intended path (overview, then diff, then evidence, then approval, then
   capsule), so demos become guided tours of the tab bar.
3. **The verdict is assembled by the reader.** The outcome notice lives on
   overview, findings on evidence, divergence on diff. The single sentence the
   demo must land — "the retry double-refunded 500000 paise; the verifier
   caught it at event 2" — exists nowhere as a sentence.
4. **The Diff page demands a mental join.** Pick an experiment, hold two runs
   in mind, map event ids to evidence links. The page is correct and complete,
   but the *meaning* of the divergence (what the extra refund cost, which
   invariant broke) is split across diff and evidence.
5. **The approval decision is separated from its justification.** The repair's
   consequence — the ledger before and after — is not beside the Approve
   button. An approval without a consequence preview is a blind signature.
6. **Capsule/replay reads as export plumbing.** "Download artifact" undersells
   the product's done-state. Replay is the proof act ("run it again, no model,
   same verdict") and should be presented as one.

## 4. Proposed experience: the incident-story spine

One incident, five beats in fixed narrative order, under a persistent incident
header. The beats are sequential (Previous/Next plus a step indicator) and
directly reachable (tabs); every beat links forward to the next question it
raises ("see the trace", "what does this cost", "decide the repair").

- **S1 Incident** — what happened, in story order, ending in the verdict
  sentence.
- **S2 Divergence** — where the worlds separate (event 2) and what it cost.
- **S3 Evidence** — the trace and findings that prove S1 and S2.
- **S4 Decision** — the repair under review, with before/after consequence.
- **S5 Proof** — capsule plus replay: reproduce the verdict without a model.

The persistent header anchors every beat: order id, the three ledger totals
(baseline 250000 / fork 500000 / repaired 250000 paise), the verdict tag, the
inference mode, and the synthetic-world label. Today those facts are one
screen apart from where they are needed; the header keeps them in view while
the viewer moves through the story.
## 5. Screen-by-screen proposal

**S1 — Incident ("What happened").** A plain-language timeline in story order:
order placed (synthetic INR 2,500); refund committed; timeout response lost;
vulnerable retry without idempotency key; second refund committed; verifier
flags duplicate-refund and total-refund invariants; idempotent repair applied;
repaired run refunds once. Below the timeline, the three ledger totals and the
verdict sentence. Ends with one forward link: "Show me where they separate"
(S2). No tabs-to-hunt: the whole incident fits one screen.

**S2 — Divergence ("Where they separate").** The first-divergence callout
(event 2: `issue_refund` attempt 1 — committed in the world, response lost),
the cause in one line (timeout after commit plus retry without an idempotency
key), and the ledger consequence table (baseline 250000 paise vs fork 500000
paise, delta +250000). Forward link: "Show me the trace" (S3, focused at
events 2–3). This beat owns the *meaning* of event 2; the console splits it
across two pages today.

**S3 — Evidence ("Prove it").** The fork trace as a table (event, tool, key
arguments, result, finding flags) plus the finding list with invariant names
and violated event ids, plus the origin line. A filter toggles all events vs
violations only. Read-only: no act buttons, so the viewer learns this screen
can only inform, never change state.

**S4 — Decision ("Decide the repair").** The repair under review (retry with
idempotency key), a before/after ledger preview (fork 500000 paise vs repaired
250000 paise), and the standing facts an approver needs (requester, scope,
expiry, what approve triggers, what reject preserves). In the prototype the
decision controls are visibly disabled with the reason ("prototype: decisions
stay in the console"); in a migrated console this is where the existing
approval action — idempotency keys included — would live beside its
consequence.

**S5 — Proof ("Run it again").** Capsule contents summarized as a checklist
(pinned fixture, fault schedule, recorded actions and ordering, expected
checks), a Replay control that produces a transcript plus a match verdict, the
"no model call" note, and download as a secondary affordance. The beat answers
"can I reproduce this" instead of "can I export a file".

## 6. What changes vs the current console and why

- **Narrative order replaces module tabs.** Why: the demo viewer thinks in
  story time (happened → separated → proved → decided → reproduced), not in
  module names. The modules stay underneath; the spine is a path through them.
- **A persistent incident header.** Why: order, amounts, verdict and inference
  mode are the anchor facts for every beat. Today they sit on the overview and
  must be remembered elsewhere.
- **The verdict as a sentence on S1.** Why: the demo has one sentence to land,
  and scattering its clauses across three views guarantees some viewers never
  assemble it.
- **Divergence owns the ledger consequence.** Why: event 2 without its cost is
  trivia; the cost without event 2 is unactionable. They belong on one beat.
- **The decision carries a before/after preview.** Why: approvers should see
  what changes. The current approval page describes the action; S4 shows the
  consequence.
- **Proof reframes capsule as reproduction.** Why: the product's done-state is
  "replay matches without model calls", not "artifact downloaded". The button
  copy and beat order should say so.
- **Entry by incident, not by id.** Why: a demo starts from the story. (The
  console keeps id entry — there is no campaign-list API, and inventing one is
  out of scope for this track.)

## 7. What stays the same

The honesty rails are non-negotiable and move verbatim into any migrated
screens: the test-data banner, the synthetic-world label, "proposal text,
untrusted" qualifiers, per-section origins, coverage notices, the
inference-mode notice, and the refusal to present fixture data as live. The
existing routes (`#/`, `#/campaign/:id/:view`), components and behaviors are
untouched by this track, and any future migration keeps them working until a
new beat reaches parity (section 10). Tool-call discipline is unchanged: the
browser still makes no provider calls, and no model identifier appears
anywhere it was not already documented.

## 8. Rejected alternatives

- **Full console rewrite.** Rejected: the console's honesty machinery is
  working, tested, and load-bearing. The problem is arrangement and a few
  missing composed views (verdict sentence, consequence preview), which is a
  migration, not a rewrite.
- **A dashboard of charts.** Rejected: the proof is one incident with exact
  amounts (250000 vs 500000 paise). Charts would decorate the numbers without
  explaining them, and invite misreading fixture data as measured traffic.
- **Merging Diff and Evidence into one page.** Rejected: they answer different
  questions ("where do the worlds separate" vs "prove it"). A merged page
  becomes the longest scroll in the product and buries both answers.
- **An auto-playing narrated tour.** Rejected: autoplay seizes control from the
  operator, harms accessibility, and fights live-demo pacing. Sequential
  Previous/Next gives the same path with the operator driving.
- **Renaming or reshaping the current routes now.** Rejected: it would break
  bookmarks, browser suites, and the parallel UI track for no demo gain. The
  prototype is a separate page until this proposal is accepted.
- **Live-wiring the prototype to the API.** Rejected: wiring it would fork the
  console into two maintained clients. The shell's job is to settle the
  experience decision; data wiring happens once, during migration.

## 9. Open questions for the user

1. **Audience.** Is the demo experience for hackathon judges, for developers
   evaluating GhostLab, or both? A judges-first spine can be tighter; a
   developers-first one may need the raw trace (S3) promoted earlier.
2. **Beat order.** Is the five-beat order right — specifically, should the
   Decision beat come before full Evidence, on the theory that approvers want
   the ask up front and drill into evidence only when skeptical?
3. **Live status in the header.** Should the incident header also cover running
   campaigns (status, reconnecting state), or stay a completed-incident
   summary with live concerns left in the console's campaign view?
4. **Campaign entry.** Is campaign-by-id entry acceptable long-term, or is a
   campaign-list API acceptable scope so the console can offer a real list?
5. **Verdict wording.** Does the verdict sentence need sign-off wording — who
   asserts it (the verifier, which version) — or is the finding reference
   enough?
6. **Next fidelity.** Is the static shell enough to decide, or should the next
   step wire one beat (suggest S2, the divergence consequence) to the existing
   fixture source as a migration pilot?

## 10. No-rewrite constraint

This track adds exactly two things: this doc and one self-contained prototype
page. It modifies zero existing files. If the proposal is accepted, migration
proceeds beat by beat behind the current views: each new beat is built from
the existing data source, logic and components, lands beside the view it
corresponds to, and only then replaces it in navigation. Existing routes stay
addressable until parity; existing tests keep passing throughout (extended,
never deleted for convenience); no API or backend change is required, because
every beat reads data the service already serves. If the proposal is rejected,
deleting the shell removes the track without residue.

## 11. Prototype shell

- **Entry:** `web/src/demo-shell.html` — one file, inline styles and script,
  no imports, no service calls (CSP `connect-src 'none'`).
- **Run:** from `web/`, `npx vite --port 5222 --strictPort`, then open
  `http://127.0.0.1:5222/demo-shell.html`. The existing console still serves
  from `/` unchanged.
- **Content:** fixture shaped like the refund proof (synthetic INR 2,500
  order; event 2 first divergence; 250000 / 500000 / 250000 paise ledgers),
  headed by PROTOTYPE and TEST DATA banners.
- **Scope:** navigation, screen order, header, verdict sentence, consequence
  preview and simulated replay transcript. Decision controls are disabled with
  reasons; nothing acts, nothing persists, nothing phones home.