import { useState, type FormEvent } from "react";
import { sendMessage, type Step } from "./api";

type Message = { role: "customer" | "agent" | "error"; text: string };

export default function App() {
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [steps, setSteps] = useState<Step[]>([]);
  // Steps from the turn in progress; replaced by the turn's full list (final reply step included) when it ends.
  const [liveSteps, setLiveSteps] = useState<Step[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [handedOff, setHandedOff] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    setDraft("");
    setBusy(true);
    setMessages((m) => [...m, { role: "customer", text }]);
    try {
      const res = await sendMessage(text, conversationId, (step) => setLiveSteps((s) => [...s, step]));
      setConversationId(res.conversation_id);
      setMessages((m) => [...m, { role: "agent", text: res.reply }]);
      setSteps((s) => [...s, ...res.steps]);
      if (res.stopped_because === "escalated" || res.stopped_because === "max_steps") setHandedOff(true);
    } catch (err) {
      setMessages((m) => [...m, { role: "error", text: (err as Error).message }]);
    } finally {
      setLiveSteps([]);
      setBusy(false);
    }
  }

  function reset() {
    setConversationId(null);
    setMessages([]);
    setSteps([]);
    setHandedOff(false);
  }

  const shown = [...steps, ...liveSteps];
  const tokens = shown.reduce((n, s) => n + s.input_tokens + s.output_tokens, 0);

  return (
    <div className="app">
      <main className="chat">
        <header>
          <h1>Returns Desk</h1>
          <button type="button" onClick={reset} disabled={busy}>New conversation</button>
        </header>
        <div className="messages" aria-live="polite">
          {messages.length === 0 && (
            <p className="hint">
              Ask about an order, a return or a refund. Try “Where is order A1043?”, then give the email on that order
              when asked.
            </p>
          )}
          {messages.map((m, i) => (
            <p key={i} className={`message ${m.role}`}>{m.text}</p>
          ))}
          {busy && <p className="message agent working">Working…</p>}
          {handedOff && <p className="notice">This conversation is now with a member of the team.</p>}
        </div>
        <form onSubmit={submit}>
          <label htmlFor="message" className="visually-hidden">Message</label>
          <textarea
            id="message"
            value={draft}
            maxLength={2000}
            rows={2}
            placeholder="Type a message"
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) submit(e);
            }}
          />
          <button type="submit" disabled={busy || !draft.trim()}>Send</button>
        </form>
      </main>

      <aside className="panel" aria-label="Agent steps">
        <h2>Tool calls</h2>
        <p className="meta">
          {conversationId ? `Conversation ${conversationId}` : "No conversation yet"} · {tokens.toLocaleString()} tokens
        </p>
        {shown.length === 0 && <p className="hint">Each tool the agent calls shows up here as it runs.</p>}
        <ol>
          {shown.map((s) => (
            <li key={`${s.turn}-${s.step}`}>
              <div className="step-head">
                Turn {s.turn}, step {s.step} · {s.latency_ms.toLocaleString()} ms ·{" "}
                {(s.input_tokens + s.output_tokens).toLocaleString()} tokens
              </div>
              {s.tool_calls.length === 0 && <div className="reply-step">Replied</div>}
              {s.tool_calls.map((c, i) => (
                <details key={i} className={c.is_error ? "call error" : "call"}>
                  <summary>
                    <code>{c.name}</code>
                    {c.is_error && <span className="badge">refused</span>}
                    <span className="latency">{c.latency_ms} ms</span>
                  </summary>
                  <pre>{JSON.stringify(c.input, null, 2)}</pre>
                  <pre>{c.result}</pre>
                </details>
              ))}
            </li>
          ))}
        </ol>
      </aside>
    </div>
  );
}
