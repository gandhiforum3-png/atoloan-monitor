# Claude Integration Approaches — Atoloan Monitor

Comparing two ways to use Claude in an autonomous SRE monitoring agent.

---

## The Two Approaches

### Approach 1 — Claude as Diagnoser (Structured Decision)

```
Signals → Orchestrator bundles them → Claude (1 call) → DiagnosisResult → Python executes
```

Claude receives a pre-assembled bundle of signals and returns a single structured
decision. Python enforces all safety constraints and executes the action.

**What Claude sees:**
- Infrastructure topology (cached)
- Bundled signals from the last 30 seconds
- Pod/node context fetched by the orchestrator

**What Claude returns:**
```json
{
  "root_cause": "Node has memory pressure, atoloan-api consuming 95% of node RAM",
  "confidence": 0.82,
  "action_type": "deployment_scale_down",
  "recommended_action": "Scale atoloan-backend-prod/atoloan-api from 2 to 1 replica",
  "estimated_blast_radius": "service"
}
```

**Python then:**
1. Checks confidence against threshold (≥ 0.85 for scale down)
2. Runs `safety_check()` hard stop
3. Runs pre-flight (replica count, PDB, cooldown)
4. Executes K8s API call or escalates to human

---

### Approach 2 — Claude as Agent (Tool Loop)

```
Signals → Claude decides what tools to call → calls them → decides again → ... → done
```

Claude is given a set of tools and drives the entire investigation and remediation
itself, calling tools in whatever sequence it determines is needed.

**Tools Claude can call:**
- `get_node_status()` — fetch live node conditions
- `get_pods_on_node()` — see what's running
- `get_pod_logs(pod_name)` — read application logs
- `restart_deployment(namespace, name)` — trigger rolling restart
- `scale_deployment(namespace, name, replicas)` — adjust replica count
- `verify_pod_healthy(namespace, name)` — confirm fix worked
- `escalate_human(diagnosis, urgency)` — hand off to operator

**A typical Claude agent turn sequence:**
```
Turn 1: [receives MemoryPressure signal]
        → calls get_node_status()

Turn 2: [sees node at 94% memory]
        → calls get_pods_on_node()

Turn 3: [sees atoloan-api using 900Mi]
        → calls scale_deployment("atoloan-backend-prod", "atoloan-api", 1)

Turn 4: [receives scale confirmation]
        → calls verify_pod_healthy("atoloan-backend-prod", "atoloan-api")

Turn 5: [pod healthy, memory pressure clearing]
        → returns final summary
```

---

## Cost Comparison

### Token usage per incident

| | Approach 1 | Approach 2 |
|---|---|---|
| API calls | 1 | 4–8 |
| Input tokens (system prompt, cached) | ~800 | ~800 × N calls |
| Input tokens (context, uncached) | ~500 | ~500 growing per turn |
| Output tokens | ~300 | ~100 per turn |
| **Total tokens per incident** | **~1,600** | **~6,000–12,000** |

### Cost per incident (Claude Sonnet 4.6 pricing)

| Token type | Rate | Approach 1 | Approach 2 |
|---|---|---|---|
| Cache write | $3.75/MTok | $0.003 (first call only) | $0.003 |
| Cache read | $0.30/MTok | $0.00024 | $0.00120 |
| Uncached input | $3.00/MTok | $0.0015 | $0.0105 |
| Output | $15.00/MTok | $0.0045 | $0.0090 |
| **Total** | | **~$0.007** | **~$0.021** |

### Monthly cost at scale

| Volume | Approach 1 | Approach 2 |
|---|---|---|
| 100 incidents/month | $0.70 | $2.10 |
| 300 incidents/month | $2.10 | $6.30 |
| 1,000 incidents/month | $7.00 | $21.00 |

> **Cost is not the deciding factor.** Both approaches cost under $25/month
> even at high volume. The architectural tradeoffs matter far more.

---

## Pros and Cons

### Approach 1 — Claude as Diagnoser

| | Detail |
|---|---|
| ✅ **Fast** | 3–5 seconds from signal to action. One API call. |
| ✅ **Predictable cost** | Always 1 call, fixed token range, no surprises. |
| ✅ **Reliable** | 1 failure point. No cascading timeouts or rate limits. |
| ✅ **Safety is pure Python** | Confidence gate, `safety_check()`, and pre-flight all run outside the LLM. Claude cannot influence them. |
| ✅ **Easy to audit** | One input, one output, one logged decision per incident. |
| ✅ **Works well for known conditions** | MemoryPressure → scale down is a well-understood mapping. |
| ❌ **Claude works blind** | Cannot ask "let me check pod logs first." Gets only what the orchestrator assembled. |
| ❌ **Context quality determines diagnosis quality** | If the orchestrator sends incomplete signals, there is no recovery path. |
| ❌ **No self-correction** | If the first diagnosis is wrong, there is no loop to catch it. |
| ❌ **Escalates novel failures** | Anything outside known signal patterns gets routed to a human, even if one more data point would resolve it. |

