Prompt-3

AI-1:
**Bottom line:** With 10 hours left, your best leverage is (1) a native 30-second demo video with a pain-point hook on X, (2) a Fly.io deployment with no cold-start embarrassment, (3) explicit Muse Spark Open tool-calling + structured output in your agent loop, (4) a community-vote push that respects the platform's rules, and (5) a demo that visibly shows planner→critic→verification behavior, not just a chatbot.

---

## 1. What makes developer-tool demo posts go viral on X

### Format: thread vs single post

The strongest evidence points to a **founder-led thread built around a native 30-second demo video**, pinned immediately, not a single post with a link. The Nowix launch playbook — based on 2026 founder data — says the optimal structure is a hook tweet, then the native demo in tweet 2, then value points, with the CTA in the second tweet rather than the first. The Sverklo launch thread (an actual developer-tool launch) uses an **8-tweet thread**, with no gap longer than 30 seconds between tweets 1 and 2 because "the hook needs to land immediately," then tweets 3–8 paced over 2–3 minutes.

The LigoSocial study of **1,209 build-in-public posts from 15 creators** found that **140–280 characters** is the reliable engagement sweet spot, but the rare long-form post (280+ characters) was the highest-variance move: lower average engagement rate, but the highest median multiple when it landed. It also found that **text-only tweets edged out image tweets** on engagement rate, while **video won the most bookmarks**. So the thread can be text-heavy, but the demo video is the bookmark magnet.

### The first 3 seconds

The Sverklo hook opens with a **shared pain point**, not "I built X": "I got tired of MCP code search tools either (a) uploading my codebase to someone else's cloud (b) hallucinating functions that don't exist." It names the two specific failure modes concretely, and "runs locally" is the tiebreaker, not the lead. The Nowix playbook echoes this: lead with the demo, not the pitch; a 30-second native video under 30 seconds, uploaded directly to X, never a link-out to YouTube.

### Captions and media

**Use MP4, not GIF.** X auto-compresses GIFs harder than MP4s, and native video plays inline without a click. The Sverklo guide specifies a 30-second, 1600×770, H.264 yuv420p faststart-encoded MP4 as the primary asset, with a GIF only as fallback. Captions should be burned in or added via X's native caption tool — the demo must work with sound off.

### Who to tag

The nowix playbook recommends a **coordinated creator block of 15–25 warm contacts DM'd directly and assigned specific jobs** (reply, quote, repost) rather than a generic "please support us". For the Meta hackathon specifically, tag **@MetaAI**, **@AIatMeta**, and **@cerebralvalley** — the organizers' accounts are the most likely to amplify. Tagging @baseten, @modal_labs, or @FireworksAI is worth doing if you use their inference, because the infrastructure partner must verify model use for the $10K prize, and a public tag creates a paper trail.

### Best posting time for US + India

The strongest data point for a US + India audience is **Tuesday or Wednesday at 8–10 AM Eastern**. That is 5:30–7:30 PM India Standard Time — a strong evening window for Indian developers and a morning window for US East Coast. A Chinese-language X analysis of 2026 posting patterns separately identified **5–11 PM, 10 AM–1 PM, and 12–2 AM** as golden windows, which aligns with the US morning / India evening overlap.

Crucially, the LigoSocial study found that **posting time barely predicted the biggest hits** — outliers landed roughly in proportion to ordinary posting volume across every weekday, with only a modest Thursday lean. So do not agonize over the exact minute; the hook and the native demo matter far more.

### Examples with numbers

- **OpenAI Developers' Open Model Hackathon recap thread** (X, Oct 2025): announced winners including RoboChef (overall), Steam Print, Bota, Memory Palace, and a scent-printer. The thread uses short, concrete descriptions per winner and links to Devpost for details. This is the format to copy: one winning project per tweet, concrete capability, link at the end.
- **10-second AI horror challenge** (X, Oct 2025): a contest where the top entry hit **70,000+ likes and 7,000 reposts**, with multiple entries above 10,000 likes. The lesson is not that horror is your category — it is that a constrained, visual, instantly understandable demo with a strong hook massively outperforms a feature list.
- **RocketRide social-media track winner**: selected on **150+ likes, 45+ comments, 45+ reposts**. This is a realistic benchmark for a niche developer-tool demo, not 70K likes.

**Most worth opening (3):**
1. **Sverklo X launch thread** — the most directly applicable developer-tool thread structure with hook, benchmark, honesty section, and media specs: `github.com/sverklo/sverklo/blob/main/launch-content/x-thread-launch.md`
2. **Nowix startup launch playbook** — the coordinated creator-block and timing playbook: `nowix.ai/blog/startup-launch-twitter/`
3. **LigoSocial build-in-public study** — the only large-N quantitative study on what actually correlates with outlier engagement: `ligosocial.com/research/x-build-in-public-outliers`

---

## 2. Cerebral Valley public gallery and community vote

### How the vote works

The Meta Global AI Developer Hackathon official waiver confirms that the organizers "may award additional prizes of up to $50,000 in the aggregate for bonus awards recognizing standout projects or community achievements. Those categories may include, without limitation, a community vote award, most viral project award, or other unique or notable project categories". The bonus prizes are therefore explicitly contemplated in the rules, but the waiver does **not** specify the mechanics of the public gallery vote — one vote per verified account, the voting window, or whether solicitation is permitted. The madrobot summary of the official rules says bonus winners are "to be announced on or about November 10," and the judging runs to November 8.

**What I could not find:** No public page specifies whether Cerebral Valley prohibits vote solicitation for this hackathon. The general Cerebral Valley judging description is a **science-fair-style first round** where judges walk assigned sections, followed by a **panel of final judges rating the top 7–8 projects** on four aspects. That is the judged track, not the community vote. The community vote appears to be a separate, platform-native mechanism. **Assume solicitation is allowed but do not use bots, vote-trading schemes, or fake accounts** — "one vote per verified account" is the only rule you can rely on, and any automated voting would risk disqualification under the "incomplete or false information" clause.

### How past community-vote winners gathered votes

The clearest precedent is the **London Agentic Evolution Hackathon** (MongoDB × Cerebral Valley, May 2026). The winning team, PlanPass AI, **received the highest number of community votes** and was ultimately awarded first place by the judging panel. Their project was a production-oriented planning-compliance platform for UK homeowners, not a toy demo — they explicitly "focused on building something with real-world application" rather than "a demo for the sake of it". The lesson: **the community-vote winner also tends to be a strong judged project**. Votes follow perceived real-world usefulness, not just polished UI.

A separate Cerebral Valley event (GLM-5.3 Flash Lightning Hackathon) says "Winners will be decided on September 2 and announced later through our community channels," but does not describe vote mechanics.

**Practical implication:** The community vote is likely a gallery where verified Cerebral Valley accounts click "vote." Your job in the next 10 hours is to make the gallery card **instantly legible** — a one-line problem statement, a screenshot or GIF, and a link to the live demo — and to mobilize your network to create verified accounts and vote, without spamming.

---

## 3. Muse Spark Open and Muse Glimmer on Baseten, Modal, Fireworks

### Muse Spark Open: model ID, capabilities, pricing, latency

The model ID you were given — `meta-models/muse-spark-open-1026` — is **close but not the ID used by the major providers**. The canonical open-weight checkpoint is **`meta-models/Muse-Spark-Open-816B-A42B`**, served on Baseten as `baseten/meta-models/muse-spark-open` and on Fireworks as `accounts/fireworks/models/muse-spark-open`. The `-1026` suffix may be an internal hackathon routing alias, but when calling Baseten or Fireworks directly, use their documented IDs. **If you are using a hackathon-specific Baseten endpoint, use the ID Baseten gave you; otherwise use `baseten/meta-models/muse-spark-open`.**

**Architecture:** 816B total parameters, 42B active per token, sparse Mixture-of-Experts (8 of 256 experts per token), hybrid attention with sliding-window layers. Context window is **1M tokens** on Baseten, Fireworks, Modal, and Together; Groq serves a 131K context preview.

**Tool calling and structured output:** Yes, across all three partners. Baseten's model page advertises **Tool Calling and JSON Mode**. The Meta prompting guide confirms the model returns **standard `tool_calls` in the OpenAI function-calling format**, with reasoning returned separately from the answer. Groq's documentation (same model) specifies **parallel tool calls** and **structured outputs with JSON schema**. Eden AI lists Baseten, Fireworks, and Together endpoints with **Tools, JSON, and Cache** support; Fireworks additionally lists **Reasoning and Vision**. One comparison site claims Baseten does **not** support structured outputs, which contradicts Baseten's own page and Eden AI — treat this as a possible stale-data error and **test structured output on your specific endpoint before relying on it**.

**Reasoning output format:** Reasoning is **always on** and cannot be disabled. `reasoning_effort` accepts `low`, `medium`, `high`, or `xhigh` (default `xhigh`). The model addresses its own messages to `self` for reasoning and `user` for the final answer, so reasoning is separated from the answer in the API response.

**Pricing (identical across Baseten, Modal, Fireworks, Together, Nebius):** **$1.25 / 1M input tokens, $4.25 / 1M output tokens, $0.15 / 1M cached input**. At 1,000 input + 500 output tokens per call, that is roughly **$3.38 per 1,000 API calls**.

**Latency:**

| Provider | Output speed | First token (TTFT) | Uptime (24h) |
|---|---|---|---|
| **Baseten** | **251 tok/s** | **0.37 s** | 100% |
| **Modal** | **235 tok/s** | **0.67 s** | 100% |
| **Fireworks** | **83 tok/s** | **0.61 s** | 100% |
| Together AI | 172 tok/s | 0.23 s | 98.2% |
| Nebius | 122 tok/s | 0.45 s | 65.2% |

Baseten and Modal are the speed leaders; Fireworks is the slowest of the three but still 100% uptime. ProviderBench independently ranks Modal second at **95.5 tok/s** in one measurement and **235 tok/s** in another, reflecting measurement-window differences — do not over-index on a single benchmark.

**Rate limits:** The clearest public numbers are for Muse Spark (not necessarily Open) on the standard tier: **3,000 RPM and 4M TPM per team**; contributor tier is **100 RPM and 3M TPM**. Baseten and Fireworks may have different hackathon-specific limits — check your hackathon dashboard. Fireworks offers **on-demand deployments with no rate limits** if you need guaranteed throughput.

### Muse Glimmer: local deployment

**Muse Glimmer is a 30B dense multimodal agentic model** (2B perception encoder + 28B decoder), Apache 2.0 licensed, released August 10, 2026, distilled from Muse Spark for autonomous agentic tasks on consumer hardware.

**Quantized sizes for laptop:**

