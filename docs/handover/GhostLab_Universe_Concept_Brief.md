# GhostLab Universe
## An open, forkable simulation laboratory for AI agents

**Project type:** Developer infrastructure / agent reliability / open-source platform  
**Hackathon:** Meta Global AI Developer Hackathon 2026  
**Working pitch:** *"Before your AI agent acts in the real world, let it crash in a thousand simulated ones."*  
**Status:** Proposed concept and implementation plan, **not** a finished product.

---

## 1. Executive summary

GhostLab Universe is a developer platform for testing AI agents **against changing, stateful worlds**, rather than judging only their conversational outputs. Developers connect an agent to a simulated application containing APIs, user identities, permissions, a database, and business rules. GhostLab uses **Meta Muse Spark** to generate increasingly challenging scenarios and investigate failures. A separate deterministic runtime performs the actual actions and verifies what happened.

Its signature feature, **Fork Reality**, takes one agent execution and branches it into different controlled circumstances: a payment succeeds normally in one branch, while the server commits the payment and then returns a timeout in another. GhostLab observes whether the agent safely handles both branches. When it discovers a failure, the system packages the exact starting state, injected fault, event sequence, and correctness check into a **replayable Failure Capsule**.

The broader open-source platform comprises:

- **WorldSpec** — a documented format for creating stateful agent-testing worlds.
- **World Packs** — reusable simulations for commerce, support, scheduling, SaaS administration, and other domains.
- **Fault Packs** — portable scenarios such as timeout-after-commit, delayed responses, duplicate webhooks, access-control failures, and stale reads.
- **Agent Adapters** — thin integrations that run agents from various frameworks inside a controlled test environment.
- **Executable Verifiers** — rules that inspect the actual world state, not just the model's explanation.
- **Failure Capsules** — reproducible test cases for local debugging and CI regression testing.

**Strategic thesis:** The community should be able to build and share environments, faults, and verifiers without needing to modify GhostLab's core engine.

---

## 2. The problem

AI agents are moving from answering questions to taking actions: issuing refunds, placing orders, modifying accounts, deploying software, and coordinating workflows. A perfectly reasonable-looking conversation can still conceal a costly software failure.

Consider a support agent asked to refund a ₹2,500 order:

1. It sends `issue_refund(order_id)`.
2. The payment service commits the refund.
3. The network times out **after** the commit but **before** the response reaches the agent.
4. The agent interprets the timeout as failure and retries the refund.
5. The customer receives ₹5,000 in refunds on a ₹2,500 order.

Traditional prompt evaluations may say the agent gave a good explanation. Basic functional tests may verify only a successful refund. Neither necessarily catches the harmful sequence of side effects.

There are already strong products for agent tracing, datasets, evaluation, and red-teaming. GhostLab must differentiate through **controlled, stateful simulation + counterfactual execution + independently verified business outcomes + reproducible regressions**, not by claiming to be the first AI agent tester.

### Target users

| User | Pain point | GhostLab value |
|---|---|---|
| Solo AI developer | Cannot afford to test agents in production | Download a World Pack and run dangerous situations safely |
| Startup engineering team | Agent changes cause surprising regressions | Turn failures into automated CI checks |
| Platform / SDK developer | Wants confidence across many agent frameworks | Publish reusable adapters and verification contracts |
| Security / reliability engineer | Wants concrete evidence of bad outcomes | Inspect world state, event logs, and minimal reproductions |
| Research / open-source community | Benchmarks are difficult to reproduce | Exchange executable scenarios and controlled evaluations |

---

## 3. The core experience: Fork Reality

Imagine a developer connecting an e-commerce refund agent to GhostLab.

**Normal universe (branch A):**

- Initial order: paid ₹2,500, refunded ₹0.
- Agent invokes `issue_refund`.
- Mock payment API commits ₹2,500 and returns `success`.
- Agent ends the task; ledger shows ₹2,500 refunded.
- Invariant passes.

**Fault universe (branch B):**

- Restore the **same initial database snapshot**.
- Agent invokes `issue_refund`.
- Mock API commits ₹2,500 but sends a timeout.
- Agent retries without an idempotency key or status check.
- Ledger shows ₹5,000 refunded.
- Independent verifier fails `total_refunded <= total_paid`.

GhostLab then:

1. Shows both event timelines side by side.
2. Identifies the earliest state divergence.
3. Uses Muse Spark to suggest the likely root cause.
4. Removes irrelevant steps until the failure is a small, reproducible sequence.
5. Exports the sequence as a **Failure Capsule**.
6. Optionally proposes a patch; a developer reviews it; GhostLab reruns both branches to verify behavior.

