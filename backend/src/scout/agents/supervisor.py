import asyncio
import json

from langgraph_supervisor import create_supervisor

from scout.agents.model_provider import get_chat_model
from scout.agents.tools_loader import MCPToolManager
from scout.agents.specialists import build_specialists
from scout.agents.intent_splitter import split_intents, merge_answers
from scout.agents.tool_guard import reset_guard

MAX_HISTORY_MESSAGES = 12  # only the most recent N messages get sent to
# the model per call — full history is still stored/returned to the
# caller, this only trims what's actually transmitted as input tokens.
# 12 is enough for the multi-turn memory tests (2-3 turn conversations,
# ~2 messages per turn) while capping cost on long sessions.


def _trim_history_for_model(history: list[dict]) -> list[dict]:
    if len(history) <= MAX_HISTORY_MESSAGES:
        return history
    return history[-MAX_HISTORY_MESSAGES:]
from scout.agents.verification import verify_price_grounding

SPECIALIST_NAMES = {
    "recommend_agent",
    "inventory_agent",
    "order_agent",
    "external_offer_agent",
    "policy_agent",
}
PRODUCT_TOOL_NAMES = {"search", "alternatives", "search_external_offers", "recommend_products"}
HANDOFF_MARKERS = {"Transferring back to supervisor"}

SUPERVISOR_PROMPT = """You are Scout, the supervisor for a retail shopping
assistant. You NEVER answer customer questions yourself and you NEVER call
tools directly. Your only job is to hand off each customer message to
exactly one specialist:

- recommend_agent: open-ended product recommendations, semantic search,
  ranking, promotions
- inventory_agent: stock checks, store pickup, nearby store availability
- order_agent: order status, tracking, order history, and RETURN
  ELIGIBILITY FOR A SPECIFIC ORDER (read-only — cannot create, modify, or
  pay for orders). If the customer mentions a specific order ID (like
  "O1001") alongside a question about returning/refunding it, that is
  order_agent's job, NOT policy_agent's — even though the word "return"
  appears. policy_agent is only for GENERAL policy questions with no
  specific order mentioned (e.g. "what's your return policy?").
- external_offer_agent: ONLY when the customer wants a product that isn't
  in our own inventory and they want a third-party alternative — hand off
  here specifically for that case, not for general recommendations
- policy_agent: returns, refunds, exchanges, and shipping policy questions

Every single customer message — including short follow-ups — must be handed
off to a specialist EXACTLY ONCE. Do not say things like "you're all set"
or end the conversation yourself. If you are unsure which specialist fits,
pick the closest match rather than answering yourself.

CRITICAL: Once ANY specialist has responded to the customer's message —
even if that response offers the customer choices, asks what they'd like
to do next, or mentions another category — that response is COMPLETE and
FINAL for this turn. Do NOT hand off to a second specialist for the same
customer message under any circumstances. A response offering options is
not an incomplete response.

MANDATORY RULE, NO EXCEPTIONS: if recommend_agent's response starts with
the exact text "NEEDS_EXTERNAL_CHECK", you MUST call
transfer_to_external_offer_agent as your ONLY action. Do NOT write any
reply text of your own. Do NOT summarize what recommend_agent said. Do
NOT explain that nothing was found. Your ENTIRE response in this case
must be that ONE tool call and nothing else — treat "NEEDS_EXTERNAL_CHECK"
exactly like a customer message that must be routed, using the text after
the colon as what to hand off. After external_offer_agent responds, that
IS final — do not hand off a third time.

ONE MORE NARROW EXCEPTION: if the customer's message is CLEARLY just
ending the conversation — a simple thank-you, goodbye, "that's all",
"no that's it", or similar, with NO actual question or request in it —
respond directly yourself with a brief, warm, genuine closing (e.g.
"You're welcome — have a great day!" or "Glad I could help. Come back
anytime!"). Do NOT hand off to a specialist for a pure closing message.
This exception applies ONLY when there is truly no question or request
left to answer — if the message contains BOTH a closing AND a real
question, hand off to the right specialist as usual.
"""


