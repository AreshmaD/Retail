from __future__ import annotations

from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool as tool_decorator
from langgraph.prebuilt import InjectedState, create_react_agent
from langgraph.types import Command
from langgraph_supervisor import create_supervisor

from scout.agents.model_provider import get_chat_model
from scout.agents.specialists import build_specialists
from scout.agents.tools_loader import MCPToolManager


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

WRITING THE HANDOFF TASK DESCRIPTION: every hand-off tool requires a
task_description argument. This description is the ONLY thing the
specialist will see — it does NOT receive the raw conversation history,
so it cannot infer anything you leave out. Write a complete, self-
contained instruction that includes every relevant detail the customer
has stated so far in this conversation:
- the exact product/category the customer is asking about
- any size, color, or budget the customer mentioned, even in an earlier
  turn of this same conversation
- any specific store or order ID mentioned
- what KIND of answer is needed (a recommendation, a stock check, a
  policy explanation, etc.)
Write it as a clear, complete instruction, e.g. "Check stock for the
Black Midi Dress (product P001) in size M, black, and let the customer
know if it's available." A vague task_description like "check
availability" gives the specialist nothing to work with and will
produce a worse answer than if you had included the details you already
know.
"""



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

    app = workflow.compile()
    try:
        app.scout_specialists = specialists
    except Exception:
        pass
    return app, tool_manager


# ─────────────────────────────────────────────────────────
# PHASE 3 MIGRATION (in progress) — manual tool-based supervisor
# New, parallel implementation alongside build_supervisor_app() above.
# NOT yet wired into ask()/ask_streaming() — exists so it can be
# tested in complete isolation before any switchover. See
# docs/phase3-migration-plan.md for full design and rationale.
# ─────────────────────────────────────────────────────────

async def build_supervisor_app_v2():
    """Step 1 of the migration: trivial pass-through handoff tools,
    no task-description logic yet. Goal at this step is ONLY to
    confirm create_react_agent + manual handoff tools work at all in
    this project's setup, before adding the real fix.
    """
    tool_manager = MCPToolManager()
    tools = await tool_manager.start()
    specialists = build_specialists(tools)

    model = get_chat_model()

    handoff_tools = [
        _make_task_description_handoff_tool(name)
        for name in (
            "recommend_agent",
            "inventory_agent",
            "order_agent",
            "external_offer_agent",
            "policy_agent",
        )
    ]

    supervisor = create_react_agent(
        model=model,
        tools=handoff_tools,
        prompt=SUPERVISOR_PROMPT,
        name="supervisor",
    )

    app = supervisor
    try:
        app.scout_specialists = specialists
    except Exception:
        pass
    return app, tool_manager


async def build_supervisor_app_v3():
    """STEP 2, ATTEMPT 2 — v2 revealed that create_react_agent alone
    builds the supervisor as a STANDALONE graph, with no actual parent
    graph containing both supervisor and specialists as nodes for
    Command(goto=...) to navigate within. Fixed here by explicitly
    building a real StateGraph with the supervisor AND all 5
    specialists added as nodes — matching the actual official
    multi-agent pattern, not a partial version of it.
    """
    from langgraph.graph import StateGraph, START, MessagesState

    tool_manager = MCPToolManager()
    tools = await tool_manager.start()
    specialists = build_specialists(tools)
    model = get_chat_model()

    handoff_tools = [
        _make_task_description_handoff_tool(name)
        for name in (
            "recommend_agent",
            "inventory_agent",
            "order_agent",
            "external_offer_agent",
            "policy_agent",
        )
    ]

    supervisor_agent = create_react_agent(
        model=model,
        tools=handoff_tools,
        prompt=SUPERVISOR_PROMPT,
        name="supervisor",
    )

    builder = StateGraph(MessagesState)
    builder.add_node("supervisor", supervisor_agent)
    for name, specialist_agent in specialists.items():
        builder.add_node(name, specialist_agent)
    builder.add_edge(START, "supervisor")
    for name in specialists:
        builder.add_edge("supervisor", name)
        builder.add_edge(name, "supervisor")

    app = builder.compile()
    try:
        app.scout_specialists = specialists
    except Exception:
        pass
    return app, tool_manager


def _make_task_description_handoff_tool(agent_name: str):
    """STEP 2 — the real fix. The supervisor LLM must provide a
    task_description argument when calling this tool; that description
    becomes the ENTIRE message history the target specialist sees,
    REPLACING (not filtering) whatever came before. This is the direct
    fix for cross-agent context bleed: the specialist never receives
    another specialist's raw tool-call traces, because no prior history
    is forwarded at all — only this one fresh, supervisor-written
    instruction. See SUPERVISOR_PROMPT's "WRITING THE HANDOFF TASK
    DESCRIPTION" section for what the supervisor is instructed to
    include in this description.
    """
    @tool_decorator(
        f"transfer_to_{agent_name}",
        description=(
            f"Hand off to {agent_name}. You MUST provide a complete, "
            f"self-contained task_description — this specialist will "
            f"see ONLY this description, not the conversation history."
        ),
    )
    def handoff(
        task_description: Annotated[str, "Complete, self-contained instruction for the specialist, including every relevant detail already known"],
        state: Annotated[dict, InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        from langgraph.graph.message import RemoveMessage
        # Root cause of the BadRequestError: with Command.PARENT, there
        # is only ONE shared MessagesState (the parent's) — there is no
        # separate per-specialist state to give a clean view. Simply
        # APPENDING (the reducer's default) left every prior tool_use/
        # tool_result pair from earlier in the conversation still
        # present when the specialist made its own first model call,
        # and Claude's API strictly requires every tool_result to
        # immediately follow its tool_use in the PREVIOUS message —
        # a requirement that broke once unrelated older pairs were
        # still mixed into what the specialist's call actually sent.
        #
        # Fix: remove every prior message (clearing this history from
        # the SHARED parent state), then add back exactly one valid,
        # self-contained pair: this handoff's own tool_use (already
        # implicitly satisfied by the supervisor's call) and its
        # tool_result, plus the fresh task description. This keeps
        # the parent state's own bookkeeping valid (the one pair that
        # remains is genuinely complete) while ensuring the specialist
        # sees nothing else.
        removals = [RemoveMessage(id=m.id) for m in state["messages"] if getattr(m, "id", None)]
        tool_message = {
            "role": "tool",
            "content": f"Successfully transferred to {agent_name}",
            "name": f"transfer_to_{agent_name}",
            "tool_call_id": tool_call_id,
        }
        task_message = {"role": "user", "content": task_description}
        return Command(
            goto=agent_name,
            update={"messages": [*removals, tool_message, task_message]},
            graph=Command.PARENT,
        )

    return handoff

