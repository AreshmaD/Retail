import asyncio
from types import SimpleNamespace

from scout.agents import supervisor
from scout.agents.diagnostics import SAFE_TIMEOUT_REPLY
from scout.agents.evidence import record_tool_call
from scout.agents.intent_splitter import split_intents_with_metadata
from scout.api.chat import ChatRequest, ChatResponse


class App:
    scout_specialists = {
        "recommend_agent": object(),
        "inventory_agent": object(),
        "order_agent": object(),
        "external_offer_agent": object(),
        "policy_agent": object(),
    }


PRODUCTS = [
    {"product_id": "P001", "name": "Black Midi Dress", "price": 79.99, "source": "internal"},
    {"product_id": "P003", "name": "Wrap Dress", "price": 68.0, "source": "internal"},
]


async def _fake_tool(calls, tool_name, args, *, agent_name):
    calls.append((agent_name, tool_name, dict(args)))
    result = _tool_result(tool_name, args)
    record_tool_call(
        tool_name=tool_name,
        validated_args=args,
        success=True,
        result=result,
        agent_name=agent_name,
    )
    return result


def _tool_result(tool_name, args):
    if tool_name == "recommend_products":
        query = str(args.get("query", "")).lower()
        if "waterproof" in query:
            return []
        return PRODUCTS
    if tool_name == "stores":
        quantity = 2 if args["product_id"] == "P001" else 0
        return {
            "found": True,
            "product_id": args["product_id"],
            "requested_store_name": args.get("store_name"),
            "requested_store_had_no_stock": quantity == 0,
            "stores": [
                {
                    "product_id": args["product_id"],
                    "store_id": "S01",
                    "store_name": args.get("store_name") or "Maple Grove",
                    "quantity": quantity,
                    "in_stock": quantity > 0,
                }
            ],
        }
    if tool_name == "stock":
        return {
            "product_id": args["product_id"],
            "size": args.get("size"),
            "color": args.get("color"),
            "quantity": 0,
            "in_stock": False,
        }
    if tool_name == "retrieve_policy_chunks":
        if "third-party" in str(args.get("query", "")).lower():
            return [
                {
                    "statement": "Scout return policy applies to purchases from Scout.",
                    "policy_name": "Return Policy",
                    "source_document": "returns.md",
                    "source_section": "scope",
                }
            ]
        return [
            {
                "statement": "Opened or worn items are not eligible for return unless defective.",
                "policy_name": "Return Policy",
                "source_document": "returns.md",
                "source_section": "returns",
            }
        ]
    if tool_name == "orders":
        return {"order_id": args["order_id"], "status": "shipped", "found": True}
    if tool_name == "return_eligibility":
        return {"order_id": args["order_id"], "likely_eligible": True, "reason": "Within the return window."}
    if tool_name == "search_external_offers":
        return [
            {
                "external_product_id": "EX001",
                "name": "Waterproof Hiking Shoe",
                "vendor_name": "Trail Vendor",
                "price": 59.99,
                "source": "external",
                "click_url": "/affiliate/click/EX001",
            }
        ]
    raise AssertionError(f"unexpected tool {tool_name}")


def _run_with_fake_tools(monkeypatch, message, conversation_context=None):
    calls = []
    graph_markers = []
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", lambda tool, args, *, agent_name: _fake_tool(calls, tool, args, agent_name=agent_name))
    monkeypatch.setattr(supervisor, "_record_multi_intent_supervisor_path", lambda plan: graph_markers.append([s.intent for s in plan.subgoals]))
    reply, history, products = asyncio.run(supervisor.ask(App(), [], message, conversation_context=conversation_context))
    return SimpleNamespace(reply=reply, history=history, products=products, calls=calls, graph_markers=graph_markers)


def test_mi_01_recommendation_transfers_product_ids_to_store_inventory(monkeypatch):
    result = _run_with_fake_tools(
        monkeypatch,
        "Recommend a dress under $80 and check which one is available at Maple Grove.",
    )

    assert result.graph_markers == [["product_recommendation", "store_availability"]]
    assert [call[1] for call in result.calls] == ["recommend_products", "stores", "stores"]
    assert result.calls[1][2]["product_id"] == "P001"
    assert result.calls[1][2]["store_name"] == "Maple Grove"
    assert "I found 2 Scout dresses" in result.reply
    assert "Black Midi Dress for $79.99" in result.reply
    assert "Maple Grove store with 2 units" in result.reply
    assert [product["product_id"] for product in result.products] == ["P001", "P003"]


def test_mi_02_recommendation_and_policy_complete_without_splitter_timeout(monkeypatch):
    result = _run_with_fake_tools(monkeypatch, "Recommend hiking shoes under $100 and explain the return policy.")

    assert result.graph_markers == [["product_recommendation", "policy_question"]]
    assert [call[1] for call in result.calls] == ["recommend_products", "retrieve_policy_chunks"]
    assert "Opened or worn items usually aren’t eligible for a return unless they’re defective" in result.reply