---

### Approach 2 — Claude as Agent

| | Detail |
|---|---|
| ✅ **Claude gathers its own context** | Can call `get_pod_logs()`, `describe_node()`, `get_metrics()` as needed. |
| ✅ **Handles novel situations** | Can investigate failure patterns it has never seen before. |
| ✅ **Self-verifying** | Calls `verify_pod_healthy()` after acting, confirms the fix worked. |
| ✅ **Mirrors real SRE behaviour** | Investigate → hypothesis → act → verify is how humans debug. |
| ✅ **Better for cross-domain incidents** | Can pull signals from K8s, DB, and EC2 in one investigation without prior correlation. |
| ❌ **Slow** | 20–45 seconds per incident across 5+ API calls. Unacceptable when pods are actively being evicted. |
| ❌ **Unpredictable token usage** | Claude may call 3 tools or 10 depending on what it finds. |
| ❌ **Multiple failure points** | Each tool call is a network hop. Rate limits, timeouts, and transient errors compound. |
| ❌ **Larger safety surface** | Claude decides when to call `restart_deployment()`. Tool implementations still have safety checks, but the call sequence is LLM-driven. |
| ❌ **Hard to debug** | A 6-turn conversation with interleaved tool results is harder to trace than 1 input → 1 output. |
| ❌ **Prompt injection risk** | If a tool returns crafted content (e.g. a pod name that looks like an instruction), Claude may act on it. |
| ❌ **Runaway loop risk** | Without a hard turn limit, Claude can keep calling tools if it cannot converge. |

---

## When to Use Each

| Situation | Approach |
|---|---|
| Condition type is well-understood (MemoryPressure, CrashLoop, slow query) | **Approach 1** |
| Action must execute in under 10 seconds | **Approach 1** |
| Single infrastructure domain involved | **Approach 1** |
| Confidence threshold system already maps signal → action | **Approach 1** |
| 2+ infrastructure layers failing simultaneously | **Approach 2** |
| Root cause requires reading logs or metrics not in the signal bundle | **Approach 2** |
| Post-incident investigation or postmortem generation | **Approach 2** |
| Novel failure with no prior pattern match | **Approach 2** |

---

## What Atoloan Monitor Does (Hybrid)

The system uses both approaches at different layers, matching the right tool to
each problem type.

```
Phase 1–3  →  Approach 1 for all known signal types
               Observer detects condition
               Orchestrator debounces 30 seconds
               Claude Sonnet diagnoses in 1 call
               Python gates, pre-flights, and executes
               Total time: 35–40 seconds (mostly debounce wait)

Phase 2    →  Approach 2 only for cross-domain incidents
               Two or more domains have active incidents
               Claude Opus with extended thinking investigates
               Claude calls tools to pull correlated signals
               Human escalation if confidence does not converge
               Total time: 60–120 seconds (acceptable for complex incidents)

Phase 4    →  Approach 2 for postmortem generation
               Not time-critical — quality matters more than speed
               Claude investigates the full incident timeline
               Produces root cause summary and follow-up actions
```

The 30-second debounce window in the orchestrator is what makes Approach 1 work well
for known conditions: by the time Claude is called, the relevant signals have already
been assembled. Claude does not need to go fetch them.

---

## Architecture Safety Summary

Both approaches enforce the same hard constraints. The difference is *where* the
decision to act originates.

```
Approach 1                          Approach 2
──────────────────────────────      ──────────────────────────────
Claude outputs a decision           Claude calls a tool
Python reads the decision           Tool implementation runs:
Python calls safety_check()           safety_check()         ← same
Python runs pre-flight                pre-flight checks      ← same
Python calls K8s API                  K8s API call           ← same

Safety constraints: same            Safety constraints: same
Decision origin: Claude output      Decision origin: Claude tool call
```

The hard-stop `FORBIDDEN_OPERATIONS` blocklist and pre-flight checks run in Python
regardless of which approach is used. Claude cannot bypass them in either pattern.

---

*Document created: 2026-06-14*
*Applies to: Atoloan Monitor v1, Phase 1–4*
