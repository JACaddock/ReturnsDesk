// Mirrors returns_desk/agent.py's Step and ToolCall, as /chat/stream sends them.
export type ToolCall = { name: string; input: unknown; result: string; is_error: boolean; latency_ms: number };
export type Step = {
  turn: number;
  step: number;
  stop_reason: string;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  text: string;
  tool_calls: ToolCall[];
};
export type ChatResponse = { conversation_id: string; reply: string; stopped_because: string; steps: Step[] };

/** POSTs a message to /chat/stream; onStep gets each tool step as it happens. Resolves with the reply. */
export async function sendMessage(
  message: string,
  conversationId: string | null,
  onStep: (step: Step) => void,
): Promise<ChatResponse> {
  const res = await fetch("/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, conversation_id: conversationId }),
  });
  if (res.status === 404) throw new Error("This conversation has expired. Start a new one.");
  if (!res.ok) throw new Error(`Request failed (${res.status}).`);

  // EventSource only does GET, so read the server-sent events off the response body by hand.
  const reader = res.body!.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) throw new Error("The connection closed before the reply arrived.");
    buffer += value;
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const event = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const kind = event.match(/^event: (.*)$/m)?.[1];
      const data = JSON.parse(event.match(/^data: (.*)$/m)?.[1] ?? "null");
      if (kind === "step") onStep(data);
      else if (kind === "done") return data;
      else throw new Error(data?.detail ?? "Something went wrong.");
    }
  }
}
