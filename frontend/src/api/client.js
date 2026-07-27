const BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

export async function sendChatMessage(message, sessionId = null) {
  const response = await fetch(`${BASE_URL}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId }),
  });

  if (!response.ok) {
    throw new Error(`Chat request failed: ${response.status}`);
  }

  return response.json();
}

export async function streamChatMessage(message, sessionId, callbacks = {}) {
  const { onSession, onToken, onDone, onError } = callbacks;

  const response = await fetch(`${BASE_URL}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId }),
  });

  if (!response.ok || !response.body) {
    onError?.(new Error(`Stream request failed: ${response.status}`));
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    const events = buffer.split("\n\n");
    buffer = events.pop();

    for (const rawEvent of events) {
      const eventMatch = rawEvent.match(/^event:\s*(.+)$/m);
      const dataMatch = rawEvent.match(/^data:\s*(.*)$/m);
      const eventType = eventMatch?.[1]?.trim();
      const data = dataMatch?.[1] ?? "";

      if (eventType === "session") {
        onSession?.(data);
      } else if (eventType === "token") {
        onToken?.(data);
      } else if (eventType === "error") {
        onError?.(new Error(data));
      } else if (eventType === "done") {
        try {
          const parsed = JSON.parse(data);
          onDone?.(parsed.reply);
        } catch {
          onDone?.(data);
        }
      }
    }
  }
}