def extract_text(content) -> str:
    """Normalizes message content to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


def get_final_reply(messages: list) -> str:
    """Returns the last substantive specialist response — skips handoff
    tool-call messages, the supervisor's own trailing commentary, AND the
    NEEDS_EXTERNAL_CHECK signal itself (a safety net: if the supervisor
    somehow fails to act on that signal and route onward, we never want
    the raw internal signal text shown to the customer).

    ONE exception: if NO specialist responded at all this turn (e.g. a
    pure "thanks, that's all!" closing message, which the supervisor is
    now allowed to answer directly rather than route), fall back to the
    supervisor's own final message instead of the generic fallback text.
    This only triggers when zero specialists were involved — if any
    specialist DID respond, their message still takes priority and the
    supervisor's trailing text is still ignored, exactly as before."""
    any_specialist_responded = any(
        getattr(msg, "name", None) in SPECIALIST_NAMES for msg in messages
    )

    for msg in reversed(messages):
        name = getattr(msg, "name", None)
        if name not in SPECIALIST_NAMES:
            continue
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            continue
        text = extract_text(getattr(msg, "content", ""))
        stripped = text.strip()
        if not stripped or stripped in HANDOFF_MARKERS:
            continue
        if stripped.startswith("NEEDS_EXTERNAL_CHECK"):
            continue
        return text

    if not any_specialist_responded:
        for msg in reversed(messages):
            if getattr(msg, "name", None) == "supervisor":
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    continue
                text = extract_text(getattr(msg, "content", ""))
                stripped = text.strip()
                if stripped and stripped not in HANDOFF_MARKERS:
                    return text

    return "I couldn't quite find what you're looking for — want to try asking a different way, or is there something else I can help with?"


