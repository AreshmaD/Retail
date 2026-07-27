import { useState, useRef, useEffect } from "react";
import { sendChatMessage } from "../api/client";
import { useChatWidget } from "../context/ChatWidgetContext";
import ProductCard from "./ProductCard";
import ReactMarkdown from "react-markdown";
import "./ChatWidget.css";

export default function ChatWidget() {
  const { isOpen, setIsOpen, prefillMessage, prefillNonce } = useChatWidget();
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef(null);

  useEffect(() => {
    if (isOpen) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isOpen]);

  // When something outside the widget (e.g. the search page) asks us to
  // open with a prefilled message, drop it straight into the input box —
  // the customer can review/edit before sending, not have it auto-sent.
  useEffect(() => {
    if (prefillMessage) {
      setInput(prefillMessage);
    }
  }, [prefillNonce]); // eslint-disable-line react-hooks/exhaustive-deps

  async function handleSend() {
    const trimmed = input.trim();
    if (!trimmed || isLoading) return;

    setMessages((prev) => [...prev, { role: "user", content: trimmed }]);
    setInput("");
    setIsLoading(true);

    try {
      const data = await sendChatMessage(trimmed, sessionId);
      setSessionId(data.session_id);
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: data.reply, products: data.products || [] },
      ]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: "error", content: "Something went wrong reaching Scout. Try again." },
      ]);
    } finally {
      setIsLoading(false);
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  if (!isOpen) {
    return (
      <button
        className="chat-widget-bubble"
        onClick={() => setIsOpen(true)}
        aria-label="Open Scout assistant"
      >
        💬
      </button>
    );
  }

  return (
    <div className="chat-widget-panel">
      <header className="chat-widget-header">
        <span className="chat-widget-title">Scout</span>
        <button
          className="chat-widget-close"
          onClick={() => setIsOpen(false)}
          aria-label="Close"
        >
          ✕
        </button>
      </header>

      <div className="chat-widget-messages">
        {messages.length === 0 && (
          <div className="chat-widget-empty">
            <p className="chat-widget-empty-title">
              Hi, I'm Scout — your retail AI assistant.
            </p>
            <p className="chat-widget-empty-subtitle">
              Ask me anything in natural language — recommend a product,
              check stock or an order, ask about our return policy — and
              I'll find you a real answer.
            </p>
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i}>
            <div className={`chat-widget-bubble-msg chat-widget-bubble-msg--${msg.role}`}>
              {msg.role === "assistant" ? (
                <ReactMarkdown
                  components={{
                    a: (props) => <a {...props} target="_blank" rel="noopener noreferrer" />,
                    p: (props) => <p style={{ margin: 0 }} {...props} />,
                  }}
                >
                  {msg.content}
                </ReactMarkdown>
              ) : (
                msg.content
              )}
            </div>

            {msg.products && msg.products.length > 0 && (
              <div className="chat-widget-products">
                {msg.products.map((p) => (
                  <ProductCard
                    key={p.product_id || p.external_product_id}
                    product={p}
                  />
                ))}
              </div>
            )}
          </div>
        ))}

        {isLoading && (
          <div className="chat-widget-bubble-msg chat-widget-bubble-msg--assistant chat-widget-loading">
            <span className="chat-widget-dot" />
            <span className="chat-widget-dot" />
            <span className="chat-widget-dot" />
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      <div className="chat-widget-input-row">
        <textarea
          className="chat-widget-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Message Scout…"
          rows={1}
        />
        <button
          className="chat-widget-send"
          onClick={handleSend}
          disabled={isLoading || !input.trim()}
        >
          Send
        </button>
      </div>
    </div>
  );
}