**Critical engineering caveat:** True counterfactual replay needs deterministic/mockable tools, snapshots, fixed seeds and controlled scheduling. A live, opaque third-party API cannot necessarily be rewound. The hackathon demo should use a sandbox world and explain these limits.

---

## 4. Main product features

### 4.1 WorldSpec: a common language for simulated environments

WorldSpec defines the world, its allowed actions, initial state, injected faults, and invariants. It is a proposed **GhostLab format**, not a Meta API standard.

```yaml
name: ecommerce-refunds
version: "0.1"
agent:
  adapter: adapters/refund_agent.py
world:
  seed: fixtures/orders.json
  tools:
    - get_order
    - issue_refund
    - get_refund_status
policies:
  - id: refund_not_above_payment
    checker: checks.refund_not_above_payment
  - id: same_customer_only
    checker: checks.same_customer_only
faults:
  - timeout_after_commit
  - stale_read
  - duplicate_response
scenarios:
  - cases/happy_path.yaml
  - cases/timeout_after_commit.yaml
```

### 4.2 Living Worlds

Stateful, sandboxed applications with real transitions rather than fake conversational answers. The prototype should implement SQLite-backed orders, refunds, users, and transaction records, with tool calls modifying actual rows.

### 4.3 Muse adversarial explorer

Muse Spark receives a compact description of the agent's tools, policy rules, prior failures, and available fault mechanisms. It proposes structured scenarios; the runtime validates their schema and safety, executes them, records failures, and sends concise feedback into the next exploration step.

A good loop avoids wasting model calls on near-identical scenarios and prefers failures that expose **new** invariant violations.

### 4.4 Deterministic outcome verifier

Code-owned rules decide whether a scenario passed:

```python
def refund_not_above_payment(state) -> bool:
    for order in state.orders.values():
        amount = sum(
            r.amount for r in state.refunds
            if r.order_id == order.id and r.status == "succeeded"
        )
        if amount > order.amount_paid:
            return False
    return True
```

The code above is schematic: the actual storage objects are supplied by GhostLab's world adapter. The important point is that a model **cannot simply declare itself successful**.

### 4.5 Failure Capsules

Each saved capsule should contain:

```text
failure-capsule/
  manifest.yaml          # versions, agent adapter, world pack
  initial_state.json     # minimal relevant snapshot
  event_schedule.json    # ordered fault and tool events
  trace.jsonl            # agent/tool/result timeline
  policy_result.json     # deterministic failed check
  replay.py              # reproducible local runner
  README.md              # human-readable explanation
```

Where model outputs cannot be made deterministic, replay uses recorded responses or a clearly labeled best-effort fresh inference mode.

### 4.6 Repair and regression mode

Muse can propose an idempotency key or query-before-retry fix. **Do not auto-merge code into a user's repository in the MVP.** Present a patch for review; run the same capsule before and after; show the actual DB ledger and policy results.

### 4.7 Open World Packs

Contributors build a pack with a manifest, tool schemas, seed data, faults, and checkers. The published interface stays stable enough that other developers can download and run it from the CLI.

Potential follow-on packs: SaaS admin privileges, travel reservation retries, inventory race conditions, webhook duplicates, customer-support escalation, and file-access control.

---

## 5. System architecture

```text
              Developer / Agent framework
                         |
                  Agent adapter API
                         |
                GhostLab test runner
                   /      |       \
          World runtime  Muse loop   Verifier
          (SQLite/tools) (Model API) (code-owned)
                   \      |       /
                   Traces + failures
                         |
           Fork Reality + minimizer
                         |
                 Failure Capsule
                         |
                CLI / CI / dashboard
```

**Suggested stack**

- Backend: Python 3.11+, FastAPI, Pydantic.
- Sandbox/state: SQLite, deterministic clock, seeded RNG, mock tools.
- Muse integration: Meta Model API via OpenAI-compatible Python SDK, optionally using its Responses API.
- Frontend: React, Tailwind, trace timeline + fork comparison.
- Testing: pytest, seeded buggy agents, fixed budgets.
- Deployment: Docker Compose or a single local development setup.
- CI: GitHub Actions example that runs a saved Failure Capsule against an agent adapter.

### Runtime interfaces (proposed)

```python
class AgentAdapter:
    async def run(self, user_message: str, tools, context: dict): ...

class World:
    def reset(self, snapshot: dict, seed: int): ...
    async def invoke(self, tool_name: str, args: dict): ...
    def snapshot(self) -> dict: ...

class Policy:
    def check(self, state: dict, event_log: list) -> dict: ...
```

