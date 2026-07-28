import { useState, useRef, useEffect } from "react";
import { streamChatMessage } from "../api/client";
import { useChatWidget } from "../context/ChatWidgetContext";
import ProductCard from "./ProductCard";
import ReactMarkdown from "react-markdown";
import "./ChatWidget.css";

// Small inline icons matching the reference design, one per starter prompt
function ShoeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 15c0-1.5 1-2.5 2.5-3l6-2.3c1-.4 1.7-1.3 1.8-2.4L12.5 5c.2-1.5 1.5-2 2.5-1l1 1c.6.6 1.4 1 2.3 1H20a2 2 0 0 1 2 2v3.5c0 2-1.5 3.5-3.5 3.5H4c-1.1 0-2-.9-2-2z" />
    </svg>
  );
}

function DressIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 3h6l1 3-2 2 3 12H7L10 8 8 6z" />
    </svg>
  );
}

function StoreIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 9l1.5-5h15L21 9" />
      <path d="M4 9v10a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V9" />
      <path d="M9 20v-6h6v6" />
    </svg>
  );
}

function CompareIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 3L4 7l4 4" />
      <path d="M4 7h16" />
      <path d="M16 21l4-4-4-4" />
      <path d="M20 17H4" />
    </svg>
  );
}

const STARTER_PROMPTS = [
  { text: "Shoes for standing all day", Icon: ShoeIcon },
  { text: "Find a dress under $100", Icon: DressIcon },
  { text: "Check store availability", Icon: StoreIcon },
  { text: "Compare similar products", Icon: CompareIcon },
];

// Maps real backend progress event values to human-readable labels.
// Only includes steps the backend genuinely emits — nothing invented.
const PROGRESS_LABELS = {
  understanding_request: "Understanding request",
  searching_products: "Searching products",
  checking_inventory: "Checking inventory",
  verifying_claims: "Verifying claims",
  preparing_response: "Preparing response",
};

export default function ChatWidget() {
  const { isOpen, setIsOpen, prefillMessage, prefillNonce } = useChatWidget();
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [progressSteps, setProgressSteps] = useState([]);
  const [progressCollapsed, setProgressCollapsed] = useState(false);
  const [connectionError, setConnectionError] = useState(false);
  const [lastFailedMessage, setLastFailedMessage] = useState(null);
  const messagesEndRef = useRef(null);
  const panelRef = useRef(null);

  useEffect(() => {
    if (isOpen) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isOpen, progressSteps]);

  useEffect(() => {
    if (prefillMessage) {
      setInput(prefillMessage);
    }
  }, [prefillNonce]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!isOpen) return;
    function handleKeyDown(e) {
      if (e.key === "Escape") {
        setIsOpen(false);
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, setIsOpen]);

  async function runSend(messageText) {
    setIsLoading(true);
    setProgressSteps([]);
    setProgressCollapsed(false);
    setConnectionError(false);
    setLastFailedMessage(null);

    let gotAnyEvent = false;

    try {
      await streamChatMessage(messageText, sessionId, {
        onSession: (id) => {
          gotAnyEvent = true;
          setSessionId(id);
        },
        onProgress: (label) => {
          gotAnyEvent = true;
          setProgressSteps((prev) => [...prev, label]);
        },
        onDone: (reply, products) => {
          gotAnyEvent = true;
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: reply, products: products || [] },
          ]);
          setProgressSteps([]);
        },
        onError: (err) => {
          gotAnyEvent = true;
          setMessages((prev) => [
            ...prev,
            { role: "error", content: "Something went wrong reaching Scout. Try again." },
          ]);
          setLastFailedMessage(messageText);
        },
      });

      if (!gotAnyEvent) {
        setConnectionError(true);
        setLastFailedMessage(messageText);
      }
    } catch (err) {
      console.error("ChatWidget stream error:", err);
      setConnectionError(true);
      setLastFailedMessage(messageText);
    } finally {
      setIsLoading(false);
      setProgressSteps([]);
    }
  }

  async function handleSend(overrideText) {
    const trimmed = (overrideText ?? input).trim();
    if (!trimmed || isLoading) return;

    setMessages((prev) => [...prev, { role: "user", content: trimmed }]);
    setInput("");
    await runSend(trimmed);
  }

  function handleRetry() {
    if (!lastFailedMessage || isLoading) return;
    runSend(lastFailedMessage);
  }

  function handleStarterClick(prompt) {
    handleSend(prompt);
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
        aria-label="Open shopping assistant"
      >
        💬
      </button>
    );
  }

  return (
    <div className="chat-widget-panel" ref={panelRef} role="dialog" aria-label="Scout shopping assistant">
      <header className="chat-widget-header">
        <div className="chat-widget-header-titles">
          <span className="chat-widget-title">Scout Shopping Assistant</span>
          <span className="chat-widget-status">
            <span className="chat-widget-status-dot" />
            Online
          </span>
        </div>
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
            <p className="chat-widget-empty-title">👋 Hi! I'm Scout.</p>
            <p className="chat-widget-empty-subtitle">How can I help you today?</p>

            <div className="chat-widget-starters">
              {STARTER_PROMPTS.map(({ text, Icon }) => (
                <button
                  key={text}
                  className="chat-widget-starter-btn"
                  onClick={() => handleStarterClick(text)}
                  disabled={isLoading}
                >
                  <Icon />
                  <span>{text}</span>
                </button>
              ))}
            </div>

            <p className="chat-widget-supporting-message">
              <span className="chat-widget-supporting-icon">✦</span>
              Ask about products, inventory, comparisons, or your order.
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

            {msg.role === "error" && lastFailedMessage && i === messages.length - 1 && (
              <button className="chat-widget-retry-btn" onClick={handleRetry} disabled={isLoading}>
                Retry
              </button>
            )}

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

        {isLoading && progressSteps.length > 0 && (
          <div className="chat-widget-progress">
            <button
              className="chat-widget-progress-toggle"
              onClick={() => setProgressCollapsed((c) => !c)}
              aria-expanded={!progressCollapsed}
            >
              <span className="chat-widget-progress-current">
                {PROGRESS_LABELS[progressSteps[progressSteps.length - 1]] || "Working…"}
              </span>
              <span className="chat-widget-progress-caret">{progressCollapsed ? "▸" : "▾"}</span>
            </button>
            {!progressCollapsed && (
              <ul className="chat-widget-progress-list">
                {progressSteps.map((step, idx) => (
                  <li key={idx} className="chat-widget-progress-item">
                    {PROGRESS_LABELS[step] || step}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {isLoading && progressSteps.length === 0 && (
          <div className="chat-widget-bubble-msg chat-widget-bubble-msg--assistant chat-widget-loading">
            <span className="chat-widget-dot" />
            <span className="chat-widget-dot" />
            <span className="chat-widget-dot" />
          </div>
        )}

        {connectionError && (
          <div className="chat-widget-disconnected">
            <p>Can't reach Scout right now. Check your connection and try again.</p>
            <button className="chat-widget-retry-btn" onClick={handleRetry}>
              Retry
            </button>
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
          placeholder="Type your question..."
          rows={1}
        />
        <button
          className="chat-widget-send"
          onClick={() => handleSend()}
          disabled={isLoading || !input.trim()}
        >
          Send
        </button>
      </div>
    </div>
  );
}