def _extract_product_dicts(value, results: list[dict]) -> None:
    """Recursively flattens ANY nesting/encoding shape a tool result might
    arrive in — different providers structure MCP tool content differently
    (Claude sends a JSON-encoded STRING containing a nested list of
    {"type": "text", "text": "<json>"} blocks; Groq/Ollama/Gemini send an
    actual Python list of similar blocks; either can nest one or more
    levels deep). Rather than handle each shape as a separate branch, this
    walks the structure generically: strings get JSON-decoded and
    recursed into, lists get each item recursed into, and a dict is
    either a wrapper (has "text" -> decode and recurse into that) or an
    actual product (has "price"/"external_product_id" -> keep it)."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return
        _extract_product_dicts(parsed, results)
        return

    if isinstance(value, list):
        for item in value:
            _extract_product_dicts(item, results)
        return

    if isinstance(value, dict):
        if "price" in value or "external_product_id" in value:
            results.append(value)
            return
        if "text" in value:
            _extract_product_dicts(value["text"], results)
            return
        # unrecognized dict shape — skip silently rather than error


def _parse_json_blocks(content) -> list[dict]:
    """Entry point: flattens a tool message's content (any shape) into a
    plain list of product dicts."""
    results: list[dict] = []
    _extract_product_dicts(content, results)
    return results


def extract_products(messages: list) -> list[dict]:
    """Scans the message trace for actual tool results from product-bearing
    tools and returns them as structured data, so the frontend can render
    real product cards instead of parsing prose. Only pulls from genuine
    tool results — never invents or infers products."""
    products: list[dict] = []
    for msg in messages:
        msg_type = getattr(msg, "type", None)
        name = getattr(msg, "name", None)
        if msg_type != "tool" or name not in PRODUCT_TOOL_NAMES:
            continue

        content = getattr(msg, "content", "")
        for item in _parse_json_blocks(content):
            if isinstance(item, dict) and ("price" in item or "external_product_id" in item):
                item.setdefault("source", "internal")
                products.append(item)

    seen = {}
    for p in products:
        key = p.get("product_id") or p.get("external_product_id")
        if key:
            seen[key] = p
    return list(seen.values())


async def build_supervisor_app():
    tool_manager = MCPToolManager()
    tools = await tool_manager.start()
    specialists = build_specialists(tools)

    model = get_chat_model()

    workflow = create_supervisor(
        [
            specialists["recommend_agent"],
            specialists["inventory_agent"],
            specialists["order_agent"],
            specialists["external_offer_agent"],
            specialists["policy_agent"],
        ],
        model=model,
        prompt=SUPERVISOR_PROMPT,
        output_mode="full_history",
    )

    return workflow.compile(), tool_manager


async def _run_single_intent(app, history: list[dict], sub_intent: str, debug: bool):
    """Runs one sub-intent through the existing supervisor/specialist
    graph. Appends both the sub-intent and its answer to history so later
    sub-intents (and future turns) have full context.

    Phase 1 verification gate: after getting a reply, checks it against a
    deterministic price-grounding rule (every $ amount mentioned must
    match a real price from this turn's tool results). If it fails,
    ALLOWS EXACTLY ONE correction attempt. If it still fails after that
    one correction, returns a safe, honest fallback instead of ever
    showing the customer an unverified claim."""
    if not sub_intent or not sub_intent.strip():
        return "I didn't quite catch that — could you rephrase your question?", []

    reset_guard(max_total_calls=10, max_identical_calls=1)

    # Track how many messages existed BEFORE this turn, so we only ever
    # scan messages genuinely produced by THIS turn for products/replies —
    # never the full accumulated session history. Without this, product
    # recommendations from earlier questions in the same session (e.g.
    # dresses asked about earlier) would keep reappearing alongside
    # unrelated later answers (e.g. shoes).
    messages_before = len(history)
    history.append({"role": "user", "content": sub_intent})
    result = await app.ainvoke({"messages": _trim_history_for_model(history)}, config={"recursion_limit": 15})
    new_messages = result["messages"][messages_before:]

    if debug:
        print(f"\n--- FULL MESSAGE TRACE (sub-intent: {sub_intent!r}) ---")
        for i, msg in enumerate(result["messages"]):
            role = getattr(msg, "type", "?")
            name = getattr(msg, "name", None)
            tool_calls = getattr(msg, "tool_calls", None)
            text = extract_text(getattr(msg, "content", ""))
            print(f"[{i}] role={role} name={name}")
            if tool_calls:
                print(f"    tool_calls={tool_calls}")
            if text:
                print(f"    content={text[:300]!r}")
        print("--- END TRACE ---\n")

    reply_text = get_final_reply(new_messages)
    all_products = extract_products(new_messages)

    # If a NEEDS_EXTERNAL_CHECK hand-off happened this turn, the internal
    # products from recommend_agent's abandoned attempt are stale — the
    # final reply is about the external results only, so only THOSE
    # should be shown. Without this, both the discarded internal
    # near-misses and the real external results get mixed into one array.
    had_external_handoff = any(
        "NEEDS_EXTERNAL_CHECK" in extract_text(getattr(msg, "content", ""))
        for msg in new_messages
        if getattr(msg, "name", None) == "recommend_agent"
    )
    if had_external_handoff:
        products = [p for p in all_products if p.get("source") == "external"]
    else:
        products = all_products

    passed, reason = verify_price_grounding(reply_text, products, customer_message=sub_intent)

    if not passed:
        if debug:
            print(f"\n[Verification] FAILED: {reason}")
            print("[Verification] Attempting ONE correction...\n")

        correction_note = (
            f"Your previous answer had a grounding problem: {reason}. "
            "Provide a corrected answer using ONLY the real prices and data "
            "from the tool results already available in this conversation — "
            "do not invent or restate an incorrect price."
        )
        reset_guard(max_total_calls=10, max_identical_calls=1)
        messages_before_correction = len(history)
        history.append({"role": "user", "content": correction_note})
        correction_result = await app.ainvoke(
            {"messages": _trim_history_for_model(history)}, config={"recursion_limit": 15}
        )
        new_correction_messages = correction_result["messages"][messages_before_correction:]

        corrected_reply = get_final_reply(new_correction_messages)
        corrected_products = extract_products(new_correction_messages) or products
        passed_now, reason_now = verify_price_grounding(corrected_reply, corrected_products, customer_message=sub_intent)

        if debug:
            status = "PASSED" if passed_now else "STILL FAILED"
            print(f"[Verification] Correction attempt {status}: {reason_now}\n")

        if passed_now:
            reply_text = corrected_reply
            products = corrected_products
        else:
            reply_text = (
                "I'm having trouble confirming accurate pricing for this "
                "request right now. Could you rephrase, or I can help you "
                "look up a specific product directly?"
            )
            products = []

    history.append({"role": "assistant", "content": reply_text})
    return reply_text, products


async def ask(app, history: list[dict], message: str, debug: bool = False):
    """Splits the customer message into sub-intents (usually just one),
    runs each through the existing supervisor/specialist graph separately,
    then merges the answers into one natural reply. Products from ALL
    sub-intents are combined and returned. Returns
    (reply_text, updated_history, products)."""
    if not message or not message.strip():
        return "Please type a message.", history, []

    splitter_model = get_chat_model()
    sub_intents = split_intents(splitter_model, message)

    if debug and len(sub_intents) > 1:
        print(f"\n[Intent splitter] Detected {len(sub_intents)} sub-intents: {sub_intents}\n")

    all_products: list[dict] = []
    sub_answers: list[str] = []

    for sub_intent in sub_intents:
        reply_text, products = await _run_single_intent(app, history, sub_intent, debug)
        all_products.extend(products)
        sub_answers.append(reply_text)

    final_reply = merge_answers(splitter_model, message, sub_answers)

    return final_reply, history, all_products


if __name__ == "__main__":
    async def _main():
        app, tool_manager = await build_supervisor_app()
        history: list[dict] = []
        print("Scout is ready. Type a message (Ctrl+C to quit).\n")
        while True:
            try:
                user_input = input("You: ")
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input.strip():
                continue
            reply, history, products = await ask(app, history, user_input, debug=True)
            print(f"Scout: {reply}\n")
            if products:
                print(f"[products returned: {len(products)}]")
                for p in products:
                    print("  -", p.get("name"), p.get("price"), p.get("source", "internal"))
                print()
        await tool_manager.stop()

    asyncio.run(_main())


async def _run_single_intent_streaming(app, history: list[dict], sub_intent: str, debug: bool = False):
    """Streaming-aware sibling of _run_single_intent, used only by the
    /chat/stream endpoint. Yields ("progress", label) tuples as real
    LangGraph node transitions occur, then yields exactly one
    ("result", (reply_text, products)) tuple at the end — after running
    the SAME verification + one-correction-retry gate _run_single_intent
    uses. No partial/unverified text is ever yielded; only real progress
    signals and the final, checked answer.
    """
    if not sub_intent or not sub_intent.strip():
        yield ("result", ("I didn't quite catch that — could you rephrase your question?", []))
        return

    reset_guard(max_total_calls=10, max_identical_calls=1)

    # Track how many messages existed BEFORE this turn so we only ever
    # scan NEW messages for products/replies — never the full session
    # history. Without this, recommendations from earlier questions in
    # the same session keep reappearing alongside later, unrelated answers.
    messages_before = len(history)
    history.append({"role": "user", "content": sub_intent})

    yield ("progress", "understanding_request")

    final_result = None
    seen_nodes: set[str] = set()

    async for event in app.astream_events(
        {"messages": history}, version="v2", config={"recursion_limit": 15}
    ):
        node_name = event.get("metadata", {}).get("langgraph_node", "")
        ev_type = event.get("event")

        if ev_type == "on_chain_start" and node_name and node_name not in seen_nodes:
            seen_nodes.add(node_name)
            if node_name in ("recommend_agent", "external_offer_agent"):
                yield ("progress", "searching_products")
            elif node_name == "inventory_agent":
                yield ("progress", "checking_inventory")

        if ev_type == "on_chain_end" and event.get("name") == "LangGraph":
            final_result = event.get("data", {}).get("output")

    if final_result is None:
        yield ("result", ("Something went wrong processing that — could you try again?", []))
        return

    new_messages = final_result["messages"][messages_before:]
    reply_text = get_final_reply(new_messages)
    all_products = extract_products(new_messages)

    had_external_handoff = any(
        "NEEDS_EXTERNAL_CHECK" in extract_text(getattr(msg, "content", ""))
        for msg in new_messages
        if getattr(msg, "name", None) == "recommend_agent"
    )
    products = (
        [p for p in all_products if p.get("source") == "external"]
        if had_external_handoff
        else all_products
    )

    yield ("progress", "verifying_claims")
    passed, reason = verify_price_grounding(reply_text, products, customer_message=sub_intent)

    if not passed:
        correction_note = (
            f"Your previous answer had a grounding problem: {reason}. "
            "Provide a corrected answer using ONLY the real prices and data "
            "from the tool results already available in this conversation — "
            "do not invent or restate an incorrect price."
        )
        reset_guard(max_total_calls=10, max_identical_calls=1)
        messages_before_correction = len(history)
        history.append({"role": "user", "content": correction_note})
        correction_result = await app.ainvoke(
            {"messages": _trim_history_for_model(history)}, config={"recursion_limit": 15}
        )
        new_correction_messages = correction_result["messages"][messages_before_correction:]
        corrected_reply = get_final_reply(new_correction_messages)
        corrected_products = extract_products(new_correction_messages) or products
        passed_now, _ = verify_price_grounding(
            corrected_reply, corrected_products, customer_message=sub_intent
        )
        if passed_now:
            reply_text = corrected_reply
            products = corrected_products
        else:
            reply_text = (
                "I'm having trouble confirming accurate pricing for this "
                "request right now. Could you rephrase, or I can help you "
                "look up a specific product directly?"
            )
            products = []

    yield ("progress", "preparing_response")

    history.append({"role": "assistant", "content": reply_text})
    yield ("result", (reply_text, products))


async def ask_streaming(app, history: list[dict], message: str, debug: bool = False):
    """Streaming-aware sibling of ask(). Yields ("progress", label) tuples
    as real steps occur across all sub-intents, then a final
    ("result", (final_reply, all_products)) tuple once everything has been
    processed and merged — same verification-gated pipeline as ask(),
    with progress visibility for the frontend.
    """
    if not message or not message.strip():
        yield ("result", ("Please type a message.", []))
        return

    splitter_model = get_chat_model()
    sub_intents = split_intents(splitter_model, message)

    all_products: list[dict] = []
    sub_answers: list[str] = []

    for sub_intent in sub_intents:
        async for kind, payload in _run_single_intent_streaming(app, history, sub_intent, debug):
            if kind == "progress":
                yield ("progress", payload)
            else:
                reply_text, products = payload
                all_products.extend(products)
                sub_answers.append(reply_text)

    final_reply = merge_answers(splitter_model, message, sub_answers)
    yield ("result", (final_reply, all_products))