| Build | Size | Target hardware |
|---|---|---|
| Q4_K_M | **16.8 GB** | Fits 24 GB VRAM |
| Dynamic Q4_K_XL | **19.7 GB** | Fits 32 GB VRAM |
| Q8_0 | **27.58 GB** | Effectively lossless; needs more VRAM |

The Q4_K_M build is the default for most people — best quality-per-byte, in the same size class Meta targeted for mainstream local deployments. **A 16.8 GB model does not fit on a typical laptop CPU with 16 GB RAM**, but it runs on a laptop with a **24 GB GPU** (e.g., RTX 4090 laptop) or on Apple Silicon with **32 GB unified memory or more**. For CPU-only inference, expect **very slow generation** — the model is designed for GPU or large unified memory. You need **llama.cpp build b10353 or newer**; older builds will refuse to load the architecture.

**Speed:** No public laptop-CPU tok/s benchmark was available in my searches. On a 24 GB GPU with Q4_K_M, expect usable but not fast agentic performance — likely single-digit to low-double-digit tok/s depending on context length. **If you need Glimmer in your demo tonight, run it on a GPU, not CPU**, or use Muse Spark Open via Baseten/Modal for the live demo and show Glimmer as a local-deployment option in your architecture slide.

### What counts as "verified" open-weight use by the infrastructure partner

The hackathon waiver says participants build with "Meta AI products, including Muse Spark, Glimmer, SAM 3, and any other Meta AI products identified on the Event page". The Best Build on Meta Open Weight Models prize requires the infrastructure partner to **verify the model use**. The most defensible way to satisfy this is:

1. **Serve Muse Spark Open through Baseten, Modal, or Fireworks** — the three named partners — and use their endpoint in your live demo.
2. **Keep API logs or request IDs** showing the model ID and timestamps.
3. **Tag the partner publicly** in your X post and submission (e.g., @baseten, @modal_labs, @FireworksAI).
4. **Show the model ID in your demo video or README** — e.g., a curl command or SDK call with `model="baseten/meta-models/muse-spark-open"`.

If you also run Glimmer locally, show it in the architecture but do not rely on it for the judged live path unless you have verified hardware.

---

## 4. What judges consider strong agentic use in 2026

The 2026 hackathon consensus is that **single-pass planning is the weakest link** in agent systems. The PlannerCritic Engine's framing is representative: "Most agents act too early, skip hard subproblems, and start executing a plan that was never reviewed. A single-pass decomposition embeds silent assumptions, and the first sign of trouble arrives mid-execution — after state has already diverged". The winning pattern is the **Draft → Critique → Revise → Escalate loop**: a planner decomposes the goal into a typed plan with tasks, dependencies, verification steps, and rollback points; a **separate critic LLM** audits every subtask for feasibility, risk, missing steps, unsafe sequencing, unverified dependencies, and weak rollback; the planner revises in a bounded loop with a revision budget and convergence detection; if it cannot converge, it escalates a precise question to a human.

This is exactly the pattern that fits your fault-injection testing tool. Your agent should:

- **Plan** a test scenario with explicit fault-injection points and expected recovery behavior.
- **Critique** its own plan with a separate model call (or the same model in a critic role) against criteria like "does this injection actually test the failure mode?" and "is there a rollback path?"
- **Execute** with tools (simulated environment APIs, fault injectors, log inspectors) and **verify** outcomes against structured assertions.
- **Escalate** when the critic cannot approve the plan after N rounds.

Other winning patterns from 2025–2026:

- **APEX Society** (Devpost): 9 specialized Qwen-powered agents with a **conflict-resolution loop up to three review rounds**, then SelfHealer, Debugger, Executor, and documentation generation.
- **GeoMind** (Multi-Agent Hackathon 2025): four agents form a **"retrieval-review-iteration-output" loop** ensuring every citation is verifiable, addressing academic hallucination.
- **`again`** (2026 Devpost): "learns it, proves it, and runs every repeat with zero AI calls" — a **verifier called FORK** proves acceptance criteria before shipping.
- **Crucible** (Trendshift): "a planner, a builder, and a separate jury of skeptical judges that proves every acceptance criterion before it ships. It loops until every criterion truly passes — then stops".
- **RoboChef** (OpenAI Open Model Hackathon overall winner): a GPT-OSS-powered kitchen assistant controlling a robot arm — **physical-world tool use with verification**.
- **PlanPass AI** (Cerebral Valley London winner): production-oriented planning-compliance platform that "analyses local council regulations, evaluates compliance, and generates interactive home designs" — **real-world domain, not a toy**.

**What the Meta judges will score (from the official criteria):** Impact **30%**, creative use of Meta's AI tools **25%**, demo including video **25%**, technical depth and execution **20%**. For the Best Agentic Use-Case bonus, the waiver's judging criteria say Impact (30%), creative use (25%), demo (25%), technical depth (20%) — but the bonus category specifically rewards **autonomy, effectiveness, and practical usefulness**. Make the demo show the agent **recovering from an injected fault without human intervention**, and show the **verification step** that proves recovery succeeded. That is the concrete, visual proof of agentic effectiveness.

---

## 5. Cheapest fast deployment tonight for FastAPI + React/Vite

The only large controlled comparison of free tiers for a FastAPI app (7 days, ~9,200 requests per host) found a clear ranking for **demo links that humans will click**:

| Host | Cold start p50 | Warm p50 | Sleeps when idle? | True $0 beyond trial? |
|---|---|---|---|---|
| **Fly.io** (shared-cpu-1x, 256MB) | **0.4 s** (auto-stop wake ~0.3–0.4 s) | 52 ms | No (auto-stop, fast wake) | ✅ within allowance |
| **Render** (free web service) | **14.2 s** | 41 ms | **Yes, after 15 min** | ✅ with sleep |
| **Railway** ($5 trial credit) | 1.9 s | 38 ms | No (trial ends) | ❌ $5 credit, then card |

The finding that matters: **Render's free tier is a conversion killer for demo links.** The author's access logs showed **43% bounce on cold starts vs 6% warm** — every person who clicks your link after lunch waits 14 seconds, and half leave. Fly.io is the only option that is **both truly $0 and does not embarrass you**: shared-cpu machines with `auto_stop_machines` wake in ~300–400 ms, fast enough that nobody notices. The catch is **256 MB is tight** — the author's FastAPI container idled at 180 MB and OOM-killed twice during bursts until they set `memory = '512mb'` (still inside the free allowance for one machine).

### Recommended setup for tonight

**Use Fly.io for the FastAPI backend and Vercel for the React/Vite frontend.**

1. **Backend on Fly.io:** `fly launch`, deploy from Dockerfile, set `[vm] memory = '512mb'`, `auto_stop_machines = true`, `auto_start_machines = true`, `min_machines_running = 0`. This gives you a stable `https://your-app.fly.dev` URL with sub-second wake.
2. **Frontend on Vercel:** free tier, 100 GB bandwidth, automatic SSL, Git integration — `vercel deploy` is live in under a minute.
3. **Database:** Supabase free tier (500 MB Postgres, 50K auth users) or Neon free Postgres. Both work with FastAPI via SQLAlchemy/asyncpg.
4. **CORS:** Set FastAPI's `allow_origins` to your Vercel URL. This is the #1 gotcha for split frontend/backend deployments.
5. **Cold-start mitigation:** Fly.io auto-stop wake is ~0.4 s, which is acceptable. If you can afford **$1–2 of paid Fly credit**, set `min_machines_running = 1` for the demo window — then there is zero cold start.

**Do not use Render free for a demo link tonight** unless you accept 14-second cold starts and 43% bounce. **Do not use Railway** unless you are willing to put in a card after the $5 trial credit — it is "cheap," not "free".

**Alternative all-in-one:** Hugging Face Spaces (Docker Space) can host FastAPI + static frontend, but cold starts on free CPU Spaces can be **30+ seconds** and the URL is less "product-like" than a custom domain. Fly.io + Vercel is the better demo aesthetic.

**Modal as backend:** Modal can serve the FastAPI app and the model in one place, but Modal's web endpoints are optimized for serverless functions, not long-running FastAPI with WebSockets. For a reliability-testing demo with simulated worlds, Fly.io gives you more control.

---

## Top 5 actions, in priority order, for the next 10 hours

### 1. Record the 30-second native demo video — and lead with the fault, not the architecture

Record a screen capture that shows, in the first 5 seconds, an agent **failing a reliability test**, then your tool **injecting a fault**, then the agent **recovering and verifying recovery**. Burn in captions. Export as H.264 MP4, 1600×770, under 30 seconds, under 1 MB. This is the single highest-leverage asset for the Most Viral prize: native video wins bookmarks, and the hook determines whether anyone watches past 3 seconds. Do **not** open with a logo or a title card.

### 2. Deploy to Fly.io + Vercel now, and test the live URL from a cold state

Deploy the FastAPI backend to Fly.io with **512 MB memory** and auto-stop enabled. Deploy the React/Vite frontend to Vercel. Verify CORS. Open the live URL in an incognito window after 20 minutes of idle — if it loads in under 2 seconds, you are demo-ready. If you see OOM kills, bump memory to 512 MB and redeploy. A broken or slow live link during judging is worse than no link.

### 3. Wire Muse Spark Open tool calling + structured output into the agent's critic/verifier step

Use **Baseten or Modal** for the fastest first token and highest throughput. Pass `tools` in OpenAI format and require the critic to return a **JSON-schema-validated verdict** (e.g., `{"approved": bool, "findings": [...], "severity": "..."}`). Set `reasoning_effort="medium"` or `"high"` — not `"xhigh"` if latency matters for the live demo. Log the model ID and endpoint in your README and show it in the video. This directly satisfies the "Best Build on Meta Open Weight Models" verification requirement and demonstrates agentic depth for the $5K prize.

### 4. Post the X thread at 8–10 AM ET Tuesday/Wednesday, with a coordinated first-hour push

Use the Sverklo-style structure: **tweet 1 = pain-point hook + native video; tweet 2 = live demo link + what the agent does; tweet 3 = the agentic loop (planner→critic→verifier) with a diagram; tweet 4 = Muse Spark Open + Baseten/Modal, model ID, latency numbers; tweet 5 = the honest limitations and what you learned; tweet 6 = CTA to vote in the Cerebral Valley gallery.** Pin tweet 1 immediately. DM 10–15 warm contacts with specific asks (reply, quote, repost) — not a generic blast. Tag @MetaAI, @AIatMeta, @cerebralvalley, and your inference partner. Post at **8–10 AM ET** to hit both US morning and India evening.

### 5. Mobilize community votes through verified accounts — and make the gallery card instantly legible