### Sample service endpoints (proposed, not Meta endpoints)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/worlds/load` | Load a World Pack |
| `POST` | `/runs` | Start a scenario run |
| `GET` | `/runs/{id}` | Results and trace |
| `POST` | `/runs/{id}/fork` | Fork from saved state |
| `POST` | `/runs/{id}/minimize` | Reduce failure sequence |
| `POST` | `/capsules/export` | Export reproducible case |
| `POST` | `/capsules/replay` | Rerun a saved case |

---

## 6. Meta Muse integration: exact role

**Meta-provided foundations (verified against public docs):**

- **Muse Spark 1.3** model ID: `muse-spark-1.3`.
- **Model API base URL:** `https://api.meta.ai/v1`.
- **Responses API:** `POST /v1/responses` for multi-step agent loops and reasoning continuity.
- **Function/tool calling:** Muse proposes calls; **our code executes** them and returns results.
- **Structured outputs:** require scenarios, explanations, and proposed patches to validate against schemas.
- **Tool search:** potentially useful when the community library grows, but not necessary for the MVP.

GhostLab-specific mechanisms—world simulation, deterministic replay, fault injection, minimization, verifier, WorldSpec—are things **we build**. They are not native Muse features.

**Illustrative connectivity test** (runs only once billing/API access works):

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://api.meta.ai/v1",
    api_key=os.environ["MODEL_API_KEY"],
)
response = client.responses.create(
    model="muse-spark-1.3",
    input="Propose three timeout-related failure modes for a refund agent.",
)
print(response.model_dump_json(indent=2))
```

For the main application, use schema-constrained outputs and a real tool/result loop instead of parsing free-form text. Keep API keys on the server; set token/cost budgets; redact secrets from logs.

### Why Muse matters rather than being decorative

Muse is the adaptive search/planning intelligence. It reads actual traces, chooses **new** fault combinations, and assists with diagnosis/repair. Without the model, the runtime still executes deterministic baseline tests; with Muse, the intended advantage is more effective discovery under the same test budget. That claim should be **measured**, not assumed.

---

## 7. Demo narrative for judges (90–120 seconds)

1. **Connect:** Run the local refund agent against a World Pack. Show a normal refund succeeding.
2. **Explore:** Click **Find failure**. Muse proposes fault scenarios with visible rationale.
3. **Fork Reality:** Compare the normal world against a branch where the refund commits but the API times out.
4. **Catch:** The ledger records two refunds. GhostLab displays the exact failed invariant and event divergence.
5. **Export:** Download a Failure Capsule; run `ghostlab replay <capsule>` to reproduce it.
6. **Repair:** Apply a reviewed idempotency fix; rerun; show both branches passing with a single refund.
7. **Community:** Add a tiny new `delayed_webhook` fault or policy from a separately packaged contribution—no changes to the engine.

The most compelling visual is the split-screen branch timeline and a before/after transaction ledger.

---

## 8. Hackathon MVP: must-have, stretch, and avoid

### Must ship

1. One working stateful e-commerce World Pack.
2. Mock agent with at least three intentionally seeded failure modes.
3. Meta Muse scenario generation plus feedback-based follow-up.
4. Deterministic business-rule verifier.
5. One true Fork Reality comparison from identical initial state.
6. Failure Capsule export and CLI replay.
7. Small React dashboard for tool traces and outcomes.
8. Documentation showing a community contributor how to add a fault or checker.

### Stretch goals

- Counterexample minimization (delta debugging).
- Human-reviewed patch proposal and verified retest.
- Minimal GitHub Actions integration.
- Another mini-world demonstrating extensibility.

### Avoid in the initial build

- Full container security platform, external production systems, auto-merging patches, ten framework adapters, autonomous financial actions, and a polished marketplace. Those distract from proving the core invention.

---

## 9. Suggested build schedule (Oct 29–Nov 4, 2026)

| Date | Deliverable | Evidence of completion |
|---|---|---|
| Oct 29 | Agent + SQLite world + invariant tests | Happy path and intentional duplicate refund reproducible |
| Oct 30 | WorldSpec loader, fault injection, snapshots | Deterministic tests pass locally |
| Oct 31 | Muse scenario planner + tool loop | At least one model-generated fault produces executable scenario |
| Nov 1 | Fork Reality + trace comparison | Two branches from same state; divergent outcomes visible |
| Nov 2 | Failure Capsule + CLI + React viewer | Teammate replays failure from export |
| Nov 3 | Benchmark seeded bugs; polish | Results table and README |
| Nov 4 | Video, submission, contingency testing | Live demo and final repository ready before deadline |

**Official project deadline:** November 4, 2026, **9:00 PM Eastern Time** (November 5, **7:30 AM IST** if Eastern is on standard time). Confirm on the event page before submission.

---

## 10. Team division (2–4 people)

- **Reliability/backend engineer:** SQLite world, tools, snapshots, faults, verifier.
- **Muse/agent engineer:** structured scenarios, adaptive exploration, trace interpretation, API budget.
- **Frontend/demo engineer:** timeline, comparison UX, replay UI, demo recording.
- **Open-source/evaluation engineer:** WorldSpec, CLI, seeded bugs, reproducibility benchmark, documentation.

For a two-person team: combine backend + verifier, and Muse + frontend; both share integration and documentation.

---

## 11. Evaluation plan: prove this is better

Build a small benchmark with ~10–20 known faulty agent variants (for example duplicate refund, wrong-account refund, stale inventory, missing idempotency key, unbounded retry).

Compare, at the same **scenario execution budget**:

1. Randomly sampled scenarios.
2. One-shot Muse-generated scenarios.
3. GhostLab's adaptive Muse exploration using prior results.

Report:

- **Bug discovery rate:** seeded failures exposed / total seeded failures.
- **Unique verified failures:** deduplicate by invariant and minimal sequence.
- **False positive rate:** flags not backed by violated invariants.
- **Tests per discovered bug.**
- **Replay reliability:** fraction reproducing on second run.
- **Model API consumption:** tokens, calls, and measured cost.

Do not invent numbers. Report a clear small-sample result even if the adaptive approach does not always win.

---

## 12. Security, trust, and limitations

- All tool side effects are simulated; nothing should charge real customers.
- Use allowlisted tool adapters and sandbox fixtures; never execute arbitrary model-written scripts by default.
- A failing invariant is proof of a problem **inside the modeled world**, not proof of production exploitability.
- Coverage depends on the World Pack, policies, and realism of faults.
- Live LLM behavior can be nondeterministic; record model/tool responses for reproducible playback.
- Human review is mandatory before applying code changes.
- Access control, secrets handling, and log redaction are essential if this becomes a hosted service.
- Avoid describing GhostLab as a certification that an agent is 'safe'.

---

## 13. Community growth and business potential

**Open-source core:** WorldSpec schema, sample runtime, pack loader, CLI, agent adapter API, starter worlds and tests. Publish under a permissive license after the team checks dependencies and IP terms.

**Community flywheel:** Developers publish World Packs → other developers run them and encounter new failures → they contribute verifiers/faults → the shared library becomes more valuable.

**Potential product path:** Managed test runs, private world packs, team collaboration, release gates, trace retention, enterprise access controls. These are possible future offerings, **not promised hackathon features**.

**Positioning:** *"The agent reliability layer for software that can take actions."*

---

## 14. Judging strategy: strengths and risks

**Strengths:** Technical ambition; a concrete and surprising failure demonstration; substantial use of Muse for adaptive reasoning; open community contributions; objective measurements.

**Risks:** Existing evaluation platforms already cover some related workflows; building realistic worlds is hard; insufficient time could produce an attractive UI without a functioning execution engine.

**Mitigation:** Make one world deeply convincing, show a real side-effect violation, replay it, fix it, and demonstrate one external pack extension.

### One-paragraph submission pitch

> GhostLab Universe is an open simulation and counterfactual testing platform for AI agents. Powered by Muse Spark, it creates adversarial conditions in stateful sandbox worlds, discovers failures that ordinary conversation evaluations can miss, and verifies actual side effects against executable rules. Its Fork Reality interface compares what an agent does when one real-world condition changes. Every discovered bug becomes a shareable, reproducible Failure Capsule that developers can run in CI. Open World Packs let the community extend GhostLab to new industries and workflows without rebuilding the engine.

---

## 15. Open decisions for the team

- Is the goal a **developer-first** tool? (This concept is intentionally not a mass-consumer app.)
- Do we focus only on refunds or include one small second domain?
- Should the team prioritize **minimization** or **repair** as its stretch feature?
- Who owns the runtime, SDK contract, Muse calls, and evaluation?
- Can we access a working Meta API key, or must the demo initially run on recorded/model-mock scenarios?

---

## 16. Sources and API implementation references

These are public references supporting Meta API capabilities and hackathon timing; the GhostLab architecture above is our proposed product design.

- Meta Model API overview: https://dev.meta.ai/docs/overview
- Available models: https://dev.meta.ai/docs/models
- API format selection: https://dev.meta.ai/docs/protocols
- Responses API: https://dev.meta.ai/docs/protocols/responses
- Function/tool calling cookbook: https://dev.meta.ai/docs/cookbook/tool-function-calling
- Tool search: https://dev.meta.ai/docs/tool-search
- API reference: https://dev.meta.ai/docs/api-reference
- Meta hackathon: https://dev.meta.ai/events/global-hackathon

*Prepared October 29, 2026. Product name, interfaces, and code sketches are conceptual and may change during implementation.*
