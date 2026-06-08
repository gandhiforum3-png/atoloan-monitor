import { useState, useEffect, useRef, useCallback } from "react";

const API = import.meta.env.VITE_AGENT_URL || "http://localhost:8000";

const ENVS = {
  local: { label: "local · minikube",         color: "#3B6D11" },
  dev:   { label: "aws-dev · 3.135.161.145",   color: "#185FA5" },
  uat:   { label: "aws-uat · uat.atoloan.com", color: "#BA7517" },
  prod:  { label: "aws-prod · atoloans.com",   color: "#A32D2D" },
};

const LAYERS = [
  { id: "all",      label: "All layers" },
  { id: "k8s",      label: "Kubernetes" },
  { id: "ec2",      label: "EC2" },
  { id: "postgres", label: "Postgres" },
  { id: "secrets",  label: "Secrets" },
  { id: "ingress",  label: "Ingress" },
];

const QUICK_CHECKS = [
  { label: "Pod health",      issue: "Check all pod statuses across namespaces. Are there any CrashLoopBackOff, Pending, or OOMKilled pods?", layer: "k8s" },
  { label: "Secret sync",     issue: "Check ExternalSecret sync status. Are all secrets synced from AWS Secrets Manager?", layer: "secrets" },
  { label: "Restarts / OOM",  issue: "Show recent pod restart counts and OOMKill events.", layer: "k8s" },
  { label: "Ingress status",  issue: "Check ingress configuration and ADDRESS status. Is nginx ingress showing an external IP?", layer: "ingress" },
  { label: "Node resources",  issue: "Check node resource usage — CPU, memory, allocatable vs requested.", layer: "k8s" },
  { label: "Postgres logs",   issue: "Check postgres pod logs for errors, slow queries, or connection failures.", layer: "postgres" },
  { label: "CoreDNS (local)", issue: "Check CoreDNS custom host IPs after minikube restart. Do the IPs in coredns-patch.yaml need updating?", layer: "k8s" },
];

function severityColor(s)  { return s === "critical" ? "#A32D2D" : s === "warning" ? "#854F0B" : "#27500A"; }
function severityBg(s)     { return s === "critical" ? "#FCEBEB" : s === "warning" ? "#FAEEDA" : "#EAF3DE"; }
function severityBorder(s) { return s === "critical" ? "#F7C1C1" : s === "warning" ? "#FAC775" : "#C0DD97"; }
function ts() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }

export default function App() {
  const [env, setEnv]         = useState("dev");
  const [layer, setLayer]     = useState("all");
  const [input, setInput]     = useState("");
  const [loading, setLoading] = useState(false);
  const [streamText, setStream] = useState("");
  const [status, setStatus]   = useState(null);
  const [history, setHistory] = useState([{
    id: 0, role: "agent", time: ts(),
    text: "Ready. I run diagnostics automatically and fix simple issues on the spot. For anything bigger I'll tell you exactly what needs your attention.",
    diagnosis: null, execResults: null,
  }]);
  const chatRef  = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => { if (chatRef.current) chatRef.current.scrollTop = chatRef.current.scrollHeight; }, [history, streamText]);
  useEffect(() => { loadStatus(env); }, [env]);

  async function loadStatus(e) {
    try {
      const r = await fetch(`${API}/status/${e}`);
      setStatus(await r.json());
    } catch { setStatus(null); }
  }

  function addMsg(role, text, diagnosis = null, execResults = null) {
    setHistory(h => [...h, { id: Date.now() + Math.random(), role, time: ts(), text, diagnosis, execResults }]);
  }

  async function runCommand(cmd) {
    try {
      const r = await fetch(`${API}/action/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ env, command: cmd }),
      });
      const d = await r.json();
      addMsg("agent", `Ran: \`${cmd}\`\n\n${d.success ? "✓" : "✗"} ${d.output}`);
      setTimeout(() => loadStatus(env), 2500);
    } catch (e) {
      addMsg("agent", `Failed to run command: ${e.message}`);
    }
  }

  async function scaleDeployment(deployment, namespace, replicas) {
    const r = await fetch(`${API}/action/scale`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ env, deployment, namespace, replicas }),
    });
    const d = await r.json();
    addMsg("agent", `Scaled ${deployment} → ${d.replicas} replicas.\n\n${d.output}`);
    setTimeout(() => loadStatus(env), 3000);
  }

  const diagnose = useCallback(async (issue, lyr = layer) => {
    if (loading) return;
    setLoading(true);
    setStream("");
    addMsg("user", issue);

    try {
      const resp = await fetch(`${API}/diagnose/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ env, issue, layer: lyr }),
      });

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let accumulated = "";
      let finalDiagnosis = null;
      let execResults = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const lines = decoder.decode(value).split("\n");
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const evt = JSON.parse(line.slice(6));
            if (evt.type === "status") {
              setStream(evt.message);
            } else if (evt.type === "token") {
              accumulated += evt.text;
              setStream("Analysing... " + accumulated.slice(-120));
            } else if (evt.type === "done") {
              finalDiagnosis = evt.diagnosis;
              execResults = evt.execution_results;
            }
          } catch {}
        }
      }

      setStream("");
      if (finalDiagnosis) {
        const d = finalDiagnosis;
        const autoFixed = (execResults || []).filter(r => r.auto_executed && r.success).length;
        const autoFailed = (execResults || []).filter(r => r.auto_executed && !r.success).length;
        const needsHuman = d.requires_human_intervention;

        let summary = d.summary || "Diagnosis complete.";
        if (autoFixed > 0) summary += ` Auto-fixed ${autoFixed} issue${autoFixed > 1 ? "s" : ""}.`;
        if (autoFailed > 0) summary += ` ${autoFailed} action${autoFailed > 1 ? "s" : ""} failed.`;

        addMsg("agent", summary, d, execResults);
        setTimeout(() => loadStatus(env), 2000);
      } else {
        addMsg("agent", "Diagnosis complete — see output above.");
      }
    } catch (e) {
      addMsg("agent", `Error connecting to agent backend at ${API}: ${e.message}`);
    } finally {
      setLoading(false);
      setStream("");
    }
  }, [env, layer, loading]);

  function handleSend() {
    const t = input.trim();
    if (!t || loading) return;
    setInput("");
    diagnose(t);
  }

  const envCfg = ENVS[env];
  const problemCount = status?.problem_pods?.length ?? 0;
  const envNs = {
    local: { "atoloan-api": "atoloan-backend", "atoloan-ui": "atoloan-frontend" },
    dev:   { "atoloan-api": "atoloan-backend-dev", "atoloan-ui": "atoloan-frontend-dev" },
    uat:   { "atoloan-api": "atoloan-backend-uat", "atoloan-ui": "atoloan-frontend-uat" },
    prod:  { "atoloan-api": "atoloan-backend-prod", "atoloan-ui": "atoloan-frontend-prod" },
  }[env];

  return (
    <div style={{ display: "flex", height: "100vh", fontFamily: "system-ui,sans-serif", fontSize: 14, background: "#f7f6f2", color: "#1a1a18" }}>

      {/* Sidebar */}
      <aside style={{ width: 216, borderRight: "1px solid #e2dfd8", background: "#fff", display: "flex", flexDirection: "column", flexShrink: 0 }}>
        <div style={{ padding: "16px 14px 12px", borderBottom: "1px solid #e2dfd8" }}>
          <div style={{ fontWeight: 700, fontSize: 15 }}>atoloan</div>
          <div style={{ fontSize: 11, color: "#999", marginTop: 1 }}>SRE Agent · autonomous</div>
        </div>

        <div style={{ padding: "10px 10px 4px" }}>
          <SideLabel>Environment</SideLabel>
          {Object.entries(ENVS).map(([id, cfg]) => (
            <SideBtn key={id} active={env === id} color={cfg.color} onClick={() => setEnv(id)}>
              <Dot color={cfg.color} /> {id}
              {id === "prod" && <span style={{ marginLeft: "auto", fontSize: 10, color: "#999" }}>human only</span>}
            </SideBtn>
          ))}
        </div>

        <div style={{ height: 1, background: "#e2dfd8", margin: "6px 0" }} />

        <div style={{ padding: "0 10px 6px" }}>
          <SideLabel>Quick checks</SideLabel>
          {QUICK_CHECKS.map((q, i) => (
            <SideBtn key={i} disabled={loading} onClick={() => diagnose(q.issue, q.layer)}>
              {q.label}
            </SideBtn>
          ))}
        </div>

        {/* Status panel */}
        <div style={{ marginTop: "auto", padding: "10px 12px 14px", borderTop: "1px solid #e2dfd8" }}>
          <SideLabel>Cluster</SideLabel>
          {status ? (
            <>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
                <span style={{ color: "#666" }}>Pods</span>
                <span style={{ fontWeight: 600, color: status.pods.running === status.pods.total ? "#3B6D11" : "#A32D2D" }}>
                  {status.pods.running}/{status.pods.total}
                </span>
              </div>
              {problemCount > 0 && (
                <div style={{ background: "#FCEBEB", borderRadius: 5, padding: "4px 8px", fontSize: 11, color: "#791F1F", marginBottom: 4 }}>
                  ⚠ {problemCount} pod{problemCount > 1 ? "s" : ""} need attention
                </div>
              )}
              <button onClick={() => loadStatus(env)} style={{ width: "100%", padding: "4px 0", border: "1px solid #ddd", borderRadius: 5, background: "none", fontSize: 11, color: "#666", cursor: "pointer" }}>↻ Refresh</button>
            </>
          ) : (
            <div style={{ fontSize: 11, color: "#bbb" }}>No connection</div>
          )}
        </div>
      </aside>

      {/* Main */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>

        {/* Top bar */}
        <div style={{ borderBottom: "1px solid #e2dfd8", padding: "10px 18px", background: "#fff", display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: envCfg.color, display: "inline-block" }} />
          <span style={{ fontWeight: 600, fontSize: 13, color: envCfg.color }}>{envCfg.label}</span>
          <div style={{ marginLeft: "auto", display: "flex", gap: 6, flexWrap: "wrap" }}>
            {LAYERS.map(l => (
              <button key={l.id} onClick={() => setLayer(l.id)} style={{
                padding: "4px 11px", borderRadius: 20, border: `1px solid ${layer === l.id ? envCfg.color : "#ddd"}`,
                background: layer === l.id ? envCfg.color + "14" : "transparent",
                color: layer === l.id ? envCfg.color : "#666", cursor: "pointer", fontSize: 12,
                fontWeight: layer === l.id ? 600 : 400,
              }}>{l.label}</button>
            ))}
          </div>
        </div>

        {/* Chat */}
        <div ref={chatRef} style={{ flex: 1, overflowY: "auto", padding: "20px 22px", display: "flex", flexDirection: "column", gap: 16 }}>
          {history.map(msg => (
            <ChatMessage
              key={msg.id}
              msg={msg}
              env={env}
              envNs={envNs}
              envColor={envCfg.color}
              onRunCommand={runCommand}
              onScale={scaleDeployment}
            />
          ))}

          {streamText && (
            <div style={{ display: "flex", gap: 10 }}>
              <Avatar agent />
              <div style={{ maxWidth: "76%", padding: "10px 14px", borderRadius: "11px 11px 11px 3px", background: "#fff", border: "1px solid #e2dfd8", fontSize: 12.5, color: "#555", fontStyle: "italic" }}>
                {streamText}
                <span style={{ display: "inline-block", width: 5, height: 13, background: "#bbb", marginLeft: 3, verticalAlign: "middle", animation: "blink 1s infinite" }} />
              </div>
            </div>
          )}

          {loading && !streamText && (
            <div style={{ display: "flex", gap: 10 }}>
              <Avatar agent />
              <div style={{ padding: "10px 14px", borderRadius: "11px 11px 11px 3px", background: "#fff", border: "1px solid #e2dfd8", fontSize: 12, color: "#bbb" }}>
                Connecting to cluster...
              </div>
            </div>
          )}
        </div>

        {/* Input */}
        <div style={{ borderTop: "1px solid #e2dfd8", padding: "10px 18px", background: "#fff", display: "flex", gap: 8 }}>
          <textarea
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
            disabled={loading}
            placeholder={`Describe an issue in ${env}…`}
            rows={1}
            style={{ flex: 1, padding: "9px 12px", border: "1px solid #ddd", borderRadius: 8, fontSize: 13, fontFamily: "inherit", resize: "none", outline: "none", background: loading ? "#f5f5f3" : "#fff" }}
          />
          <button onClick={handleSend} disabled={loading || !input.trim()} style={{
            padding: "9px 16px", border: "none", borderRadius: 8, background: (!input.trim() || loading) ? "#ddd" : "#1a1a18",
            color: "#fff", fontSize: 13, cursor: (!input.trim() || loading) ? "not-allowed" : "pointer", fontWeight: 500,
          }}>
            {loading ? "..." : "Run ↗"}
          </button>
        </div>
      </div>

      <style>{`
        @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
        textarea:focus { border-color: #aaa !important; }
      `}</style>
    </div>
  );
}

// ── Small components ──────────────────────────────────────────────────────────

function SideLabel({ children }) {
  return <div style={{ fontSize: 10, fontWeight: 600, color: "#bbb", letterSpacing: "0.08em", textTransform: "uppercase", padding: "6px 4px 4px" }}>{children}</div>;
}

function SideBtn({ children, active, color, disabled, onClick }) {
  const [hov, setHov] = useState(false);
  return (
    <button onClick={onClick} disabled={disabled} style={{
      display: "flex", alignItems: "center", gap: 7, width: "100%", padding: "7px 8px",
      border: "none", borderRadius: 6, cursor: disabled ? "not-allowed" : "pointer",
      textAlign: "left", fontSize: 12.5, background: active ? (color + "14") : hov ? "#f5f5f2" : "transparent",
      color: active ? color : "#444", fontWeight: active ? 600 : 400, marginBottom: 1, opacity: disabled ? 0.5 : 1,
    }}
      onMouseEnter={() => setHov(true)} onMouseLeave={() => setHov(false)}
    >{children}</button>
  );
}

function Dot({ color }) {
  return <span style={{ width: 7, height: 7, borderRadius: "50%", background: color, flexShrink: 0, display: "inline-block" }} />;
}

function Avatar({ agent }) {
  return (
    <div style={{
      width: 28, height: 28, borderRadius: "50%", flexShrink: 0,
      background: agent ? "#185FA5" : "#e8e5de",
      color: agent ? "#fff" : "#666",
      display: "flex", alignItems: "center", justifyContent: "center",
      fontSize: 12, fontWeight: 700,
    }}>{agent ? "A" : "U"}</div>
  );
}

// ── Chat message ──────────────────────────────────────────────────────────────

function ChatMessage({ msg, env, envNs, envColor, onRunCommand, onScale }) {
  const isAgent = msg.role === "agent";
  const d = msg.diagnosis;
  const results = msg.execResults || [];

  return (
    <div style={{ display: "flex", gap: 10, flexDirection: isAgent ? "row" : "row-reverse" }}>
      <Avatar agent={isAgent} />
      <div style={{ maxWidth: "80%", display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ fontSize: 11, color: "#bbb", textAlign: isAgent ? "left" : "right" }}>
          {isAgent ? "SRE Agent" : "You"} · {msg.time}
        </div>
        <div style={{
          padding: "10px 14px",
          borderRadius: isAgent ? "11px 11px 11px 3px" : "11px 11px 3px 11px",
          background: isAgent ? "#fff" : "#1a1a18",
          border: isAgent ? "1px solid #e2dfd8" : "none",
          color: isAgent ? "#333" : "#fff",
          fontSize: 13, lineHeight: 1.65, whiteSpace: "pre-wrap",
        }}>{msg.text}</div>

        {d && !d.raw && (
          <DiagnosisCard
            d={d}
            execResults={results}
            env={env}
            envNs={envNs}
            envColor={envColor}
            onRunCommand={onRunCommand}
            onScale={onScale}
          />
        )}
      </div>
    </div>
  );
}

// ── Diagnosis card ────────────────────────────────────────────────────────────

function DiagnosisCard({ d, execResults, env, envNs, envColor, onRunCommand, onScale }) {
  const [runState, setRunState] = useState({});
  const autoRan = execResults.filter(r => r.auto_executed);
  const autoOk  = autoRan.filter(r => r.success);
  const autoFail = autoRan.filter(r => !r.success);
  const deferred = execResults.filter(r => !r.auto_executed);
  const needsHuman = d.remediation?.needs_human || [];
  const requiresHuman = d.requires_human_intervention;

  async function handleRun(cmd, idx) {
    setRunState(s => ({ ...s, [idx]: "running" }));
    await onRunCommand(cmd);
    setRunState(s => ({ ...s, [idx]: "done" }));
  }

  return (
    <div style={{ background: "#fff", border: `1px solid ${severityBorder(d.severity)}`, borderRadius: 10, overflow: "hidden" }}>

      {/* Severity header */}
      <div style={{ padding: "8px 14px", background: severityBg(d.severity), borderBottom: `1px solid ${severityBorder(d.severity)}`, display: "flex", alignItems: "center", gap: 8 }}>
        <Badge color={severityColor(d.severity)} bg={severityBg(d.severity)}>{d.severity}</Badge>
        <span style={{ fontSize: 13, fontWeight: 600, color: severityColor(d.severity) }}>{d.summary}</span>
        {d.estimated_recovery_minutes && (
          <span style={{ marginLeft: "auto", fontSize: 11, color: "#888" }}>~{d.estimated_recovery_minutes}m to recover</span>
        )}
      </div>

      <div style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 10 }}>

        {/* Root cause */}
        {d.root_cause && (
          <div>
            <SectionLabel>Root cause</SectionLabel>
            <div style={{ fontSize: 13, color: "#333", lineHeight: 1.6 }}>{d.root_cause}</div>
          </div>
        )}

        {/* Affected */}
        {d.affected_components?.length > 0 && (
          <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
            {d.affected_components.map((c, i) => (
              <span key={i} style={{ background: "#f2f1ed", color: "#555", padding: "2px 9px", borderRadius: 10, fontSize: 11, fontWeight: 500 }}>{c}</span>
            ))}
          </div>
        )}

        {/* ── HUMAN ESCALATION BANNER ── */}
        {requiresHuman && (
          <div style={{ background: "#FCEBEB", border: "1px solid #F7C1C1", borderRadius: 8, padding: "12px 14px" }}>
            <div style={{ fontWeight: 700, fontSize: 13, color: "#791F1F", marginBottom: 6, display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ fontSize: 18 }}>🚨</span> Human intervention required
            </div>
            <div style={{ fontSize: 13, color: "#A32D2D", lineHeight: 1.6 }}>
              {d.escalation_reason || "This issue requires human judgment. The agent has stopped and will not attempt any automated fix."}
            </div>
          </div>
        )}

        {/* ── AUTO-EXECUTED ACTIONS ── */}
        {autoRan.length > 0 && (
          <div>
            <SectionLabel>Agent handled automatically</SectionLabel>
            {autoOk.map((r, i) => (
              <ActionRow key={i} icon="✓" iconColor="#3B6D11" bg="#EAF3DE" border="#C0DD97">
                <span style={{ fontWeight: 500, color: "#27500A" }}>{r.label}</span>
                <Code>{r.command}</Code>
                {r.output && <div style={{ fontSize: 11, color: "#555", marginTop: 3, fontFamily: "monospace", background: "#f5f5f2", borderRadius: 4, padding: "3px 6px" }}>{r.output.slice(0, 200)}</div>}
              </ActionRow>
            ))}
            {autoFail.map((r, i) => (
              <ActionRow key={i} icon="✗" iconColor="#A32D2D" bg="#FCEBEB" border="#F7C1C1">
                <span style={{ fontWeight: 500, color: "#791F1F" }}>{r.label} — failed</span>
                <Code>{r.command}</Code>
                {r.output && <div style={{ fontSize: 11, color: "#A32D2D", marginTop: 3 }}>{r.output.slice(0, 200)}</div>}
              </ActionRow>
            ))}
          </div>
        )}

        {/* ── DEFERRED ACTIONS (auto_execute=false by policy) ── */}
        {deferred.length > 0 && (
          <div>
            <SectionLabel>Safe actions — your approval</SectionLabel>
            {deferred.map((r, i) => (
              <CommandBlock
                key={i}
                label={r.label}
                command={r.command}
                reason={r.reason}
                state={runState[`d${i}`]}
                onRun={() => handleRun(r.command, `d${i}`)}
              />
            ))}
          </div>
        )}

        {/* ── NEEDS HUMAN (complex actions) ── */}
        {needsHuman.length > 0 && (
          <div>
            <SectionLabel>Needs your judgment</SectionLabel>
            {needsHuman.map((a, i) => (
              <CommandBlock
                key={i}
                label={a.label}
                command={a.command}
                reason={a.reason}
                state={runState[`h${i}`]}
                onRun={() => handleRun(a.command, `h${i}`)}
                warn
              />
            ))}
          </div>
        )}

        {/* ── SCALE PANEL ── */}
        {!requiresHuman && d.affected_components?.some(c => envNs?.[c]) && (
          <ScalePanel components={d.affected_components} envNs={envNs} onScale={onScale} />
        )}

        {/* Watch for */}
        {d.watch_for && (
          <div style={{ background: "#F4FAF0", border: "1px solid #C0DD97", borderRadius: 7, padding: "8px 12px", fontSize: 12.5, color: "#27500A" }}>
            <strong>Verify fix:</strong> {d.watch_for}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Reusable bits ─────────────────────────────────────────────────────────────

function SectionLabel({ children }) {
  return <div style={{ fontSize: 10, fontWeight: 600, color: "#bbb", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 5 }}>{children}</div>;
}

function Badge({ children, color, bg }) {
  return (
    <span style={{ background: color, color: "#fff", padding: "1px 8px", borderRadius: 10, fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em" }}>
      {children}
    </span>
  );
}

function Code({ children }) {
  return <div style={{ fontFamily: "monospace", fontSize: 11.5, color: "#1a1a18", marginTop: 2 }}>{children}</div>;
}

function ActionRow({ children, icon, iconColor, bg, border }) {
  return (
    <div style={{ display: "flex", gap: 8, padding: "7px 10px", background: bg, border: `1px solid ${border}`, borderRadius: 7, marginBottom: 5 }}>
      <span style={{ color: iconColor, fontWeight: 700, fontSize: 14, flexShrink: 0, marginTop: 1 }}>{icon}</span>
      <div style={{ flex: 1 }}>{children}</div>
    </div>
  );
}

function CommandBlock({ label, command, reason, state, onRun, warn }) {
  return (
    <div style={{ border: `1px solid ${warn ? "#F7C1C1" : "#e2dfd8"}`, borderRadius: 7, marginBottom: 6, overflow: "hidden" }}>
      <div style={{ padding: "6px 10px", background: warn ? "#FCEBEB" : "#f5f5f2", borderBottom: `1px solid ${warn ? "#F7C1C1" : "#e2dfd8"}`, fontSize: 12.5, color: warn ? "#791F1F" : "#444", fontWeight: 500 }}>
        {label}
      </div>
      {command && (
        <div style={{ padding: "6px 10px", fontFamily: "monospace", fontSize: 11.5, color: "#1a1a18", background: "#fafaf8" }}>
          {command}
        </div>
      )}
      {reason && (
        <div style={{ padding: "4px 10px", fontSize: 11, color: "#888", background: warn ? "#FFF7F7" : "#f5f5f2" }}>
          {reason}
        </div>
      )}
      {command && (
        <div style={{ padding: "6px 10px", background: "#f5f5f2", borderTop: `1px solid ${warn ? "#F7C1C1" : "#e2dfd8"}`, display: "flex", gap: 7 }}>
          <button onClick={onRun} disabled={state === "running"} style={{
            padding: "4px 13px", border: "1px solid #1a1a18", borderRadius: 5,
            background: state === "done" ? "#EAF3DE" : "#1a1a18",
            color: state === "done" ? "#27500A" : "#fff",
            fontSize: 11.5, cursor: state === "running" ? "wait" : "pointer", fontWeight: 500,
          }}>
            {state === "running" ? "Running…" : state === "done" ? "✓ Done" : "Run this"}
          </button>
          <button onClick={() => navigator.clipboard?.writeText(command)} style={{ padding: "4px 10px", border: "1px solid #ddd", borderRadius: 5, background: "transparent", color: "#666", fontSize: 11.5, cursor: "pointer" }}>Copy</button>
        </div>
      )}
    </div>
  );
}

function ScalePanel({ components, envNs, onScale }) {
  const scalable = components.filter(c => envNs?.[c]);
  if (!scalable.length) return null;
  return (
    <div>
      <SectionLabel>Scale replicas (your approval)</SectionLabel>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {scalable.map(dep => [1, 2, 3].map(n => (
          <button key={`${dep}-${n}`} onClick={() => onScale(dep, envNs[dep], n)} style={{
            padding: "5px 11px", border: "1px solid #ddd", borderRadius: 6, background: "#fff", fontSize: 12, cursor: "pointer", color: "#333",
          }}
            onMouseEnter={e => e.currentTarget.style.background = "#f5f5f2"}
            onMouseLeave={e => e.currentTarget.style.background = "#fff"}
          >{dep.replace("atoloan-", "")} → {n}</button>
        )))}
      </div>
    </div>
  );
}