Submit the project to the Cerebral Valley gallery as early as possible tonight. Write the gallery description as a **one-line problem + one-line solution + one proof point** (e.g., "AI agents fail silently in production. We inject faults in simulated worlds and verify recovery. 94% recovery rate across 200 injected failures."). Ask your network to **create verified Cerebral Valley accounts and vote** — do not use bots or vote-trading. The precedent winner (PlanPass AI) won both the community vote and the judged first prize by being a real, production-oriented solution, not a demo. Your gallery card should signal "real tool, real results" in under 5 seconds.

---

**What I could not find:** (1) The exact Cerebral Valley vote-solicitation rules for this hackathon — the waiver covers bonus prizes but not vote mechanics. (2) A public laptop-CPU tok/s benchmark for Muse Glimmer Q4_K_M. (3) A definitive statement on whether Baseten's Muse Spark Open endpoint supports structured outputs — Baseten's own page says JSON Mode, but one comparison site says no; test before relying on it. (4) A past Cerebral Valley community-vote winner's exact vote-gathering tactics beyond the PlanPass AI outcome. When in doubt, optimize for the judged criteria (Impact 30%, creative Meta use 25%, demo 25%, technical depth 20%) and treat the community vote as an amplifier, not a substitute.

AI-2:
For your tool - AI agent reliability testing via fault injection in simulated worlds - you have a strong shot at all 4 bonuses because it hits devtools + agentic + open-weight at once. Here's what I found, with links:

## 1. What makes developer-tool demo posts go viral on X

**Length:**
- X feed cold traffic = 15-30s, designed to play silently with captions. Front-load everything
- Devtool sweet spot = 30-60s. Longer walkthroughs go in reply thread. Rule from Launch Playbook: Keep it to 30 to 60 seconds
- Proof clip table: 20-60s for demo/receipt. If you can hold retention, 90s-3min can work: Most high-performing launch videos run between 90 seconds and three minutes. Length matters far less than retention

**First 3 seconds:**
- Show the problem in first 3s, show magic moment by second 10
- Framework: lead with strong 3-second hook, move through Problem, Solution, Proof
- Retention falls off a cliff in opening seconds. Fix: Show the product in opening frames. Do not build up to it

**Captions / format:**
- Put captions on screen - lot of people scroll with sound off. Native upload > YouTube link for algo.
- Single post with video + 2-3 reply thread performs better than thread-only for viral prize counting. Your engagement counted is likes + reposts + comments on *one* X post, so keep all CTA to that post.
- Thread vs single: Use single 60s demo as parent, then reply with architecture, GitHub, and gallery link.

**Who to tag for this hackathon:**
Tag @AIatMeta @MetaAI @cerebral_valley @baseten @modal @fireworks_ai + 2-3 builders. Don't spam. Judges track #MetaAIHackathon tag.

**Best posting time US + India:**
- Benchmark best time is weekday mornings around 9 AM in your audience's local time
- US Eastern: 8 AM to 10 AM and 12 PM to 2 PM
- India: 9 AM to 11 AM and 1 PM to 3 PM, with evening 7-9 PM IST stronger
- For AI launches specifically: post Tuesday/Wednesday 10am-noon Eastern, which is 7am-9am Pacific. Wednesday 9 AM ET is best

**Overlap for you tonight:** Post at **9 AM ET = 7:30 PM IST** captures US morning scroll + India evening peak. Since deadline is Nov 4 9pm ET, post your demo *before* submission, then re-quote it Nov 5 9am ET.

**Examples of high-engagement AI hackathon demos:**
I could not find public like-counts for Meta Global AI Hackathon 2026 X demos yet - gallery just opened. Past Cerebral Valley pattern is 200-2k likes for top demo threads.

Closest public analogs worth opening:
- **YouTube - CaseBrief | Evidence Before Confidence | Meta Global AI Developer Hackathon 2026** - Shows team used Muse Spark Open through OpenRouter with Baseten as provider - This is your verification pattern. **TOP 3 TO OPEN**
- Instagram reel - Virality Simulator built at Insforge hackathon SF, 20K Likes, 180 comments - screen recording of network graph simulating demo performance. Shows faceless terminal + metrics style that works.
- Instagram reel - CyberSentinel X AI Cyber Risk Command Center 27-sec dark dashboard demo with AUTONOMOUS SOC PLATFORM text - good example of problem-first framing.

What I could NOT find: exact X engagement numbers for Meta Global 2026 viral prize - counting runs until Nov 8, so leaderboard not public yet.

## 2. How Cerebral Valley gallery + community vote work

**Official wording for this hackathon:**
Bonus prizes (up to $50,000 in all) for standout projects or community achievements, such as a public vote or the most viral project, to be announced on or about November 10 and also listed as Bonus prizes (up to $50,000 in all): for standout projects or community achievements, such as a public vote or the most viral project

Gallery is at `cerebralvalley.ai/e/{slug}/hackathon/gallery` - public projects with team, placement, description, GitHub/demo links. Example: OpenAI Voice Hack Night gallery shows 60+ projects.

**Vote mechanics:**
For this edition, Cerebral Valley official rules page only says Official Rules will be published before the Hackathon begins on October 26, 2026. Specific UI not documented publicly. Based on past CV events:
- One vote per verified account, winner announced Nov 16 per your brief.
- Past People's Choice was based on votes from event participants and guests or attendees must be present to collect votes

**Solicitation rules:**
I could not find an explicit Cerebral Valley ban on vote solicitation for this edition. Closest rule in similar hackathons: You can ask for a vote, but not trade or purchase votes. Safe: DM friends, post on LinkedIn/X/ Discord asking for gallery votes, don't buy bots or automate.

Past winners gathered votes by: posting demo in CV Discord #showcase, LinkedIn personal story + gallery link, short Loom explaining problem, asking 3-5 friends to vote early to get on trending.

## 3. Muse Spark Open on Baseten / Modal / Fireworks + Muse Glimmer

**Exact IDs - hackathon:**
- You were told `meta-models/muse-spark-open-1026` - community hackathon repo confirms Use Baseten model ID `meta-models/muse-spark-open-1026` per sub-agent search. Canonical HF repos are `meta-models/Muse-Spark-Open-816B-A42B` BF16 and NVFP4.
- Total 816B, 42B active, context 1M tokens, Input modalities text, image, video, audio in

**Pricing (same across partners):**
OpenRouter lists `meta/muse-spark-open` at $1.25 per million input tokens, $4.25 per million output, $0.15 cached. Every upstream endpoint charges same: Makora, Modal, Together, Nebius, Fireworks and Baseten

- Modal table: Muse Spark Open | Text, Image, Audio | 1.25 / 0.15 cached | 4.25
- Fireworks also $1.25 / $4.25, with US-only Serverless 50% premium waived on Muse Spark Open per sub-agent.

**Tool/function calling + structured output:**
- Baseten: Introducing function calling and structured output for open-source LLMs - lets you pass defined tools, model selects and returns most appropriate function
- Structured output support recently launched for TensorRT-LLM Engine Builder, enabling JSON and function calling with structured parameters via outlines logit masks
- Fireworks: All Fireworks language models can now be invoked with JSON schema or context-free grammar to guarantee format. They subclass OpenAI, so `tool_calls` array works
- Modal is OpenAI-compatible Shared Endpoints billed per token

**Reasoning output format:**
- Muse Spark Open is reasoning model, supports `reasoning_effort` param: `low`, `medium`, `high`, `xhigh` (default xhigh). Against local vLLM you pass `extra_body={"chat_template_kwargs": {"reasoning_effort": "high"}}`. Thinking tokens come back in `reasoning_content` separate field, similar to standard `reasoning_content` field

**Latency / rate limits:**
- Telemetry snapshot: Vercel displayed 269 TPS and OpenRouter 249 TPS for Baseten on Oct 16, 2026. Lowest time per task on Baseten table shows ~0.49s TTFT for top models - Spark Open similar.
- Rate limits not public on Baseten Model API page, contact sales; Modal Starter includes $30 compute/month, Shared Endpoint tokens billed from first request. For hackathon, use partner credits: Baseten $100, Modal $100, Fireworks $25 per your invite email.

**Glimmer:**
- About 29.6 billion total parameters, including image-understanding encoder, dense transformer. Also described as 28B text decoder + 1.8B perception encoder
- Compressed to roughly 4-bit precision, fits under 20 GB, targeting 24-32 GB envelope. Full precision >55 GB. Quantized to about 4 bits, weights shrink from around 60 GB to just under 16 GB. Community reported working on RTX 3090 at 22-23 GB
- Ollama has 15 versions 17GB to 65GB plus dflash builds 20GB. Can run quantized via llama.cpp/Ollama: Yes, Q4_K_M ~16.8 GB, fits 24GB card. CPU 8-16GB RAM can only handle 1B-4B models comfortably, so Glimmer needs GPU or high unified memory Mac.
- Speed: Unsloth notes 4-bit close to original, recommends UD-IQ2_S minimum for agentic work. DFlash drafter guesses 16 tokens at a time.

**What counts as "verified" open-weight use:**
I could not find official published verification spec. Evidence: CaseBrief used Meta Muse Spark Open through OpenRouter, with BaseTen returned as the provider on successful inference runs. That implies verification = inference logs showing model ID `meta-models/muse-spark-open-1026` served by partner. Best practice: include screenshot of Baseten dashboard with model ID, keep request logs, add "Built with Muse Spark Open" attribution required by license, and link to your deployment on partner.

## 4. What judges consider strong agentic use in 2026

- All Things Agentic Hackathon (Google Cloud): Innovation & Operational Utility 40% - How much real-world friction does agent remove on its own? We reward autonomous, high-value action over simple chat — agents that make decisions and complete tasks with little to no hand-holding per sub-agent report.
- Technical Architecture 30%: System Design: Does agent logic handle errors, API timeouts, edge cases? Robustness: Does agent avoid hallucinations? Is there evidence of grounding? (Build With AI Hackathon rubric)

**Patterns that win "Best Agentic" now:**
- Planner/Executor/Critic loop: three roles: planner picks next step, builder does it, and judge grades the result. It cycles until judge approves. Judge should be separate agent
- For your fault-injection tester: perfect - you already have budgeted autonomy + verification. Judges love:
  1. **Tool use with verification** - agent proposes fault, simulates, checks logs, grades pass/fail
  2. **Planner/critic** - separate critic that scores reliability, not same model self-review
  3. **Deterministic harness** - show 100 simulated worlds, failure rate before/after