def test_mi_03_constraints_survive_recommendation_inventory_policy_handoff(monkeypatch):
    result = _run_with_fake_tools(
        monkeypatch,
        "Recommend a black dress under $80, check whether it is available in medium at Maple Grove, and explain whether I can return it after opening it.",
    )

    assert result.graph_markers == [["product_recommendation", "store_availability", "policy_question"]]
    stock_calls = [call for call in result.calls if call[1] == "stock"]
    store_calls = [call for call in result.calls if call[1] == "stores"]
    assert stock_calls[0][2] == {"product_id": "P001", "size": "M", "color": "black"}
    assert store_calls[0][2] == {"product_id": "P001", "store_name": "Maple Grove"}
    assert "Maple Grove has other inventory for that item, but the black, size M option is currently out of stock" in result.reply
    assert "Opened or worn items usually aren’t eligible for a return unless they’re defective" in result.reply


def test_mi_03_short_demo_prompt_is_not_swallowed_by_follow_up_context(monkeypatch):
    context = {
        "active_product_id": "P002",
        "active_product_name": "Floral Sundress",
        "active_selected_products": [{"product_id": "P002", "name": "Floral Sundress"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_store": "Maple Grove",
    }

    result = _run_with_fake_tools(
        monkeypatch,
        "Recommend a black dress under $80, check Maple Grove medium availability, and explain opened-item returns.",
        conversation_context=context,
    )

    assert result.graph_markers == [["product_recommendation", "store_availability", "policy_question"]]
    assert [call[1] for call in result.calls] == [
        "recommend_products",
        "stock",
        "stores",
        "stock",
        "stores",
        "retrieve_policy_chunks",
    ]
    assert result.calls[1][2] == {"product_id": "P001", "size": "M", "color": "black"}
    assert result.calls[2][2] == {"product_id": "P001", "store_name": "Maple Grove"}
    assert "Floral Sundress" not in result.reply
    assert "Maple Grove has other inventory for that item, but the black, size M option is currently out of stock" in result.reply
    assert "Opened or worn items usually aren’t eligible for a return unless they’re defective" in result.reply


def test_mi_04_order_status_and_return_eligibility_are_read_only(monkeypatch):
    result = _run_with_fake_tools(monkeypatch, "Where is order O1001, and is it eligible for return?")

    assert result.graph_markers == [["order_status", "return_eligibility"]]
    assert [call[1] for call in result.calls] == ["orders", "return_eligibility"]
    assert result.reply == "Order O1001 has shipped and is currently eligible for return."
    assert not any(call[1] in {"refund", "cancel", "checkout", "payment"} for call in result.calls)


def test_mi_05_external_fallback_after_internal_insufficiency(monkeypatch):
    result = _run_with_fake_tools(
        monkeypatch,
        "Find waterproof hiking shoes under $70. If Scout has none, show an outside option and explain its return-policy limitation.",
    )

    assert result.graph_markers == [["product_recommendation", "external_offer", "policy_question"]]
    assert [call[1] for call in result.calls] == [
        "recommend_products",
        "search_external_offers",
    ]
    assert result.products == [
        {
            "external_product_id": "EX001",
            "name": "Waterproof Hiking Shoe",
            "vendor_name": "Trail Vendor",
            "source": "external",
            "price": 59.99,
            "click_url": "/affiliate/click/EX001",
        }
    ]
    assert "I found a third-party option: Waterproof Hiking Shoe from Trail Vendor for $59.99" in result.reply
    assert "Scout’s return policy doesn’t apply" in result.reply
    assert "verified return-policy information for that retailer" in result.reply
    assert "Scout return policy applies" not in result.reply


def test_compound_request_cannot_enter_single_intent_direct_routing():
    app = App()
    split = split_intents_with_metadata(
        object(),
        "Recommend a dress under $80 and check which one is available at Maple Grove.",
    )

    assert split.execution_plan is not None
    assert supervisor._direct_route_agent(app, split.structured_intent, split.sub_intents) is None


def test_single_intent_direct_routing_remains_unchanged():
    app = App()
    cases = {
        "Recommend a dress under $80.": "recommend_agent",
        "Is the black dress available at Maple Grove?": "inventory_agent",
        "What is your return policy?": "policy_agent",
        "Where is order O1001?": "order_agent",
    }
    for message, expected in cases.items():
        split = split_intents_with_metadata(object(), message)
        assert split.execution_plan is None
        assert supervisor._direct_route_agent(app, split.structured_intent, split.sub_intents) == expected


def test_streaming_non_streaming_and_api_schemas_remain_compatible(monkeypatch):
    message = "Recommend a dress under $80 and check which one is available at Maple Grove."
    calls = []
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", lambda tool, args, *, agent_name: _fake_tool(calls, tool, args, agent_name=agent_name))

    non_reply, _history, non_products = asyncio.run(supervisor.ask(App(), [], message))
    events = asyncio.run(_collect(supervisor.ask_streaming(App(), [], message)))

    assert events[-1] == ("result", (non_reply, non_products))
    assert set(ChatRequest.model_fields) == {"message", "session_id"}
    assert set(ChatResponse.model_fields) == {"session_id", "reply", "products"}


def test_provider_timeout_still_returns_safe_response(monkeypatch):
    monkeypatch.setattr(supervisor.settings, "CHAT_REQUEST_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    async def slow_tool(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(supervisor, "_execute_read_only_tool", slow_tool)

    reply, _history, products = asyncio.run(
        supervisor.ask(App(), [], "Recommend a dress under $80 and check which one is available at Maple Grove.")
    )

    assert reply == SAFE_TIMEOUT_REPLY
    assert products == []


async def _collect(async_iterable):
    return [item async for item in async_iterable]