- Winning examples from 2025-26: FoundrAI autonomous startup agent, Agentic OS for a Phone (Voice Hack Night People's Choice), math tutor with small decision model listening and calling Gemini only when needed. Common trait: agent does work without human in loop.

## 5. Cheapest fast way to deploy FastAPI + React/Vite tonight with stable URL

**TL;DR for tonight:** Vercel (Vite frontend) + Render (FastAPI backend). Zero dollars, stable URLs.

| Platform | Free allowance | Sleep? | Cold start | Gotcha |
|---|---|---|---|---|
| **Render Web Service** | 750 instance-hours/month per workspace free | Yes, spins down after 15 min idle, spin-up about one minute | ~30-60s, Render docs says about one minute | Free Postgres expires 30 days, 512MB RAM |
| **Railway** | Dropped original free tier 2025, now $1 credit/month free plan | No sleep (always-on) | None | $1 credit dies fast |
| **Fly.io** | No permanent free for new orgs, pay-as-you-go from ~$2/mo | Configurable | None | Need Dockerfile, CLI learning curve |
| **Vercel Hobby** | 100GB bandwidth, unlimited deploys, no sleep on static | No | None | Frontend only, functions need separate backend |


**Best path for 10-hour deadline:**
1. Frontend: `npm run build` -> Vercel import GitHub repo, set framework Vite. Instant URL, no cold start.
2. Backend: Render - connect GitHub, root: FastAPI, build: `pip install -r requirements.txt`, start: `uvicorn main:app --host 0.0.0.0 --port $PORT`. Add env var `CORS_ORIGINS=https://your-vercel.app`
3. Alternative if Render cold start unacceptable for demo: Fly.io two machines ~$4-5/mo, or Railway $5 credit for tonight.

Gotchas tonight: Render first request after 15 min idle will be 30-60s - warm it before judges open. Keep backend light - no heavy ML model load on Render free (512MB). Use Baseten/Modal for Llama calls, not local.

---

## Top 5 actions in priority order for next 10 hours

**1. Lock verification for Best Build prize (30 min):** Deploy a tiny endpoint that calls ` meta-models/muse-spark-open-1026` on Baseten (or Modal shared) with tool calling. Log provider name. Add footer "Built with Muse Spark Open" to UI. Screenshot dashboard showing model ID. This is required proof - without it you lose $10K.

**2. Ship stable demo URL before 8pm ET (2 hrs):** Vercel frontend + Render backend. Test CORS. Add `/health` that warms. Put URL in Cerebral Valley submission, GitHub README, and X post. Record 45-sec screen capture: 0-3s show flaky agent failing, 3-10s show your fault-injection world, 10-45s show before/after reliability score.

**3. Post viral X demo before 9am ET Nov 5 and optimize for counting until Nov 8:** Post tonight ~9am ET / 7:30pm IST slot. Caption: "AI agents fail in prod. I built a chaos lab that breaks them on purpose. 100 simulated worlds. Fault injection. Auto-grading. Built with @baseten @AIatMeta Muse Spark Open." Tag @cerebral_valley @MetaAI. Pin. Then reply thread with gallery link, GitHub, architecture (planner/critic loop). **This is $20K prize.**

**4. Maximize Community Vote - start soliciting now:** Once gallery goes live, DM 30 friends with direct vote link + 1-line ask. Post on LinkedIn: story of why reliability testing matters, ask for vote. Post in hackathon Discord #showcase. Remember you can ask for a vote, but not trade or purchase votes. Early votes push you to trending.

**5. Strengthen agentic judging narrative for demo video (1 hr):** Reframe your project as agentic harness, not just testing tool: show autonomy (agent runs 100 worlds alone), effectiveness (found X bugs), practical usefulness (saves eng hours). Judges score impact 30%, creative use of Meta tools 25%, demo video 25%, technical depth 20%. In video, explicitly say planner/critic separation, verification loop, budgeted autonomy.

Good luck - you have a devtool that directly speaks to what 2026 judges care about. Get that Baseten log + URL live first, viral post second.

AI-3:
Your strongest play is to **submit first**, then immediately publish one tight, founder-led X demo post with a 20–35 second screen recording, a pinned reply containing the live URL and GitHub, and coordinated quote-posts from real users/builders. For the Meta hackathon, use **Baseten’s exact hackathon slug `meta-models/muse-spark-open-1026`** if that is what the hackathon instructions specify; the public Baseten catalog slug is `meta-models/muse-spark-open`, so do not assume they are interchangeable. [github](https://github.com/utsavgoswami/utstack-famitune/blob/main/packages/kb/hackathon/meta-global-ai-developer-hackathon.md)

## 1. X demo virality

For a developer tool, the winning format is usually a **single high-signal post plus a threaded follow-up**, not a long opening thread. Lead with the failure you prevent—not “AI agent reliability testing”—then show a fault being injected and the agent failing/recovering on screen. Developer-tool guidance specifically favors a strong problem/promise hook, visuals or code, a concise TL;DR, and 1–2 high-signal hashtags. [business.daily](https://business.daily.dev/resources/social-media-marketing-for-developer-tools-platform-by-platform-guide/)

- **Length:** Keep the launch post short—roughly 1–3 lines plus video. Put the technical explanation in replies or a thread.
- **First 3 seconds:** Open on the UI with an obvious before/after: “Agent passed the happy path. We injected an API failure—and caught it in 12 seconds.”
- **Caption formula:** Problem → proof → what the viewer sees → link in reply. Avoid a link in the main post; use the first reply for the live demo, GitHub, and submission link.
- **Thread vs single post:** Publish the demo as one post; add 2–4 replies covering architecture, fault-injection scenarios, and a short technical explainer. Threads are useful for depth, but a standalone visual demo is easier to repost and quote. [business.daily](https://business.daily.dev/resources/social-media-marketing-for-developer-tools-platform-by-platform-guide/)
- **Who to tag:** Tag only accounts genuinely likely to respond—Meta AI/Cerebral Valley if their event guidance invites it, plus 2–4 credible AI/dev-tools builders. Do not mass-tag; ask a few people for **quote posts with their own take**, which generally spreads better than plain reposts. [growthmatrix](https://www.growthmatrix.agency/x-launch-playbook/)
- **Timing:** You are at 7:30 PM IST / 10:00 AM ET on Wednesday—post as soon as the submission is live. That hits the US East Coast workday and gives Europe and India follow-on exposure; X engagement benchmarks commonly favor weekday mornings, especially 9–11 AM ET. [business.daily](https://business.daily.dev/resources/social-media-marketing-for-developer-tools-platform-by-platform-guide/)
- **Engagement mechanics:** Replies, quote-posts, saves, and fast early engagement matter; stay in replies for the first 1–2 hours and add one new demo angle rather than deleting a slow post. Do not buy engagement or use engagement pods—X prohibits artificial engagement and it can damage the account. [growthmatrix](https://www.growthmatrix.agency/x-launch-playbook/)

**Examples worth opening (top 3 marked):**
- **[Open] Meta Muse Spark Open launch post/article:** [https://research.meta.ai/blog/muse-spark-open](https://research.meta.ai/blog/muse-spark-open) — useful model positioning, but not a hackathon-demo engagement example. [research.meta](https://research.meta.ai/blog/muse-spark-open)
- **[Open] Cartesia founder launch case study:** [https://www.growthmatrix.agency/x-launch-playbook/](https://www.growthmatrix.agency/x-launch-playbook/) — reports a founder post reaching 7.1M views, about 340x the founder’s following; its playbook is directly relevant to launch mechanics. [growthmatrix](https://www.growthmatrix.agency/x-launch-playbook/)
- **[Open] Cerebral Valley Enterprise MCP Hackathon gallery:** [https://cerebralvalley.ai/e/mcp-hackathon/hackathon/gallery](https://cerebralvalley.ai/e/mcp-hackathon/hackathon/gallery) — shows the public-gallery/voting interface pattern and confirms a two-minute-max demo-video norm for at least one Cerebral Valley event. [cerebralvalley](https://cerebralvalley.ai/e/mcp-hackathon/hackathon/gallery)

I could **not find reliable public engagement counts and links for specific high-engagement AI-hackathon demo X posts** in the available results; treat any precise “likes/reposts” benchmark as unverified rather than copying it.

## 2. Cerebral Valley voting

Cerebral Valley hosts public project galleries for hackathons, and at least the Enterprise MCP Hackathon gallery explicitly displayed an **“Open Voting”** state.  However, I could not find the Meta hackathon’s public gallery URL, its vote-counting rules, or an explicit anti-solicitation clause in the indexed event pages. [cerebralvalley](https://cerebralvalley.ai/e/mcp-hackathon/hackathon/gallery)

The official waiver says bonus prizes may include a community-vote award, may be announced on or about November 10, and do **not** reduce track prizes.  The event page confirms judging runs November 4–8 and winners are announced November 9, while your stated community-vote announcement date is November 16; follow the submission portal and Discord for the controlling rules. [cerebralvalley](https://cerebralvalley.ai/e/meta-ai-developer-hackathon/waiver)

**Practical vote strategy:**
- Put the gallery vote link in your X pinned reply, GitHub README, LinkedIn post, and Discord build-log—but ask for **one vote from people who genuinely viewed the project**, not “vote-for-vote” exchanges.
- Make voting frictionless: one screenshot showing where the Vote button is, plus a one-line reason to vote.
- Do not offer rewards, run giveaways tied to votes, or coordinate reciprocal voting; even where not explicitly prohibited, it risks disqualification and violates the spirit of a public vote. The launch guidance likewise warns against artificial engagement and pods. [growthmatrix](https://www.growthmatrix.agency/x-launch-playbook/)
- I could not identify a verified past Cerebral Valley community-vote winner or a documented vote-gathering tactic; do not rely on anecdotes here.

## 3. Muse models and providers

### Muse Spark Open

| Item | Baseten | Modal | Fireworks |
|---|---|---|---|
| Public model ID | `meta-models/muse-spark-open`  [baseten](https://www.baseten.co/library/muse-spark-open/) | I could not verify a public endpoint/model ID from the available results | `accounts/fireworks/models/muse-spark-open`  [fireworks](https://fireworks.ai/models/fireworks/muse-spark-open) |
| Hackathon ID | Use `meta-models/muse-spark-open-1026` **if your hackathon instructions say so**; this differs from the public catalog slug.  [github](https://github.com/utsavgoswami/utstack-famitune/blob/main/packages/kb/hackathon/meta-global-ai-developer-hackathon.md) | Verify in the hackathon Discord/provider instructions | Verify whether the hackathon requires a special 1026 identifier |
| OpenAI-compatible API | Yes; Baseten’s example uses the OpenAI SDK against `https://inference.baseten.co/v1`.  [baseten](https://www.baseten.co/library/muse-spark-open/) | Muse Spark Open is listed as a hosted inference platform, but I could not verify endpoint specifics | Yes; Fireworks supports OpenAI-compatible calls.  [fireworks](https://fireworks.ai/models/fireworks/muse-spark-open) |
| Tool calling | Yes.  [baseten](https://www.baseten.co/blog/muse-spark-open-launch/) | Listed as supported by Meta, but provider-specific details unverified | Yes.  [fireworks](https://fireworks.ai/models/fireworks/muse-spark-open) |
| Structured output / JSON mode | **No for Muse Spark Open**: Baseten says all hosted language models except Muse Spark Open support structured outputs and JSON mode.  [docs.baseten](https://docs.baseten.co/inference/model-apis/overview) | Unverified | Fireworks lists function calling; I could not verify structured-output support.  [fireworks](https://fireworks.ai/models/fireworks/muse-spark-open) |
| Reasoning output | Returned separately from `content`; tool calls arrive in a standard `tool_calls` array.  [dev.meta](https://dev.meta.ai/docs/muse-spark-open/deploy) | Unverified | Reasoning model; exact output shape unverified from available results |
| Pricing | I could not verify Baseten’s public per-token price from the available results | Unverified | $1.25 input / $0.15 cached input / $4.25 output per 1M tokens, Standard serverless.  [fireworks](https://fireworks.ai/models/fireworks/muse-spark-open) |
| Rate limits | Hackathon participants selected for credits receive $100 credits and 2M TPM limits; exact public per-key limits unverified.  [cerebralvalley](https://cerebralvalley.ai/e/meta-ai-developer-hackathon) | Unverified | Serverless has usage limits; on-demand deployments have “no rate limits” per Fireworks.  [fireworks](https://fireworks.ai/models/fireworks/muse-spark-open) |
| Latency | Not publicly specified in the available results | Not publicly specified | Not publicly specified; Fireworks markets Fast modes as 100+ tokens/sec for interactive apps, but that is not a Muse-Spark-specific latency figure.  [fireworks](https://fireworks.ai/pricing) |

**Important:** Do **not** rely on structured JSON output from Muse Spark Open on Baseten. Use tool calling with a strict tool schema, or ask for JSON and validate/repair it in your FastAPI backend. Baseten explicitly excludes Muse Spark Open from structured outputs and JSON mode. [docs.baseten](https://docs.baseten.co/inference/model-apis/overview)

### Muse Glimmer

Muse Glimmer is Meta’s open-weight, multimodal model distilled from Muse Spark and designed for local hardware; it can be served through vLLM, SGLang, llama.cpp, or ExecuTorch.  Fireworks identifies the current release as **Muse Glimmer 30B** (29.7B parameters), with a 131K+ context window, schema-based tool calling, failure recovery, and selectable reasoning strength from low to xhigh. [dev.meta](https://dev.meta.ai/docs/models)

- **Laptop GPU:** A 30B dense model is plausible on a high-VRAM GPU only with aggressive quantization; it is not a comfortable default for most consumer laptops.
- **CPU / llama.cpp / Ollama:** Technically supported through llama.cpp, but a 30B dense model will likely be too slow for a live hackathon demo on CPU. I could not find a verified tokens/sec benchmark for Muse Glimmer on llama.cpp/Ollama.
- **Demo-safe alternative:** Use Baseten/Fireworks for the primary live demo, and show a local Glimmer inference path only if you already have it working. Do not spend the final hours making local inference reliable.
- **What “verified” likely means:** The infrastructure partner verifies that requests actually used its Muse endpoint/model. Use the hackathon-specific slug, keep the provider dashboard/request logs, name the model and provider in the README, and include a short architecture diagram plus a redacted API-response log. The event page says Baseten offers credits and 2M TPM limits to selected participants using Meta open-weight models, which supports partner-side verification. [cerebralvalley](https://cerebralvalley.ai/e/meta-ai-developer-hackathon)

## 4. Strong agentic design

Judges will likely reward an agent that can **pursue a goal, use tools, detect its own errors, and produce an auditable result**—not merely chain prompts. Meta positions Muse Spark explicitly for multi-step tool loops, software-engineering assistants, long-context reasoning, parallel tool calls, streamed tool-call arguments, and reasoning carried across turns. [dev.meta](https://dev.meta.ai/docs/models)

For your fault-injection product, make the agent loop visible:

1. **Planner:** Choose a test objective, target workflow, and fault hypothesis.
2. **Executor:** Call simulated-world tools—APIs, browsers, databases, message queues, UI actions.
3. **Critic/verifier:** Check invariants, expected side effects, logs, and user-visible outcomes; distinguish “tool failed” from “agent handled failure correctly.”
4. **Budgeted autonomy:** Cap steps, cost, wall-clock time, and blast radius; require approval for destructive actions.
5. **Evidence trail:** Save the plan, each tool call, observation, verification result, final score, and replayable trace.

That maps well to Muse Spark’s tool-loop strengths and gives judges something concrete to inspect.  Winning agentic projects tend to pair autonomy with human oversight and a clear practical workflow—for example, the AI Agents Hackathon winner combined automated social-media response generation with human oversight and analytics, while other winners automated development or contract-review workflows.  I could not verify a current “Best Agentic Use-Case” winner list specific to this Meta hackathon. [dev.meta](https://dev.meta.ai/docs/models)

## 5. Deploy tonight

**Best default: Vercel for the React/Vite frontend + Render free tier for FastAPI**, with a public demo URL and a recorded backup video. This separates static-asset serving from Python backend cold starts and is the least risky path tonight.

| Option | Best use | Free tier / cold start | Tonight’s verdict |
|---|---|---|---|
| **Vercel + Render** | React/Vite frontend + FastAPI backend | Vercel Hobby is free for personal/non-commercial projects; Render’s free web services can sleep and cold-start | **Best overall**; pre-warm backend and record backup |
| **Railway** | One simple full-stack deployment | Trial/limited free usage; sleeping/cold-start behavior varies by plan | Good if already configured; avoid starting from zero tonight |
| **Fly.io** | Dockerized FastAPI + frontend | Free allowances have changed over time; machines may suspend | Good if your Dockerfile already works |
| **Hugging Face Spaces** | Gradio/Docker demo | Free CPU Basic has 2 vCPU/16 GB RAM, but compute Spaces require a paid plan and free Spaces sleep | Use only for a simple Gradio demo; not ideal for React + FastAPI tonight.  [autointerviewai](https://www.autointerviewai.com/blog/best-ai-hackathon-tools-2026) |
| **Modal** | Python backend / model inference | Good for serverless Python, but not the simplest public full-stack host | Use for inference if needed; keep frontend on Vercel |
| **Render alone** | FastAPI serving built frontend | Simple, but static frontend + API on one service can be awkward | Fine fallback |

A current hackathon-tools roundup likewise recommends Vercel for the frontend and warns that free-tier rate limits and sleeping services are the common demo failure modes; it specifically notes Hugging Face Spaces sleep when unused.  **Before submitting:** deploy, open the URL in an incognito mobile browser, run one complete fault-injection test, pre-warm the backend, and record a local backup demo. [autointerviewai](https://www.autointerviewai.com/blog/best-ai-hackathon-tools-2026)

## Top 5 actions

1. **Submit the project now**, even if polish is incomplete; include the live URL, GitHub, 2-minute demo video, architecture diagram, and explicit Baseten/Muse model evidence. The deadline is 9:00 PM ET today. [cerebralvalley](https://cerebralvalley.ai/e/meta-ai-developer-hackathon)

2. **Deploy Vercel + Render immediately**, use environment variables for all keys, pre-warm the FastAPI service, and verify a full end-to-end demo from a phone before publishing anything. [autointerviewai](https://www.autointerviewai.com/blog/best-ai-hackathon-tools-2026)

3. **Wire the hackathon-specific Baseten model ID**—`meta-models/muse-spark-open-1026` if that is the official instruction—then demonstrate tool calling, a critic/verification step, budget caps, and a saved agent trace. Do not use Baseten structured-output/JSON mode for this model. [github](https://github.com/utsavgoswami/utstack-famitune/blob/main/packages/kb/hackathon/meta-global-ai-developer-hackathon.md)

4. **Post the X demo within the next hour**: 20–35 second screen recording, problem-first caption, live URL and GitHub in the first reply, pin it, and ask 3–5 credible builders for quote-posts—not reciprocal votes. Stay active in replies for two hours. [business.daily](https://business.daily.dev/resources/social-media-marketing-for-developer-tools-platform-by-platform-guide/)

5. **Prepare the gallery/vote conversion path**: add a prominent “Vote for this project” section to the README and landing page, with a screenshot showing exactly where to vote; share it only through genuine audiences and never tie votes to rewards or vote-trading. Cerebral Valley galleries support public voting, but the Meta event’s exact solicitation rules were not publicly indexed. [cerebralvalley](https://cerebralvalley.ai/e/mcp-hackathon/hackathon/gallery)

AI-4:
I'll research all five areas, verifying the model and hackathon details first since those are the claims most likely to be wrong.

**Deadline:** 9 PM ET is 7:30 AM IST on Nov 5, so you have about 12 hours, not 10. The X post has to go live *before* you submit, because its URL goes into the submission form (details in section 1).

**Caveat on sourcing:** I couldn't find a public page listing the four bonus prizes. The official rules only say bonus prizes of up to $50K total, for things like a public vote or most viral project, announced around November 10. The specific mechanics below come from other participants' repos quoting Discord, so treat them as soft and confirm in #questions.

## 1. Going viral on X

**What gets counted.** One participant's tracker says you paste the post URL into the submission form, and scoring is likes + reposts + comments through Nov 8, with views excluded. That has three consequences:
- Post before you submit.
- Optimize for replies and reposts, not watch time. End the post with a question, because comments count.
- Use a single post with native video, not a thread. A thread spreads engagement across several posts, and probably only the main one is scored.

**Format:**
- **Upload the video natively.** Posts linking to YouTube or other video platforms don't get X's video boost.
- **Length.** Sources disagree: some say under 60 seconds gets the most boost, others say under 2:20. A 30–45s vertical cut fits both.
- **First 3 seconds.** One guide says the algorithm weighs the hook and the completion rate; that source is weak. For your tool, open on the failure: an agent visibly breaking under an injected fault, with a large caption. Burn captions in, since autoplay is muted.
- **Links.** Most guides claim a 30–50% reach penalty for external links. A reading of xAI's 2026 open-sourced code found no hard-coded link penalty. Putting the link in the first reply costs nothing, so do that.

**Who to tag:** @AIatMeta, @MetaforDevs, @cerebral_valley, and your inference partner. Meta tagged @FireworksAI_HQ, @baseten and @modal in its open-weights post. Another entrant plans to use #MetaAIHackathon and ask in #questions which Meta account to tag; do the same. Whether the partners repost builders is my guess, not verified.

**When to post:** Right now (7:30 PM IST) is 9 AM ET. The US + India overlap is roughly 8–9 AM EST / 6:30–7:30 PM IST, and developer audiences also peak around 9–10 PM local. Post between 11 PM and 12:30 AM IST: Indian devs are still up, it's lunch on the US East Coast and mid-morning on the West Coast.

Line up 15–20 people to reply and repost in the first 30–60 minutes, since early engagement velocity is reportedly the strongest distribution signal. Don't buy engagement: X excludes accounts that manipulate metrics.

**Examples with numbers:** I couldn't find a set of AI hackathon demo posts with public engagement numbers. What I could verify:
- Gibberlink, from the ElevenLabs London hackathon, passed 15M views on X in a week and was reposted by Marques Brownlee.
- In this hackathon, Meta's launch post got 55.7K views, the partner post 14.2K, and a community post 72.9K.

These are view counts, not the scored metrics.

## 2. Cerebral Valley gallery and community vote

**What I verified.** Cerebral Valley galleries show an "Open Voting" state with a Vote button on each card. Cards with YouTube links show the video thumbnail; Loom or Drive links show only truncated link text. So:
- Use a YouTube link and pick a strong thumbnail frame.
- Make the first sentence of the description the hook, because cards truncate it.

**Couldn't find:**
- This event's gallery URL or vote window.
- The one-vote-per-verified-account rule text.
- Any anti-solicitation rule.
- Write-ups from past community-vote winners.

**Date conflict:** Participants plan a vote push from Nov 9 to 16, which matches your date, but that conflicts with the Nov 10 bonus announcement in the rules. Confirm in Discord.

**Vote-gathering norms:** Devpost explicitly encourages entrants to share with their networks, and it advises organizers to review votes for fraud. Asking for votes is fine. Vote swaps, incentives, or alt accounts are where the disqualification risk is.

## 3. Muse Spark Open and Glimmer

**Baseten model ID: two answers.**
- Baseten's public docs list `meta-models/muse-spark-open`.
- Discord told participants to use `meta-models/muse-spark-open-1026`, and one participant reports it works.
- Another notes no public page mentions a -1026 suffix.

Settle it by calling `GET https://inference.baseten.co/v1/models` with your key. If `-1026` is listed, use it; the hackathon credit tracking probably attaches to that ID (my inference).

**Baseten behaviour:**
- **Tool calling:** supported.
- **Structured output / JSON mode:** not supported. Every Baseten model supports them except Muse Spark Open. Validate JSON in your own code with a retry.
- **Reasoning:** always on. Effort levels are low/medium/high/xhigh, default xhigh, and none/minimal fall back to low. Reasoning arrives in `reasoning_content`, the answer in `content`, and reasoning tokens are billed as completion tokens.
- **Other limits:** `n` > 1 returns HTTP 400, logprobs are ignored, and neither seed nor temperature 0 makes output reproducible.
- **Price:** $1.25 input / $0.15 cached input / $4.25 output per 1M tokens, the same as Fireworks and Modal.
- **Rate limits:** account defaults are 15 RPM / 100K TPM unverified, 120 / 500K verified. The hackathon offered 2M TPM with Baseten credits.
- **Latency:** At xhigh, the median time to first answer token is 13.95s, at about 145 tok/s. One independent test at medium effort measured Baseten at 147 tok/s, Modal 91, Fireworks 68, with first tokens under a second everywhere. Low effort took about 11s per short job against 22.6s at xhigh.

  Set `reasoning_effort="low"` in your agent loop, or the demo will crawl.

**Modal:** a per-token, OpenAI-compatible Shared Endpoint at the same prices, served in NVFP4 and created with `modal endpoint create --shared --model meta-models/Muse-Spark-Open-816B-A42B`. I couldn't verify tool calling, structured output or rate limits there.

**Fireworks:** `accounts/fireworks/models/muse-spark-open`, with function calling and image input supported, serverless on US-only capacity. Text and image input only; structured output there is unverified.

**Credits:** a participant relaying Discord on Nov 1 says partner credits are closed. At these prices that hardly matters.

**Muse Glimmer:**
- A 30B dense vision model under Apache 2.0.
- The 17 GB Q4 GGUF runs at about 1.6 tok/s on a four-core laptop CPU, so a 130-token answer takes around 80 seconds, plus reasoning time.
- Smaller quants are Q4_K_L at 18.3 GB and Q2_K_L at 12.35 GB; on Ollama, `ollama run hf.co/bartowski/Muse-Glimmer-30B-GGUF:Q4_K_M`.
- I found no laptop-GPU benchmarks. Q4 doesn't fit in 8–16 GB of VRAM, so it would partially offload to CPU; expect single-digit tok/s. Not viable for tonight.

**What counts as "verified":** I found no public document describing how partners verify. The Discord relay says every runtime model must be a Meta model, and Muse Spark Open via Baseten qualifies for the $10K open-weight bonus. Other entrants assume the open-weight step must be a real path a judge can reproduce, with the model ID logged.

Also, the license requires you to prominently display "Built with Muse Spark Open" in the product UI. Add that badge.

## 4. What judges reward as "agentic"

**Scoring weights:** impact 30%, creative use of Meta's tools 25%, demo 25%, technical depth and execution 20%.

**Meta's own vocabulary** (judges from Meta will recognise it):
- **Agents 101 workshop:** a loop with explicit stop statuses (answered, needs_input, awaiting_signoff, paused) that saves state and pauses on API errors.
- **Agents 102 workshop:** "Verifiable Agents".
- **Meta's Agents Rule of Two:** in one session, an agent should have at most two of three properties: untrusted input, sensitive data access, and the ability to change state.

**Framing for your project:** the red-team agent *is* the agent. The loop:
1. Spark Open plans a fault campaign.
2. It injects faults into the simulated world.
3. A deterministic oracle grades the agent under test, so the LLM isn't grading itself.
4. A critic targets the next fault at the weakest spot found so far.
5. It stops on a budget (steps, tokens, $) or on coverage, and reports why it stopped.

The demo moment to aim for: it finds a failure you didn't script, with the budget meter and stop reason on screen. A similar pattern in this hackathon: MetaECU Copilot sends planner output through a deterministic safety gate that rejects bad plans back to the planner.

I couldn't find past "best agentic" winners with published judging rationale. Colosseum's $5K "Most Agentic" prize, for example, had undisclosed criteria.

## 5. Deploying tonight

**Recommendation: Hugging Face Spaces**, Docker SDK, one container with FastAPI serving the built Vite assets.
- Free tier: 2 vCPU, 16 GB RAM, sleeps after 48 hours idle.
- Gotchas: bind to 0.0.0.0:7860, don't run uvicorn with `--reload`, put secrets in Space settings, and remember there's no persistent disk.

**Alternatives:**
- **Render free:** 512 MB / 0.1 CPU, spins down after 15 minutes idle, and restarting takes about a minute. A judge clicking once sits on a loading page.
- **Railway:** no free tier, only a $5 trial, and one source says new signups were paused as of Oct 23.
- **Fly.io:** a trial of 2 machine-hours or 7 days, so it dies mid-judging.
- **Vercel + separate backend:** two deploys plus CORS for no gain here.

**Judge path:** per the Discord relay, a live URL is optional; judges run from the public repo with their own keys via .env.example, and a keyless replay mode is accepted. Ship a replay mode. It also protects your demo from Baseten 529 "overloaded" errors.

## X links

- ★ [AI at Meta: Spark Open launch](https://x.com/AIatMeta/status/2110779754274292212): candidate to quote-post, and confirms the handle.
- ★ [MetaforDevs: open-weights partner post](https://x.com/MetaforDevs/status/2112971571321332179): the exact partner handles to tag (14.2K views).
- ★ [Suraj Sharma on this hackathon](https://x.com/suraj_sharma14/status/2103280824309408234): a terse bullet format plus a "bookmark + tag a builder" call to action (72.9K views).
- [MetaforDevs launch post](https://x.com/MetaforDevs/status/2110045152601633114): 55.7K-view baseline for this community.
- [Luke Harries (ElevenLabs)](https://x.com/lukeharries/status/1894062627267170789): Wikipedia cites it for Gibberlink; I couldn't confirm it is the viral clip itself.
- [LinkedIn Gibberlink repost](https://www.linkedin.com/posts/georgefirican_have-you-heard-of-gibberlink-mode-heres-activity-7300892491572989952-Kd8s): the comments show "it was scripted" backlash. Lesson: don't overclaim autonomy.
- YouTube: I found nothing worth your time.

## Top 5 actions, in priority order

1. **Now to ~9:30 PM IST: make Spark Open the agent's brain on Baseten.**
   - Confirm the slug via `/v1/models`.
   - Set `reasoning_effort="low"` and enable tools; validate JSON in code.
   - Write the model ID and request ID into every run artifact.
   - Add the "Built with Muse Spark Open" badge.

   This is the gate for the $10K prize and the core of your agentic story.
2. **~9:30–11 PM: record one session and cut two videos from it.**
   - The 3-minute judging video; it's the primary judging artifact.
   - A 30–45s vertical cut for X: open on the failure, captions burned in, end on the scorecard with budget and stop reason.
   - Upload the long one to YouTube for the gallery thumbnail.
3. **~11:30 PM IST (1 PM ET): post on X.**
   - Native video, no link in the body, tag the handles above, use the hashtag once Discord confirms it.
   - Close with a question; put the repo and live link in the first reply, then pin the post.
   - Have your 15–20 people engage in the first 30 minutes, and answer every reply for the first hour.
4. **Midnight to ~3 AM:**
   - Deploy to HF Spaces with the keyless replay mode, a README judge path and `.env.example`.
   - Ask Discord #questions to confirm the X-post rules, the vote window, and whether bonus prizes stack.
5. **Submit by 5:30 AM IST (2 hours of buffer).**
   - Include the X URL and the YouTube link; lead the description with the hook.
   - Draft the vote-push message now and send it when the gallery opens. No incentives or vote swaps.

AI-5:
Your deadline is **November 4 at 9:00 PM ET (November 5 at 7:30 AM IST)**—less than 12 hours away. This breakdown covers the verified telemetry, rules, model specifications, and deployment tactics needed to lock in all four bonus categories.

### 1. Going Viral on X with Developer-Tool Demos ($20K Prize)

For the **Most Viral Project ($20K)** prize, organizers sum **likes + reposts + comments** on your submitted X demo post until **November 8 EOD**. You must submit the X post URL directly on the Cerebral Valley submission form before the 9:00 PM ET deadline.

* **Length & Format:** Although the Cerebral Valley platform requires a **3-minute max** demo video (judges enforce a hard stop at 3:00 and recommend aiming for **2:50–2:55**), native X engagement peaks at **45–90 seconds** for the feed video, or by front-loading the most explosive 15 seconds of your 2:55 video.
* **First 3 Seconds:** Open with a split-screen visual of the **simulated world breaking** (fault injected on the left, agent autonomously diagnosing and recovering in real time on the right). *Crucial Hackathon Rule:* For the official submission video, organizers require an **opening 1-line provenance statement in the first 10 seconds** (spoken + on-screen caption) stating that the project was built 100% from scratch during Oct 26–Nov 4 (or explicitly separating pre-existing vs. new work) to avoid immediate disqualification.
* **Captions:** Hardcode high-contrast subtitles (e.g., via Remotion or CapCut). Over 80% of developer-tool videos on X are watched on mute initially.
* **Single Post vs. Thread:** Make a **single hero post** containing the native MP4 video, a 3-bullet summary of the chaos-engineering problem solved, and no external links in the main body (X's algorithm penalizes external URLs in the root post). Put your GitHub repo, live demo URL, architecture diagram, and tagged accounts in **Reply #1**, and ask a technical question in **Reply #2** to seed the comment counter.
* **Who to Tag:** Tag `@AIatMeta`, `@CerebralValley`, and your verified inference partner (`@basetenco`, `@modal_labs`, or `@FireworksAI_HQ`), plus workshop leads/speakers from the hackathon (`@SinanOzdemir`, `@subramen` / Suraj Subramanian). Put tags at the bottom of the root post or in Reply #1 so the hook reads cleanly.
* **Best Posting Window (US + India Overlap):** **8:30 AM – 10:00 AM ET (7:00 PM – 8:30 PM IST)**. Because you must include the X link in your submission before 9:00 PM ET tonight, post your video by **8:15 PM ET (6:45 AM IST)** prior to submitting, then Quote-Repost it tomorrow during the 8:30 AM ET / 7:00 PM IST window to drive secondary waves into the original post.

**Social & Video Links (1 Line Each — Top 3 Marked):**

* ⭐ **[TOP 1 TO OPEN] YouTube (`youtu.be/nHpTvlQewb4`)** — *TBWIM: Meta AI Developer Hackathon 2026 3-Min Demo*: Live 3-minute submission example showing how top contenders structure description links (live URL, public JSONL dataset, Meta API spending ledger, and autonomous agent contract).
* ⭐ **[TOP 2 TO OPEN] YouTube (`youtu.be/NTTN8SVFPgw`)** — *ProxyHubb: Meta Global AI Developer Hackathon Demo*: Indie track submission demonstrating exact compliance with the provenance disclosure rule (separating pre-existing infra from new Muse Spark + MCP features).
* ⭐ **[TOP 3 TO OPEN] YouTube (`youtu.be/E9I7kA9nHVM`)** — *What's Left Ep. 05 with Mike Brown (1st Place Winner, Cerebral Valley Claude Code Hackathon)*: Breaks down how his winning demo and adversarial multi-agent planning beat 13,000 applicants.
* **YouTube (`youtu.be/0uW7S5VdzK0`)** — *Backstep: Meta Global AI Developer Hackathon Demo*: Visual physical-world "rewind button" teardown demo showing how to hook viewers with frame-exact proof in under 5 seconds.
* **YouTube (`youtu.be/6p7Xl7akJf8`)** — *NULL: Autonomous Evidence Intelligence (Meta Hackathon 2026)*: Indie track agentic demo structured around a four-stage *Prevent → Investigate → Mitigate → Verify* workflow.
* **X Mirror (`michaelguo.vercel.app/xposts.html` / `@michaelzsguo`)** — *Claude Code Hackathon Winners Breakdown (199.4K views, 80 likes, 18 saves)*: Viral X post analyzing why domain-specific tools built by practitioners swept Cerebral Valley's flagship AI hackathon.
* **X (`x.com/ClaudeDevs` & `x.com/oikon48` via `youtu.be/hH0amSzSrJU`)** — *Cerebral Valley x Anthropic Hackathon Launch & Demo Threads*: Official builder showcase feeds used to amplify participant demos.
* *What couldn't be found:* Direct URLs to individual X demo posts with exact final like/repost counts for the current Meta hackathon, as the viral counting window just opened and runs through Nov 8 EOD.

---

### 2. Cerebral Valley Public Gallery & Community Vote ($10K Prize)

* **How the Gallery & Voting Work:** According to the official Participant Guide (`cerebralvalley.ai/e/meta-ai-developer-hackathon`) and schedule, initial judging runs **Nov 5 – Nov 8**. On **Monday, Nov 9 at 12:00 PM ET**, the closing ceremony reveals the Top 10 finalists and **officially opens the Community Vote** on the public project gallery (though gallery voting mechanics go live post-submission). Each **verified Cerebral Valley account** gets **1 vote** (authenticated via GitHub/Google/email verification on `cerebralvalley.ai`), and the winner is announced **Monday, Nov 16** on Discord.
* **How Past Winners Gathered Votes:** In prior 2026 Cerebral Valley hackathons ([Built with Opus 4.6 Gallery](https://cerebralvalley.ai/e/claude-code-hackathon/hackathon/gallery) and [Google I/O Hackathon Gallery](https://cerebralvalley.ai/e/google-io-hackathon/hackathon/gallery)), top-voted projects converted traffic by:
1. **Zero-Auth "Try Demo" Buttons:** Gallery cards display your title, a 2-line snippet, and your links. Winners like *Forge* (`forge.brandt-thomassen.dk`) and *Serve* (`play.theminimallab.com`) placed a one-click **"Try Demo Account (No Login Needed)"** button right at the top of their description.
2. **In-App Vote Banner:** Placing a subtle top banner inside the live web app: *"Testing an agent? If this fault-injection sandbox helped you, vote for us on Cerebral Valley (1 click)."*
3. **Discord & Niche Community Mobilization:** Sharing the direct gallery card link (`cerebralvalley.ai/e/meta-ai-developer-hackathon/hackathon/gallery/[id]`) in the hackathon's `#showcase` Discord channel, Reddit (`r/LocalLLaMA`, `r/AI_Agents`), and WhatsApp/Discord university/alumni groups.


* **Rules Against Vote Solicitation:** Organic promotion on X, LinkedIn, Discord, and communities is **explicitly encouraged** (and required for the $20K Viral prize). However, the Event Waiver and Rules strictly forbid *"duplicate applications, false identity information, or spam"* and state that violating platform policies results in immediate disqualification at the Sponsor's sole discretion. Do **not** use bot farms, temporary email generators, or paid upvote services—Cerebral Valley deduplicates votes by verified identity and flags burst voting from disposable accounts.

---

### 3. Muse Spark Open & Muse Glimmer Technical Specs ($10K Open-Weight Prize)

#### Muse Spark Open (`816B Total / 42B Active MoE`, 1M Context, Multimodal)

Released October 15, 2026 (`meta-models/Muse-Spark-Open-816B-A42B` on Hugging Face), Muse Spark Open is an 816B-parameter Mixture-of-Experts model with **42B active parameters per token** and a **1,048,576-token context window** (up to 131K–944K max output tokens depending on provider).

| Specification | Baseten | Modal Endpoints | Fireworks AI |
| --- | --- | --- | --- |
| **Hackathon Model ID** | `meta-models/muse-spark-open-1026` (Standard catalog: `meta-models/muse-spark-open`) | `meta-models/Muse-Spark-Open-816B-A42B` | `accounts/fireworks/models/muse-spark-open` |
| **OpenAI Tool Calling** | Yes (Full OpenAI-compatible `tools` schema) | Yes | Yes (`0.17%` tool call error rate—lowest measured) |
| **Structured Outputs** | Yes (`response_format` JSON schema) | Yes | Yes (`0.60%` structured output error rate) |
| **Pricing (In / Out / Cache)** | `$1.25` / `$4.25` / `$0.15` per 1M tokens | `$1.25` / `$4.25` / `$0.15` per 1M tokens | `$1.25` / `$4.25` / `$0.15` per 1M tokens |
| **Latency (TTFT) & Speed** | `0.40s–0.47s` TTFT, `~173 tok/s` throughput (`6.45s` avg E2E) | `0.47s–0.56s` TTFT, `148–209 tok/s` (`3.54s` avg E2E) | `0.47s–0.96s` TTFT, `~53 tok/s` throughput |
| **Hackathon Credits & Limits** | `$100` credits, **2,000,000 TPM (2M tokens/min)** rate limit | `$100` credits (serverless auto-scaling) | `$25–$100` credits (tier-dependent rate limits) |

*Sources: [Meta AI Docs](https://dev.meta.ai/docs/muse-spark-open/deploy), [OpenRouter Provider Telemetry](https://openrouter.ai/meta/muse-spark-open), [Models.dev](https://models.dev/models/meta/muse-spark-open), [LiteLLM Pricing](https://models.litellm.ai/models/muse-spark-open), and the [Official Participant Guide](https://github.com/oherna25/trading_bot_project/blob/main/hack_info.md). (Note: Precise RPM limits for Modal and Fireworks hackathon tiers were not publicly disclosed).*

* **CRITICAL Model ID Rule:** The official hackathon participant guide explicitly warns: *"CRITICAL: Docs use `meta-models/muse-spark-open`. For this hackathon, use `meta-models/muse-spark-open-1026` instead."* On Baseten, pass `model="meta-models/muse-spark-open-1026"` (supports environment fallback if routing via standard Baseten Model APIs).
* **Reasoning Output Format:** Muse Spark Open uses `--reasoning_parser musespark`. In OpenAI-compatible chat completions, the chain-of-thought is returned in a separate **`reasoning_content`** field on the message/delta object (alongside standard `content` and `tool_calls`), and supports reasoning effort parameters (`low`, `medium`, `high`, `xhigh`, defaulting to `xhigh`). Beware that if `max_tokens` is set too low on `xhigh` effort, the model can produce a reasoning-only turn (`reasoning_content` populated, `content: null`).

#### Muse Glimmer (`30B Dense`, Local / Edge Agentic Model)

* **Architecture & Size:** `meta-models/Muse-Glimmer-30B` (Apache 2.0) is a **dense causal transformer with 29.6B total parameters** (including a ~1.8B frozen multimodal perception encoder capable of 4,096 visual tokens per image).
* **Can it run on a laptop GPU or CPU?** **Yes.** At full BF16 precision it requires ~55 GB VRAM, so Meta released two official 4-bit GGUF/quantized variants alongside community `llama.cpp` and `Ollama` builds:
* **`K-Quant-Dynamic` (32 GB VRAM/Unified Memory):** 0.2% quality degradation from BF16.
* **`K-Quant-17GB` / `Q4_K_M` (~17.9–18.3 GB file, fits 24 GB GPU or 24–32 GB Mac):** ~1.0% degradation.
* **`IQ2_M` / `IQ3_XXS` (10.5–12.0 GB file):** Runs on **12–16 GB laptop GPUs or 16 GB Apple Silicon Macs**.


* **Measured Local Speeds ([GingerLabs](https://gingerlabs.ai/blog/meta-muse-glimmer-performance-and-hardware-requirements-for-local-ai) & [LLMRun Benchmarks](https://llmrun.dev/model/meta-models-muse-glimmer-30b)):**
* **Apple M4 Max (Unified Memory):** `23.7 tok/s` baseline, jumping to **`37.8 tok/s`** using Meta's **DFlash** block-speculative decoding drafter head.
* **16 GB Mac (`IQ2_M`):** **`~13 tok/s`**.
* **Laptop/Desktop GPU (24 GB RTX 3090/4090 `Q4_K_M`):** **`33–36 tok/s`** (and up to `233.4 tok/s` on RTX 5090 with DFlash).
* **Pure Laptop CPU (x86 DDR5 RAM, no GPU offload):** Not officially benchmarked by Meta, but a 30B dense Q4 model (~18 GB weights) is memory-bandwidth bound at **`~2–4 tok/s`** on dual-channel laptop DDR5 RAM—too slow for live multi-turn agent loops unless using `IQ2_XXS` (~8.7 GB) or offloading to Baseten/Fireworks.



#### What Counts as "Verified" Open-Weight Use?

In Round 1 judging, projects competing for **Best Build on Meta Open Weight Models ($10K)** are flagged and cross-checked with the **infrastructure partners (Baseten, Modal, Fireworks)**.

1. **API Telemetry Match:** The partner checks your account's API request logs against the model ID (`meta-models/muse-spark-open-1026` on Baseten, `accounts/fireworks/models/muse-spark-open` on Fireworks, or Modal container/endpoint logs) to confirm real runtime inference occurred during the build/judging window.
2. **Load-Bearing Runtime Role:** Per the [Submission & Judging Checklist](https://artifacts.watt-mind.com/meta-hackathon-2026/checklist), the open-weight model must be **load-bearing at runtime** (not a dummy ping, and hosted SAM 3.1 alone does not qualify). Show live telemetry/logs of the `muse-spark-open-1026` calls on screen during your 3-minute video.

---

### 4. Winning "Best Agentic Use-Case" ($5K) in 2026

Judging weights are **Impact (30%)**, **Use of Meta AI Tooling (25%)**, **Demo (25%)**, and **Technical Depth & Execution (20%)**. Based on Meta's hackathon workshops (*Agents 102: Best Practices for Verifiable Agents* by Yash Khare) and 2026 Cerebral Valley winners, judges actively penalize single-prompt wrappers and look for four specific patterns:

* **Adversarial Planner / Critic Loops:** First-place Claude Code Hackathon winner **CrossBeam** (Mike Brown) won by running an adversarial loop where one agent generates an execution plan, a second critic model predicts how that plan will fail in production, and the planner patches the holes before acting. Your fault-injection simulator naturally embodies this: use **Muse Spark Open (`meta-models/muse-spark-open-1026`)** as the **Chaos/Adversary Agent** that analyzes the target agent's trajectory and injects targeted faults (API 503s, corrupted tool payloads, stale state, prompt injections), and a second agent (or **Muse Glimmer 30B**) as the **SUT (System Under Test) / Recovery Verifier**.
* **Deterministic Verification Gates:** Top 2026 submissions pair LLM reasoning with deterministic verification:
* **Forge Proof (`botsec`):** Combined LLM agents with formal verification to mathematically prove smart contract vulnerabilities.
* **Expectrail & CerebralValleyMetaHackathon2026 (`sramey`):** Every AI-generated action is playtested by a scripted bot across 3 seeds × 2 simulated worlds in 1–6 ms; 93% of builds (162/175) pass deterministic verification before landing.


* **Budgeted Autonomy & Spend Guards:** Judges love explicit safety envelopes. Top contender **Serve (`gino6178`)** implemented Ed25519-signed agent actions where an agent can tighten budget/latency constraints autonomously, but attempting to loosen a limit returns `403 · needs: person`. Adding a visible **token/retry/cost budget ledger** (`BUDGET_MAX_TOKENS`, `MAX_RECOVERY_STEPS`) to your UI directly hits "practical usefulness."
* **Quantitative A/B Benchmarks in README & Video:** Organizers explicitly noted that quantitative benchmarks (e.g., *"Pass@1 recovery rate across 50 injected faults: Baseline Agent 34% vs. Fault-Hardened Agent 88%, median recovery latency 4.2s"*) heavily boost the 20% Technical Depth score.

---

### 5. Cheapest, Fastest Public Deployment Tonight (FastAPI + React/Vite)

Because judges hate cold starts during a 3-minute evaluation—and **Render's free tier spins down after 15 minutes of inactivity with a 30–60 second cold start**—do not put your FastAPI backend on a sleeping free tier.

| Platform | Cost Tonight | Cold Start Behaviour | FastAPI + React/Vite Gotchas & Verdict |
| --- | --- | --- | --- |
| **Railway (Hobby / Trial)** | `$0` (`$5` free credit) or `$5/mo` Hobby | **Zero cold start** (always-on container) | **BEST FOR BACKEND.** Deploy FastAPI + WebSockets/SSE in 3 minutes from GitHub. Can serve built Vite `dist/` directly via FastAPI `StaticFiles` on one URL (zero CORS headaches). |
| **Vercel (Frontend) + Railway/Modal (API)** | `$0` | **Zero cold start** on static Vite SPA | **BEST SPLIT SETUP.** Vercel deploys React/Vite in 45 seconds on a global CDN. Set `VITE_API_URL` to your Railway or Modal backend. Watch out for Vercel serverless 10–60s timeouts if proxying SSE streams. |
| **Modal (Web Endpoints)** | `$0` (Uses your `$100` hackathon credit) | `1–3s` warm/fast-boot, **`0s` with `keep_warm=1**` | **DOUBLE-DUTY BONUS.** Decorating your FastAPI app with `@modal.asgi_app(keep_warm=1)` gives you an instant `*.modal.run` URL, zero cold starts, AND logs runtime compute directly on a hackathon inference partner! |
| **Fly.io** | `$0–$2` (Pay-as-you-go) | `1–3s` Firecracker wake, or `0s` (`auto_stop_machines = false`) | Rock-solid for WebSockets and stateful simulations, but requires `flyctl` CLI auth and a Dockerfile. Skip unless you already have `flyctl` configured locally. |
| **Hugging Face Spaces (Docker)** | `$0` (2 vCPU, 16 GB RAM) | Sleeps after 48h idle (**zero cold start tonight**) | Generous RAM for in-memory world simulations, but forces port `7860`, runs behind an iframe by default (unless visiting the direct `*.hf.space` subdomain), and builds Docker images slowly. |
| **Render (Free Web Service)** | `$0` (or `$7/mo` Starter) | **30–60s cold start** after 15m idle on Free | **AVOID FREE TIER.** A 50-second hang when a judge clicks your link kills your Demo score (25%). Only use Render if you pay `$7` for the Starter instance or ping `/health` every 5 minutes via cron-job.org. |

**Fastest Winning Architecture Tonight (15 Minutes Total):**
Build your React/Vite frontend (`npm run build`) and mount its `dist/` folder directly in FastAPI (`app.mount("/", StaticFiles(directory="dist", html=True))`), or deploy the frontend to **Vercel** and backend to **Modal (`keep_warm=1`)** or **Railway**. Crucially, add a **"Replay Pre-Recorded Fault Trace (Instant / Keyless)"** button on the landing page alongside the **"Run Live Muse Spark Open Chaos Test"** button so judges and Community Voters can see rich visual execution in 0.5 seconds without burning your API credits or hitting rate limits.

---

### Top 5 Priority Actions for the Next 10 Hours

1. **Hours 0–2: Wire `meta-models/muse-spark-open-1026` on Baseten + Dual-Model Telemetry ($10K Open-Weight + $5K Agentic)**
Verify your backend calls the exact hackathon model ID **`meta-models/muse-spark-open-1026`** on Baseten (and optionally a second Meta model like Muse Glimmer 30B or Muse Spark 1.3 for critic/verification). Surface a live UI panel in your app showing the exact model ID, `reasoning_content` trace, token budget guard, and latency for every injected fault and recovery step.
2. **Hours 2–4: Run a 30-Episode Fault Benchmark & Add Instant "Keyless Replay Mode" (20% Tech Depth + $10K Community Vote)**
Run your agent through 25–50 automated fault-injection scenarios right now (e.g., tool timeout, malformed JSON, hallucinated state, rate-limit 429) to generate real Baseten partner logs and save the JSON execution traces into your repo. Put a summary benchmark table in your README and wire a **1-click "Replay Saved Chaos Run (No Login / Key Needed)"** button into your UI so judges and voters experience instant playback.
3. **Hours 4–5.5: Deploy Publicly with Zero Cold Starts & Clean the Public GitHub Repo**
Deploy your FastAPI + React app to **Railway** or **Modal (`keep_warm=1`)** + **Vercel**. Confirm there is zero OAuth/login wall. Make your GitHub repository **public**, verify `.env.example` is clean (no leaked keys), and add a 3-command local Quickstart plus an explicit **"Built During Oct 26 – Nov 4 Hackathon"** provenance section at the very top of `README.md`.
4. **Hours 5.5–8: Record & Caption the 2:50 Demo Video (25% Demo Rubric + $20K Viral)**
Record a crisp **2:50–2:55** walkthrough (strictly under 3:00).
* **0:00–0:10:** State verbally and on-screen: *"Built 100% from scratch during the Meta Global AI Developer Hackathon (Oct 26–Nov 4)"* while showing a live simulated world breaking under fault injection.
* **0:10–1:45:** Live product execution (no slide decks)—inject a fault, show `meta-models/muse-spark-open-1026` reasoning and tool-calling live on Baseten, and show the deterministic verifier confirming recovery.
* **1:45–2:50:** Show the quantitative benchmark results (before vs. after fault-hardening) and architecture diagram. Burn in subtitles.


5. **Hours 8–9.5: Publish the X Demo Post & Submit on Cerebral Valley Before 8:30 PM ET (30-Min Buffer)**
Upload your video natively to **X** with a punchy chaos-testing hook, tag `@AIatMeta`, `@CerebralValley`, and `@basetenco`, and put your live demo and GitHub links in Reply #1. Copy that X post URL, your public GitHub URL, and your video link into the **Cerebral Valley submission portal before 8:30 PM ET (7:00 AM IST)**—never wait until 8:59 PM ET when portal uploads lag.
