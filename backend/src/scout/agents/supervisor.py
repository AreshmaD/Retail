from __future__ import annotations

import asyncio
import json
import math
from typing import Annotated

from langchain_core.tools import tool as tool_decorator, InjectedToolCallId
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from langgraph_supervisor import create_supervisor

from scout.config import settings
from scout.agents.diagnostics import (
    SAFE_TIMEOUT_REPLY,
    clear_diagnostics,
    get_diagnostics,
    input_size,
    log_request_complete,
    mark_correction_ran,
    mark_timeout,
    record_model_call,
    record_selected_specialist,
    record_tool_call_timing,
    start_diagnostics,
    timed_stage,
)
from scout.agents.model_provider import get_chat_model
from scout.agents.tools_loader import MCPToolManager
from scout.agents.specialists import build_specialists
from scout.agents.intent_splitter import (
    ExecutionPlan,
    PlannedSubIntent,
    SplitIntentResult,
    StructuredIntent,
    classify_clear_single_intent,
    detect_supported_compound_intent,
    split_intents,
    split_intents_with_metadata,
    merge_answers,
)
from scout.agents.tool_guard import (
    clear_inventory_argument_constraints,
    reset_guard,
    set_inventory_argument_constraints,
)
from scout.agents.claims import ClaimType, propose_claims
from scout.agents.rendering import final_safety_scan, render_verified_response
from scout.agents.result_normalization import (
    evidence_entries_from_tool_messages,
    extract_product_candidates,
    extract_tool_result_candidates,
    normalize_agent_result,
)
from scout.agents.verification import verify_claims, verify_price_grounding
from scout.agents.evidence import (
    clear_evidence_context,
    get_evidence_entries,
    get_tool_call_records,
    make_sub_intent_id,
    record_tool_call,
    start_evidence_context,
)

MAX_HISTORY_MESSAGES = 12  # only the most recent N messages get sent to
# the model per call — full history is still stored/returned to the
# caller, this only trims what's actually transmitted as input tokens.
# 12 is enough for the multi-turn memory tests (2-3 turn conversations,
# ~2 messages per turn) while capping cost on long sessions.
_ORIGINAL_SPLIT_INTENTS = split_intents
CORRECTION_FINALIZATION_ALLOWANCE_SECONDS = 3.0
BUDGET_PATTERN = re.compile(
    r"\b(?:under|below|maximum|max|no more than|less than|up to)\s*\$?(\d+(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)
CONTEXT_KEYS = {
    "active_product_id",
    "active_product_name",
    "active_category",
    "active_selected_products",
    "requested_size",
    "requested_color",
    "requested_store",
    "pending_intent",
    "pending_missing_fields",
}
FOLLOW_UP_PRODUCT_RE = re.compile(r"\b(?:it|this|that one|the black one|what about|how about)\b", re.IGNORECASE)
USE_CASE_RE = re.compile(r"^[A-Za-z][A-Za-z\s-]{1,40}[?.!]?$")
CATEGORY_FOLLOW_UP_LABELS = {
    "dress": "dresses",
    "dresses": "dresses",
    "shoe": "shoes",
    "shoes": "shoes",
    "shirt": "shirts",
    "shirts": "shirts",
    "jacket": "jackets",
    "jackets": "jackets",
    "coat": "coats",
    "coats": "coats",
    "pants": "pants",
    "skirt": "skirts",
    "skirts": "skirts",
    "sweater": "sweaters",
    "sweaters": "sweaters",
    "top": "tops",
    "tops": "tops",
    "bag": "bags",
    "bags": "bags",
}


def _trim_history_for_model(history: list[dict]) -> list[dict]:
    if len(history) <= MAX_HISTORY_MESSAGES:
        return history
    return history[-MAX_HISTORY_MESSAGES:]


def _safe_conversation_context(context: dict | None) -> dict | None:
    if context is None:
        return None
    for key in CONTEXT_KEYS:
        context.setdefault(key, [] if key in {"active_selected_products", "pending_missing_fields"} else None)
    return context


def _contextual_split_result(message: str, context: dict | None) -> SplitIntentResult | None:
    context = _safe_conversation_context(context)
    if context is None:
        return None

    compound_plan = detect_supported_compound_intent(message)
    if compound_plan is not None and compound_plan.multi_intent:
        return None

    pending = _continue_pending_clarification(message, context)
    if pending is not None:
        return pending

    resolved = _resolve_follow_up_intent(message, context)
    if resolved is None:
        return None
    if isinstance(resolved, str):
        return SplitIntentResult(
            sub_intents=[message],
            structured_intent=StructuredIntent(
                text=message,
                request_type="shopping_clarification",
                confidence=0.96,
                needs_clarification=True,
                clarification_question=resolved,
            ),
            fast_path_used=True,
            llm_splitter_invoked=False,
        )
    return SplitIntentResult(
        sub_intents=[resolved.text],
        structured_intent=resolved,
        fast_path_used=True,
        llm_splitter_invoked=False,
    )


def _continue_pending_clarification(message: str, context: dict) -> SplitIntentResult | None:
    if context.get("pending_intent") != "product_recommendation":
        return None
    intent = classify_clear_single_intent(message)
    if intent is not None and intent.request_type not in {"shopping_clarification", "product_recommendation"}:
        return None
    if "budget" in (context.get("pending_missing_fields") or []):
        category_follow_up = _category_only_pending_follow_up(message)
        if category_follow_up and _parse_max_budget(message) is None:
            context["active_category"] = category_follow_up
            context["pending_missing_fields"] = ["budget"]
            question = f"Got it — {category_follow_up}. What budget should I use?"
            structured = StructuredIntent(
                text=message,
                request_type="shopping_clarification",
                confidence=0.96,
                product_type=category_follow_up,
                needs_clarification=True,
                clarification_question=question,
                extraction_source="deterministic_context_clarification",
            )
            _record_context_resolution("clarification_budget_followup", True, structured)
            return SplitIntentResult(
                sub_intents=[message],
                structured_intent=structured,
                fast_path_used=True,
                llm_splitter_invoked=False,
            )
    if not context.get("active_category"):
        return None
    normalized = _pending_context_value(message)
    if not normalized or not USE_CASE_RE.match(normalized):
        return None
    if any(term in normalized.lower() for term in ("order", "policy", "refund", "return", "checkout", "charge")):
        return None
    category = str(context["active_category"])
    merged = f"Find {normalized.lower()} {category}"
    merged_intent = StructuredIntent(
        text=merged,
        request_type="product_recommendation",
        confidence=0.93,
        product_type=category,
        extraction_source="deterministic_context_continuation",
    )
    _record_context_resolution("clarification_continuation", True, merged_intent)
    return SplitIntentResult(
        sub_intents=[merged],
        structured_intent=merged_intent,
        fast_path_used=True,
        llm_splitter_invoked=False,
    )


def _pending_context_value(message: str) -> str | None:
    text = (message or "").strip().strip(".!?")
    if not text:
        return None
    lowered = text.lower()
    budget = re.search(r"\b(?:under|below|max(?:imum)?|up to|less than)\s*\$?\d+(?:\.\d{1,2})?\b", lowered)
    if budget:
        return budget.group(0)
    for pattern in (
        r"\bfor\s+([a-z][a-z\s-]{1,30})(?:\s+specifically)?$",
        r"\bsomething\s+for\s+([a-z][a-z\s-]{1,30})(?:\s+specifically)?$",
        r"^(?:what about|how about)\s+([a-z][a-z\s-]{1,30})(?:\s+specifically)?$",
    ):
        match = re.search(pattern, lowered)
        if match:
            return match.group(1).strip()
    stripped = re.sub(r"^(?:what about|how about|something for)\s+", "", lowered).strip()
    stripped = re.sub(r"\s+specifically$", "", stripped).strip()
    return stripped or None


def _category_only_pending_follow_up(message: str) -> str | None:
    lowered = (message or "").strip().strip(".!?").lower()
    if not lowered:
        return None
    labels = []
    for term, label in sorted(CATEGORY_FOLLOW_UP_LABELS.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(term)}\b", lowered) and label not in labels:
            labels.append(label)
    if not labels:
        return None
    remainder = lowered
    for term in CATEGORY_FOLLOW_UP_LABELS:
        remainder = re.sub(rf"\b{re.escape(term)}\b", " ", remainder)
    remainder = re.sub(
        r"\b(?:show|show me|find|recommend|looking for|i need|need|me|some|a|an|nice|good|great|pretty|cute|party|outfit|item|items|options?)\b",
        " ",
        remainder,
    )
    remainder = re.sub(r"\b(?:and|or|plus|also|both)\b|[,/&+]", " ", remainder)
    if re.sub(r"\s+", "", remainder):
        return None
    return _join_context_labels(labels)


def _join_context_labels(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{', '.join(labels[:-1])}, and {labels[-1]}"


def _resolve_follow_up_intent(message: str, context: dict) -> StructuredIntent | str | None:
    base_intent = classify_clear_single_intent(message)
    normalized = (message or "").lower()
    is_follow_up = bool(FOLLOW_UP_PRODUCT_RE.search(normalized))
    if base_intent is None and not is_follow_up:
        return None

    request_type = base_intent.request_type if base_intent else None
    if request_type not in {None, "inventory_availability", "store_availability"}:
        return None
    if request_type is None and not any(term in normalized for term in ("available", "stock", "pickup", "pick up", "pick it up", "medium", "large", "small", "size")):
        return None

    product = _resolve_context_product(message, context)
    if product is None:
        if is_follow_up and context.get("active_selected_products"):
            return "Which product should I check?"
        return None
    if product == "ambiguous":
        return "Which product should I check?"

    size = (base_intent.size if base_intent else None) or _parse_requested_size(message) or context.get("requested_size")
    color = (base_intent.color if base_intent else None) or _parse_requested_color(message) or context.get("requested_color")
    location = (base_intent.location if base_intent else None) or _parse_requested_store_name(message)
    resolved_type = "store_availability" if location or any(term in normalized for term in ("pickup", "pick up", "store")) else "inventory_availability"
    product_name = product.get("name") or context.get("active_product_name") or product["product_id"]
    if resolved_type == "store_availability":
        text = f"Check pickup availability for product_id {product['product_id']} ({product_name})"
        if location:
            text += f" at {location}"
        if size:
            text += f" in size {size}"
        if color:
            text += f" color {color}"
        text += "."
    else:
        text = f"Check stock for product_id {product['product_id']} ({product_name})"
        if size:
            text += f" in size {size}"
        if color:
            text += f" color {color}"
        text += "."
    structured = StructuredIntent(
        text=text,
        request_type=resolved_type,
        confidence=0.96,
        product_type=context.get("active_category") or (base_intent.product_type if base_intent else None),
        budget_max=base_intent.budget_max if base_intent else None,
        size=size,
        color=color,
        location=location,
        product_id=product["product_id"],
        fulfillment_preference="pickup" if resolved_type == "store_availability" else None,
        extraction_source="deterministic_conversation_context",
    )
    _record_context_resolution("follow_up_resolution", True, structured)
    return structured


def _resolve_context_product(message: str, context: dict) -> dict | str | None:
    selected = [item for item in context.get("active_selected_products") or [] if isinstance(item, dict) and item.get("product_id")]
    normalized_message = _normalize_context_text(message)
    explicit_matches = [
        item for item in selected
        if item.get("name") and _normalize_context_text(item["name"]) in normalized_message
    ]
    if len(explicit_matches) == 1:
        return explicit_matches[0]
    if len(explicit_matches) > 1:
        return "ambiguous"
    active_id = context.get("active_product_id")
    if active_id:
        for item in selected:
            if item.get("product_id") == active_id:
                return item
        return {"product_id": active_id, "name": context.get("active_product_name")}
    if len(selected) == 1 and FOLLOW_UP_PRODUCT_RE.search(message or ""):
        return selected[0]
    return None


def _record_context_resolution(stage: str, resolved: bool, intent: StructuredIntent | None = None) -> None:
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "conversation_context",
            0,
            context_stage=stage,
            context_resolved=resolved,
            selected_intent_type=intent.request_type if intent else None,
            llm_splitter_invoked=False if resolved else None,
        )


def _update_context_for_clarification(context: dict | None, structured_intent: StructuredIntent | None) -> None:
    context = _safe_conversation_context(context)
    if context is None or structured_intent is None or structured_intent.request_type != "shopping_clarification":
        return
    if structured_intent.extraction_source == "deterministic_context_clarification":
        return
    context["pending_intent"] = "product_recommendation"
    if structured_intent.product_type:
        context["active_category"] = structured_intent.product_type
        context["pending_missing_fields"] = ["use_case", "budget"]
    else:
        context["pending_missing_fields"] = ["item_type", "budget"]


def _update_context_from_verified_turn(
    context: dict | None,
    *,
    finalized: FinalizedResponse,
    structured_intent: StructuredIntent | None,
) -> None:
    context = _safe_conversation_context(context)
    if context is None:
        return
    approved_ids = set(finalized.verification_result.approved_claim_ids)
    approved_claims = [claim for claim in finalized.proposed_claims if claim.claim_id in approved_ids]
    products = [_context_product(product) for product in finalized.products if _context_product(product)]
    if products:
        context["active_selected_products"] = products
        if structured_intent and structured_intent.product_type:
            context["active_category"] = structured_intent.product_type
        if len(products) == 1:
            context["active_product_id"] = products[0]["product_id"]
            context["active_product_name"] = products[0].get("name")
        context["pending_intent"] = None
        context["pending_missing_fields"] = []

    product_names = {
        claim.subject_id: claim.value
        for claim in approved_claims
        if claim.claim_type == ClaimType.PRODUCT_IDENTITY.value and claim.field in {"name", "product_name"}
    }
    for claim in approved_claims:
        subject = str(claim.subject_id or "")
        product_id = _product_id_from_inventory_subject(subject)
        if not product_id:
            continue
        context["active_product_id"] = product_id
        if product_names.get(product_id):
            context["active_product_name"] = product_names[product_id]
        if ":size:" in subject:
            context["requested_size"] = subject.split(":size:", 1)[1].split(":", 1)[0]
        if ":color:" in subject:
            context["requested_color"] = subject.split(":color:", 1)[1].split(":", 1)[0]
    if structured_intent:
        if structured_intent.size:
            context["requested_size"] = structured_intent.size
        if structured_intent.color:
            context["requested_color"] = structured_intent.color
        if structured_intent.location:
            context["requested_store"] = structured_intent.location


def _context_product(product: dict) -> dict | None:
    product_id = product.get("product_id")
    if not isinstance(product_id, str) or not product_id.strip():
        return None
    output = {"product_id": product_id}
    for key in ("name", "source", "category", "image_url"):
        if isinstance(product.get(key), str) and product[key].strip():
            output[key] = product[key]
    for key in ("price", "rating"):
        if product.get(key) is not None:
            output[key] = product[key]
    promotion = product.get("promotion")
    if isinstance(promotion, dict):
        output["promotion"] = promotion.copy()
    return output


def _product_id_from_inventory_subject(subject: str) -> str | None:
    match = re.search(r"(?:^|:)product:([^:]+)", subject)
    if match:
        return match.group(1)
    if subject.startswith("P"):
        return subject
    return None


def _normalize_context_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _parse_requested_color(customer_message: str) -> str | None:
    lower = (customer_message or "").lower()
    for color in ("black", "white", "red", "blue", "green", "yellow", "pink", "purple", "brown", "gray", "grey", "navy", "cream", "beige"):
        if re.search(rf"\b{re.escape(color)}\b", lower):
            return color
    return None


def _parse_requested_store_name(customer_message: str) -> str | None:
    match = re.search(r"\b(?:at|in|near)\s+([A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*){0,3})\b", customer_message or "")
    return match.group(1).strip() if match else None


def _inventory_constraints_from_structured_intent(
    structured_intent: StructuredIntent | None,
) -> dict[str, str]:
    if structured_intent is None:
        return {}
    if structured_intent.request_type not in {"inventory_availability", "store_availability"}:
        return {}
    constraints = {}
    if structured_intent.product_id:
        constraints["product_id"] = structured_intent.product_id
    if structured_intent.size:
        constraints["size"] = structured_intent.size
    if structured_intent.color:
        constraints["color"] = structured_intent.color
    return constraints


async def _run_tool_first_if_possible(
    *,
    structured_intent: StructuredIntent | None,
    sub_intent: str,
    agent_name: str,
) -> bool:
    if structured_intent is None:
        return False
    if (
        agent_name == "inventory_agent"
        and structured_intent.request_type == "store_availability"
        and structured_intent.product_id
        and structured_intent.location
        and (structured_intent.size or structured_intent.color)
    ):
        stock_args = {"product_id": structured_intent.product_id}
        if structured_intent.size:
            stock_args["size"] = structured_intent.size
        if structured_intent.color:
            stock_args["color"] = structured_intent.color
        await _execute_read_only_tool("stock", stock_args, agent_name=agent_name)
        await _execute_read_only_tool(
            "stores",
            {"product_id": structured_intent.product_id, "store_name": structured_intent.location},
            agent_name=agent_name,
        )
        _record_tool_first("stores", agent_name, continued_from="stock")
        return True
    tool_name, args = _tool_first_call(structured_intent, agent_name)
    if not tool_name:
        return False
    await _execute_read_only_tool(tool_name, args, agent_name=agent_name)
    _record_tool_first(tool_name, agent_name)
    return True


def _tool_first_call(structured_intent: StructuredIntent, agent_name: str) -> tuple[str | None, dict]:
    if agent_name == "inventory_agent":
        if structured_intent.request_type == "inventory_availability" and structured_intent.product_id and structured_intent.size:
            args = {"product_id": structured_intent.product_id, "size": structured_intent.size}
            if structured_intent.color:
                args["color"] = structured_intent.color
            return "stock", args
        if structured_intent.request_type == "store_availability" and structured_intent.product_id and structured_intent.location:
            return "stores", {"product_id": structured_intent.product_id, "store_name": structured_intent.location}
    if agent_name == "external_offer_agent" and structured_intent.request_type == "external_offer":
        category = structured_intent.product_type or _category_from_text(structured_intent.text)
        if category:
            args = _external_offer_args(structured_intent.text, category=category, budget_max=structured_intent.budget_max)
            if structured_intent.budget_max is not None:
                args["max_price"] = structured_intent.budget_max
            return "search_external_offers", args
    return None, {}


async def _execute_read_only_tool(tool_name: str, args: dict, *, agent_name: str) -> object:
    allowed = {
        "inventory_agent": {"stock", "stores"},
        "external_offer_agent": {"search_external_offers"},
        "recommend_agent": {"recommend_products"},
        "order_agent": {"orders", "return_eligibility"},
        "policy_agent": {"retrieve_policy_chunks"},
    }
    if tool_name not in allowed.get(agent_name, set()):
        raise ValueError("deterministic tool-first path attempted a disallowed tool")
    started = time.perf_counter()
    try:
        from scout.mcp_server import server as local_tools

        result = await asyncio.to_thread(getattr(local_tools, tool_name), **args)
    except Exception:
        record_tool_call(
            tool_name=tool_name,
            validated_args=args,
            success=False,
            error_code="tool_execution_failed",
            agent_name=agent_name,
        )
        record_tool_call_timing(
            tool_name=tool_name,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            agent_name=agent_name,
        )
        raise
    record_tool_call(
        tool_name=tool_name,
        validated_args=args,
        success=True,
        result=result,
        agent_name=agent_name,
    )
    record_tool_call_timing(
        tool_name=tool_name,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        agent_name=agent_name,
    )
    return result


def _record_tool_first(tool_name: str, agent_name: str, *, continued_from: str | None = None) -> None:
    record_selected_specialist(agent_name)
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "deterministic_tool_first",
            0,
            deterministic_tool_first_used=True,
            tool_name=tool_name,
            selected_specialist=agent_name,
            model_calls_avoided=1,
            continued_from=continued_from,
        )


def _category_from_text(text: str) -> str | None:
    lowered = (text or "").lower()
    for category in ("dresses", "dress", "shoes", "shoe", "tops", "shirts", "pants", "jeans", "jackets", "coats", "skirts", "bags"):
        if re.search(rf"\b{re.escape(category)}\b", lowered):
            return "dresses" if category == "dress" else "shoes" if category == "shoe" else category
    return None


async def _continue_after_tool_evidence(
    *,
    structured_intent: StructuredIntent | None,
    agent_name: str,
) -> bool:
    if structured_intent is None:
        return False
    entries = [entry for entry in get_evidence_entries() if getattr(entry, "success", False)]
    records = [record for record in get_tool_call_records() if getattr(record, "success", False)]
    latest = entries[-1] if entries else None
    if agent_name == "inventory_agent" and latest is not None and latest.tool_name == "search":
        candidate = _unique_product_candidate(latest.normalized_facts)
        if candidate is None:
            return False
        product_id = candidate["product_id"]
        if structured_intent.request_type == "inventory_availability" and structured_intent.size:
            args = {"product_id": product_id, "size": structured_intent.size}
            if structured_intent.color:
                args["color"] = structured_intent.color
            await _execute_read_only_tool("stock", args, agent_name="inventory_agent")
            _record_tool_first("stock", "inventory_agent", continued_from="search")
            return True
        if structured_intent.request_type == "store_availability" and structured_intent.location:
            await _execute_read_only_tool(
                "stores",
                {"product_id": product_id, "store_name": structured_intent.location},
                agent_name="inventory_agent",
            )
            _record_tool_first("stores", "inventory_agent", continued_from="search")
            return True
    latest_record = records[-1] if records else None
    if agent_name == "recommend_agent" and latest_record and latest_record.tool_name in {"recommend_products", "search"}:
        products = [
            facts
            for entry in entries
            if entry.tool_name in {"recommend_products", "search"}
            for facts in _iter_completion_facts(entry.normalized_facts)
            if facts.get("product_id") and facts.get("name")
        ]
        if _internal_products_satisfy_explicit_request(products, structured_intent) or _scout_only_requested(structured_intent.text):
            return False
        category = structured_intent.product_type or _category_from_text(structured_intent.text)
        if not category:
            return False
        args = _external_offer_args(structured_intent.text, category=category, budget_max=structured_intent.budget_max)
        await _execute_read_only_tool("search_external_offers", args, agent_name="external_offer_agent")
        _record_tool_first("search_external_offers", "external_offer_agent", continued_from=latest_record.tool_name)
        return True
    return False


def _unique_product_candidate(facts: dict) -> dict | None:
    candidates = []
    for item in _iter_completion_facts(facts):
        product_id = item.get("product_id")
        if isinstance(product_id, str) and product_id.strip():
            candidates.append({"product_id": product_id, "name": item.get("name") or item.get("product_name")})
    by_id = {}
    for candidate in candidates:
        by_id.setdefault(candidate["product_id"], candidate)
    values = list(by_id.values())
    return values[0] if len(values) == 1 else None


def _scout_only_requested(text: str) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in ("scout only", "only scout", "in your inventory only", "internal only"))


def _internal_products_satisfy_explicit_request(products: list[dict], structured_intent: StructuredIntent) -> bool:
    if not products:
        return False
    lowered_request = (structured_intent.text or "").lower()
    required_terms = []
    if structured_intent.color:
        required_terms.append(structured_intent.color.lower())
    for term in ("cocktail", "hiking", "running", "waterproof", "work"):
        if re.search(rf"\b{term}\b", lowered_request):
            required_terms.append(term)
    if not required_terms:
        return True
    return any(_product_contains_terms(product, required_terms) for product in products)


def _product_contains_terms(product: dict, terms: list[str]) -> bool:
    searchable_values = []
    for key in ("name", "category", "department", "brand", "description"):
        value = product.get(key)
        if isinstance(value, str):
            searchable_values.append(value.lower())
    for key in ("tags", "colors"):
        value = product.get(key)
        if isinstance(value, list):
            searchable_values.extend(str(item).lower() for item in value)
        elif isinstance(value, str):
            searchable_values.append(value.lower())
    haystack = " ".join(searchable_values)
    return all(term in haystack for term in terms)


def can_finalize_from_evidence(
    *,
    intent: str,
    structured_intent: StructuredIntent | None,
    evidence_entries: list,
    tool_history: list | None = None,
) -> EvidenceCompletionDecision:
    successful = [entry for entry in evidence_entries if getattr(entry, "success", False)]
    if not successful:
        return EvidenceCompletionDecision(False)
    request_type = structured_intent.request_type if structured_intent else intent

    if request_type == "product_recommendation":
        products = [
            facts
            for entry in successful
            if entry.tool_name in {"recommend_products", "search"}
            for facts in _iter_completion_facts(entry.normalized_facts)
            if facts.get("product_id") and facts.get("name") and facts.get("price") is not None
        ]
        max_budget = Decimal(str(structured_intent.budget_max)) if structured_intent and structured_intent.budget_max is not None else None
        if products and all(max_budget is None or _decimal_money(product.get("price")) <= max_budget for product in products if _decimal_money(product.get("price")) is not None):
            return EvidenceCompletionDecision(True, "product", [entry.evidence_id for entry in successful if entry.tool_name in {"recommend_products", "search"}], "product_evidence_complete")

    if request_type == "external_offer":
        offers = [
            facts
            for entry in successful
            if entry.tool_name == "search_external_offers"
            for facts in _iter_completion_facts(entry.normalized_facts)
            if facts.get("external_product_id") and facts.get("name") and facts.get("vendor_name") and facts.get("price") is not None and facts.get("click_url")
        ]
        if offers:
            return EvidenceCompletionDecision(True, "external", [entry.evidence_id for entry in successful if entry.tool_name == "search_external_offers"], "external_offer_evidence_complete")

    if request_type == "inventory_availability":
        requested_size = structured_intent.size if structured_intent else None
        requested_color = structured_intent.color.lower() if structured_intent and structured_intent.color else None
        for entry in successful:
            if entry.tool_name != "stock":
                continue
            for facts in _iter_completion_facts(entry.normalized_facts):
                if not facts.get("product_id"):
                    continue
                if requested_size and str(facts.get("size", "")).upper() != requested_size:
                    continue
                if requested_color and str(facts.get("color", "")).lower() != requested_color:
                    continue
                if facts.get("quantity") is not None or facts.get("in_stock") is not None:
                    return EvidenceCompletionDecision(True, "inventory", [entry.evidence_id], "inventory_evidence_complete")

    if request_type == "store_availability":
        requested_store = (structured_intent.location or "").strip().lower() if structured_intent else ""
        for entry in successful:
            if entry.tool_name != "stores":
                continue
            for facts in _iter_completion_facts(entry.normalized_facts):
                if not facts.get("product_id"):
                    continue
                store_name = str(facts.get("store_name", "")).strip().lower()
                if requested_store and store_name != requested_store:
                    continue
                if facts.get("store_id") and (facts.get("quantity") is not None or facts.get("in_stock") is not None):
                    return EvidenceCompletionDecision(True, "store", [entry.evidence_id], "store_evidence_complete")

    if request_type == "order_status":
        requested_order = structured_intent.order_id if structured_intent else None
        for entry in successful:
            if entry.tool_name != "orders":
                continue
            for facts in _iter_completion_facts(entry.normalized_facts):
                if facts.get("order_id") == requested_order and facts.get("status"):
                    return EvidenceCompletionDecision(True, "order", [entry.evidence_id], "order_status_evidence_complete")

    if request_type == "policy_question":
        policy_entries = [
            entry
            for entry in successful
            if entry.tool_name == "retrieve_policy_chunks"
            and any(facts.get("statement") for facts in _iter_completion_facts(entry.normalized_facts))
        ]
        if policy_entries:
            return EvidenceCompletionDecision(True, "policy", [entry.evidence_id for entry in policy_entries], "policy_evidence_complete")

    return EvidenceCompletionDecision(False)


def _iter_completion_facts(facts: dict):
    if isinstance(facts, dict):
        yield facts
        for key in ("items", "stores"):
            value = facts.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item


def _split_intents_for_supervisor(model, message: str) -> SplitIntentResult:
    if split_intents is not _ORIGINAL_SPLIT_INTENTS:
        return SplitIntentResult(sub_intents=split_intents(model, message))
    return split_intents_with_metadata(model, message)


def _result_contains_messages(value) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("messages"), list):
            return True
        return any(_result_contains_messages(item) for item in value.values())
    return isinstance(getattr(value, "messages", None), list)


def _finalize_verified_response(
    *,
    original_reply: str,
    products: list,
    evidence_entries: list,
    customer_message: str,
) -> FinalizedResponse:
    with timed_stage("claim_proposal"):
        proposed_claims = propose_claims(
            reply_text=original_reply,
            products=products,
            evidence_entries=evidence_entries,
        )
    with timed_stage("claim_verification"):
        verification_result = verify_claims(
            proposed_claims=proposed_claims,
            evidence_entries=evidence_entries,
            customer_message=customer_message,
        )
    with timed_stage("rendering"):
        rendered_reply, rendered_products = render_verified_response(
            original_reply=original_reply,
            products=products,
            proposed_claims=proposed_claims,
            verification_result=verification_result,
            customer_message=customer_message,
        )
    passed, _ = verify_price_grounding(rendered_reply, rendered_products, customer_message=customer_message)
    if not passed:
        rendered_reply = "I couldn’t verify a reliable product result for that request."
        rendered_products = []
    approved_ids = set(verification_result.approved_claim_ids)
    rendered_reply, rendered_products = final_safety_scan(
        reply=rendered_reply,
        products=rendered_products,
        approved_claims=[claim for claim in proposed_claims if claim.claim_id in approved_ids],
        customer_message=customer_message,
    )
    return FinalizedResponse(
        reply=rendered_reply,
        products=rendered_products,
        proposed_claims=proposed_claims,
        verification_result=verification_result,
    )


def _response_usefulness(result: FinalizedResponse) -> tuple[int, int, int]:
    approved_count = len(result.verification_result.approved_claim_ids)
    rejected_count = len(result.verification_result.rejected_claims)
    product_count = len(result.products)
    return approved_count, -rejected_count, product_count


def _choose_better_response(
    initial: FinalizedResponse,
    corrected: FinalizedResponse,
) -> FinalizedResponse:
    return corrected if _response_usefulness(corrected) > _response_usefulness(initial) else initial


def _get_specialist_registry(app) -> dict:
    registry = getattr(app, "scout_specialists", None)
    return registry if isinstance(registry, dict) else {}


def _direct_route_agent(app, structured_intent: StructuredIntent | None, sub_intents: list[str]) -> str | None:
    if structured_intent is None or len(sub_intents) != 1:
        return None
    if structured_intent.needs_clarification:
        return None
    specialist_name = DIRECT_ROUTE_SPECIALISTS.get(structured_intent.request_type)
    if specialist_name is None:
        return None
    return specialist_name if specialist_name in _get_specialist_registry(app) else None


def _deterministic_conversational_reply(structured_intent: StructuredIntent | None, sub_intents: list[str]) -> str | None:
    if structured_intent is None or len(sub_intents) != 1:
        return None
    if structured_intent.request_type == "shopping_clarification" and structured_intent.clarification_question:
        return structured_intent.clarification_question
    return DETERMINISTIC_CONVERSATIONAL_REPLIES.get(structured_intent.request_type)


def _should_attempt_targeted_correction(
    finalized: FinalizedResponse,
    *,
    correction_attempted: bool,
    specialists: dict,
    customer_message: str = "",
    correction_deadline: float | None = None,
    now: float | None = None,
) -> bool:
    if correction_attempted or finalized.verification_result.verified:
        return False

    if _initial_product_result_is_sufficient(finalized, customer_message=customer_message):
        return False

    if not _has_enough_time_for_correction(correction_deadline, now=now):
        return False

    correction_agent = finalized.verification_result.correction_agent
    if correction_agent not in CORRECTION_SPECIALISTS or correction_agent not in specialists:
        return False

    rejected_claims = finalized.verification_result.rejected_claims
    if not rejected_claims:
        return False

    specialist_rejections = [
        rejected
        for rejected in rejected_claims
        if rejected.responsible_agent == correction_agent
    ]
    if not specialist_rejections:
        return False

    if not _has_essential_correctable_rejection(finalized, specialist_rejections):
        return False

    if any(
        rejected.responsible_agent not in {correction_agent, "supervisor", None}
        for rejected in rejected_claims
    ):
        return False

    return all(
        rejected.responsible_agent == correction_agent
        and rejected.reason_code in CORRECTABLE_REJECTION_CODES
        for rejected in specialist_rejections
    )


def _initial_product_result_is_sufficient(finalized: FinalizedResponse, *, customer_message: str) -> bool:
    requested_domain = _requested_domain(customer_message)
    if requested_domain in {"inventory", "store"}:
        return _initial_availability_result_is_sufficient(finalized, customer_message=customer_message, requested_domain=requested_domain)
    if requested_domain != "product":
        return False
    if not _is_product_result(finalized):
        return False
    if not finalized.products:
        return False
    approved_claims = _approved_claims_by_subject(finalized)
    max_budget = _parse_max_budget(customer_message)
    for product in finalized.products:
        if not isinstance(product, dict):
            continue
        subject_id = product.get("external_product_id") or product.get("product_id")
        if not subject_id:
            continue
        subject_claims = approved_claims.get(str(subject_id), {})
        if not _product_identity_is_approved(product, subject_claims):
            continue
        if not _product_price_is_approved(product, subject_claims):
            continue
        if not _product_satisfies_budget(product, max_budget):
            continue
        passed, _ = verify_price_grounding(finalized.reply, finalized.products, customer_message=customer_message)
        if passed:
            return True
    return False


def _initial_availability_result_is_sufficient(
    finalized: FinalizedResponse,
    *,
    customer_message: str,
    requested_domain: str,
) -> bool:
    if not finalized.reply or not finalized.products and not finalized.proposed_claims:
        return False
    approved_claims = [
        claim
        for claim in finalized.proposed_claims
        if claim.claim_id in set(finalized.verification_result.approved_claim_ids)
    ]
    if requested_domain == "inventory":
        requested_size = _parse_requested_size(customer_message)
        for claim in approved_claims:
            if claim.claim_type not in {ClaimType.INVENTORY_AVAILABILITY.value, ClaimType.INVENTORY_QUANTITY.value}:
                continue
            if requested_size and f":size:{requested_size}" not in str(claim.subject_id):
                continue
            passed, _ = verify_price_grounding(finalized.reply, finalized.products, customer_message=customer_message)
            return passed
    if requested_domain == "store":
        requested_store = _parse_requested_store(customer_message)
        store_claims = {
            claim.subject_id: claim.value
            for claim in approved_claims
            if claim.claim_type == ClaimType.STORE_IDENTITY.value and claim.field == "store_name"
        }
        matching_store_ids = {
            store_id
            for store_id, store_name in store_claims.items()
            if requested_store is None or str(store_name).strip().lower() == requested_store
        }
        for claim in approved_claims:
            if claim.claim_type not in {ClaimType.INVENTORY_AVAILABILITY.value, ClaimType.INVENTORY_QUANTITY.value, ClaimType.PICKUP_AVAILABILITY.value}:
                continue
            subject = str(claim.subject_id)
            if any(f":store:{store_id}" in subject for store_id in matching_store_ids):
                passed, _ = verify_price_grounding(finalized.reply, finalized.products, customer_message=customer_message)
                return passed
    return False


def _requested_domain(customer_message: str) -> str:
    message = (customer_message or "").lower()
    if any(term in message for term in ("order", "tracking")):
        return "order"
    if any(term in message for term in ("policy", "refund", "return")):
        return "policy"
    if any(term in message for term in ("third-party", "third party", "external")):
        return "external"
    if any(term in message for term in ("store", " at ")):
        return "store"
    if any(term in message for term in ("available", "availability", "stock", "in stock", "size")) or _parse_requested_size(message):
        return "inventory"
    return "product"


def _parse_requested_size(customer_message: str) -> str | None:
    match = re.search(
        r"\b(?:in\s+(?:a\s+)?|available\s+in\s+|size\s+)(extra small|extra large|xs|small|medium|large|xl|xxl|s|m|l)\b|"
        r"\b(extra small|extra large|xs|small|medium|large|xl|xxl|s|m|l)\s+size\b",
        customer_message or "",
        re.IGNORECASE,
    )
    if not match:
        return None
    value = next(group for group in match.groups() if group)
    return {
        "xs": "XS",
        "extra small": "XS",
        "s": "S",
        "small": "S",
        "m": "M",
        "medium": "M",
        "l": "L",
        "large": "L",
        "xl": "XL",
        "extra large": "XL",
        "xxl": "XXL",
    }.get(value.lower())


def _parse_requested_store(customer_message: str) -> str | None:
    match = re.search(r"\b(?:at|in|near)\s+([A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*){0,3})\b", customer_message or "")
    return match.group(1).strip().lower() if match else None


def _is_product_result(finalized: FinalizedResponse) -> bool:
    if finalized.products:
        return True
    product_claim_types = {
        ClaimType.PRODUCT_IDENTITY.value,
        ClaimType.PRODUCT_PRICE.value,
        ClaimType.PRODUCT_RATING.value,
        ClaimType.PROMOTION.value,
        ClaimType.EXTERNAL_OFFER_IDENTITY.value,
        ClaimType.EXTERNAL_OFFER_PRICE.value,
        ClaimType.EXTERNAL_OFFER_VENDOR.value,
    }
    return any(getattr(claim, "claim_type", None) in product_claim_types for claim in finalized.proposed_claims)


def _approved_claims_by_subject(finalized: FinalizedResponse) -> dict[str, dict[str, set]]:
    approved_ids = set(finalized.verification_result.approved_claim_ids)
    by_subject: dict[str, dict[str, set]] = {}
    for claim in finalized.proposed_claims:
        if claim.claim_id not in approved_ids or claim.subject_id is None:
            continue
        subject_claims = by_subject.setdefault(str(claim.subject_id), {})
        subject_claims.setdefault(str(claim.claim_type), set()).add((claim.field, _normalized_claim_value(claim.value)))
    return by_subject


def _product_identity_is_approved(product: dict, subject_claims: dict[str, set]) -> bool:
    name = _normalized_claim_value(product.get("name"))
    identity_claims = subject_claims.get(ClaimType.PRODUCT_IDENTITY.value, set()) | subject_claims.get(
        ClaimType.EXTERNAL_OFFER_IDENTITY.value, set()
    )
    return any(field in {"name", "product_name"} and value == name for field, value in identity_claims)


def _product_price_is_approved(product: dict, subject_claims: dict[str, set]) -> bool:
    if product.get("price") is None:
        return False
    price = _normalized_claim_value(product.get("price"))
    price_claims = subject_claims.get(ClaimType.PRODUCT_PRICE.value, set()) | subject_claims.get(
        ClaimType.EXTERNAL_OFFER_PRICE.value, set()
    )
    return ("price", price) in price_claims


def _product_satisfies_budget(product: dict, max_budget: Decimal | None) -> bool:
    if max_budget is None:
        return True
    price = _decimal_money(product.get("price"))
    return price is not None and price <= max_budget


def _has_essential_correctable_rejection(finalized: FinalizedResponse, specialist_rejections: list) -> bool:
    claims_by_id = {claim.claim_id: claim for claim in finalized.proposed_claims}
    optional_product_claims = {ClaimType.PRODUCT_RATING.value, ClaimType.PROMOTION.value}
    for rejected in specialist_rejections:
        claim = claims_by_id.get(rejected.claim_id)
        if claim is None:
            return True
        if claim.claim_type not in optional_product_claims:
            return True
    return False


def _has_enough_time_for_correction(correction_deadline: float | None, *, now: float | None = None) -> bool:
    if correction_deadline is None:
        return True
    current = time.monotonic() if now is None else now
    required = settings.MODEL_INVOCATION_TIMEOUT_SECONDS + CORRECTION_FINALIZATION_ALLOWANCE_SECONDS
    return correction_deadline - current >= required


def _parse_max_budget(customer_message: str) -> Decimal | None:
    matches = BUDGET_PATTERN.findall(customer_message or "")
    if len(matches) != 1:
        return None
    return _decimal_money(matches[0])


def _decimal_money(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _normalized_claim_value(value) -> str:
    money = _decimal_money(value)
    if money is not None:
        return f"{money:.2f}"
    return str(value).strip().lower()


def _build_correction_instruction(
    *,
    original_request: str,
    finalized: FinalizedResponse,
) -> str:
    verification_result = finalized.verification_result
    claims_by_id = {claim.claim_id: claim for claim in finalized.proposed_claims}
    rejected_claims = verification_result.rejected_claims
    claim_types = _unique_text([
        claims_by_id[rejected.claim_id].claim_type
        for rejected in rejected_claims
        if rejected.claim_id in claims_by_id
    ])
    subject_ids = _unique_text([
        claims_by_id[rejected.claim_id].subject_id
        for rejected in rejected_claims
        if rejected.claim_id in claims_by_id
    ])
    reason_codes = _unique_text([rejected.reason_code for rejected in rejected_claims])
    missing = _unique_text([
        missing
        for rejected in rejected_claims
        for missing in rejected.missing_evidence
        if missing
    ])

    return (
        f"Recheck this customer request: {original_request}. "
        f"Affected claim types: {', '.join(claim_types) or 'unknown'}. "
        f"Affected subjects or evidence categories: {', '.join(subject_ids or missing) or 'unknown'}. "
        f"Safe rejection categories: {', '.join(reason_codes)}. "
        "Use only your approved tools. Return only facts directly supported by new tool results. "
        "Do not repeat unsupported facts."
    )


def _unique_text(values: list) -> list[str]:
    seen = set()
    output = []
    for value in values:
        if not value:
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output

SPECIALIST_NAMES = {
    "recommend_agent",
    "inventory_agent",
    "order_agent",
    "external_offer_agent",
    "policy_agent",
}
PRODUCT_TOOL_NAMES = {"search", "alternatives", "search_external_offers", "recommend_products"}
HANDOFF_MARKERS = {"Transferring back to supervisor"}
CORRECTABLE_REJECTION_CODES = {
    "missing_evidence",
    "unknown_evidence_id",
    "unsuccessful_evidence",
    "subject_mismatch",
    "field_not_present",
    "value_mismatch",
    "ambiguous_subject",
    "budget_exceeded",
    "wrong_agent_domain",
    "external_offer_not_labeled",
    "insufficient_policy_support",
}
CORRECTION_SPECIALISTS = {
    "recommend_agent",
    "inventory_agent",
    "order_agent",
    "external_offer_agent",
    "policy_agent",
}
DIRECT_ROUTE_SPECIALISTS = {
    "product_recommendation": "recommend_agent",
    "inventory_availability": "inventory_agent",
    "store_availability": "inventory_agent",
    "order_status": "order_agent",
    "return_eligibility": "order_agent",
    "policy_question": "policy_agent",
    "external_offer": "external_offer_agent",
}
DETERMINISTIC_CONVERSATIONAL_REPLIES = {
    "greeting": "Hi — how can I help you shop today?",
    "thanks": "You're welcome — happy to help.",
    "purchase_execution": (
        "I can help you select the product and prepare your cart, but payment must be "
        "completed through Scout's secure storefront checkout."
    ),
    "shopping_clarification": (
        "What type of item are you looking for, and what budget would you like to stay within?"
    ),
    "out_of_scope": (
        "I don’t have live access for that kind of request here, but I can help with Scout products, "
        "availability, orders, checkout guidance, and return or refund policy."
    ),
}


@dataclass
class FinalizedResponse:
    reply: str
    products: list
    proposed_claims: list
    verification_result: object


@dataclass
class MultiIntentExecutionState:
    multi_intent: bool
    execution_plan: ExecutionPlan
    selected_product_ids: list[str]
    selected_products: list[dict]
    comparison_candidates: list[dict]
    ranking_criteria: list[str]
    selected_best_product_id: str | None
    requested_stores: list[str]
    budget_max: float | None
    requested_size: str | None
    requested_color: str | None
    requested_store: str | None
    completed_subgoals: list[str]
    pending_subgoals: list[str]
    evidence_by_specialist: dict[str, list]
    current_subgoal: str | None = None
    next_subgoal: str | None = None


@dataclass
class EvidenceCompletionDecision:
    complete: bool
    responsible_domain: str = ""
    supporting_evidence_ids: list[str] | None = None
    completion_reason: str = ""

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


def _get_final_reply_for_agent(messages: list, agent_name: str) -> str:
    for msg in reversed(messages):
        if getattr(msg, "name", None) != agent_name:
            continue
        text = extract_text(getattr(msg, "content", ""))
        stripped = text.strip()
        if stripped and stripped not in HANDOFF_MARKERS and not stripped.startswith("NEEDS_EXTERNAL_CHECK"):
            return text
    return "I couldn’t verify a corrected answer from the selected specialist."


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
    return extract_tool_result_candidates(messages)


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
        _make_passthrough_handoff_tool(name)
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


def _make_passthrough_handoff_tool(agent_name: str):
    """STEP 1 ONLY — forwards full history unchanged, same as
    create_supervisor()'s current behavior. Includes a matching tool
    RESULT message for the handoff call itself (required — LangGraph's
    message validation raised KeyError on the tool_call_id when this
    was missing in the first draft, confirmed via direct testing).
    Still NOT the fix yet — Step 2 replaces the body with the real
    task-description logic, keeping this same tool-result-completion
    structure.
    """
    @tool_decorator(f"transfer_to_{agent_name}")
    def handoff(
        state: Annotated[dict, InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        tool_message = {
            "role": "tool",
            "content": f"Successfully transferred to {agent_name}",
            "name": f"transfer_to_{agent_name}",
            "tool_call_id": tool_call_id,
        }
        return Command(
            goto=agent_name,
            update={"messages": [*state["messages"], tool_message]},
        )

    return handoff


async def _wait_for(coro, timeout_seconds: float, stage: str):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        mark_timeout()
        raise TimeoutError(f"{stage} timed out") from exc


class ProviderWorkflowError(RuntimeError):
    pass


def _is_provider_or_transport_error(exc: BaseException) -> bool:
    module_name = exc.__class__.__module__.lower()
    class_name = exc.__class__.__name__.lower()
    return (
        isinstance(exc, (TimeoutError, asyncio.TimeoutError))
        or "httpx" in module_name
        or "httpcore" in module_name
        or "ollama" in module_name
        or "readtimeout" in class_name
        or "connecterror" in class_name
        or "connection" in class_name
    )


def _record_handled_provider_error(exc: BaseException) -> None:
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "handled_provider_error",
            0,
            handled_provider_error_code=exc.__class__.__name__,
        )


async def _invoke_graph_with_timing(app, payload: dict, *, config: dict):
    if not hasattr(app, "astream_events"):
        messages = payload.get("messages", [])
        count, chars = input_size(messages)
        started = time.perf_counter()
        try:
            result = await app.ainvoke(payload, config=config)
        except Exception as exc:
            if _is_provider_or_transport_error(exc):
                _record_handled_provider_error(exc)
                raise ProviderWorkflowError("provider failure") from exc
            raise
        record_model_call(
            stage="supervisor_model_invocation",
            elapsed_ms=(time.perf_counter() - started) * 1000,
            message_count=count,
            input_chars=chars,
        )
        return result

    final_result = None
    model_starts: dict[str, tuple[float, int, int, str]] = {}
    try:
        stream = app.astream_events(payload, version="v2", config=config)
        async for event in stream:
            event_type = event.get("event")
            node_name = event.get("metadata", {}).get("langgraph_node", "")
            run_id = str(event.get("run_id", ""))

            if event_type == "on_chain_start" and node_name in SPECIALIST_NAMES:
                record_selected_specialist(node_name)

            if event_type == "on_chat_model_start":
                messages = event.get("data", {}).get("input", {}).get("messages", [])
                count, chars = input_size(messages if isinstance(messages, list) else [])
                model_starts[run_id] = (time.perf_counter(), count, chars, node_name or "unknown")

            if event_type == "on_chat_model_end" and run_id in model_starts:
                started, count, chars, node = model_starts.pop(run_id)
                stage = "specialist_model_invocation" if node in SPECIALIST_NAMES else "supervisor_model_invocation"
                record_model_call(
                    stage=stage,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    message_count=count,
                    input_chars=chars,
                )

            if event_type == "on_chain_end":
                output = event.get("data", {}).get("output")
                if event.get("name") == "LangGraph" or _result_contains_messages(output):
                    final_result = output
    except Exception as exc:
        if _is_provider_or_transport_error(exc):
            _record_handled_provider_error(exc)
            raise ProviderWorkflowError("provider failure") from exc
        raise

    return final_result


async def _invoke_agent_with_timing(
    agent,
    payload: dict,
    *,
    config: dict,
    agent_name: str,
    structured_intent: StructuredIntent | None = None,
):
    if not hasattr(agent, "astream_events"):
        messages = payload.get("messages", [])
        count, chars = input_size(messages)
        started = time.perf_counter()
        try:
            result = await agent.ainvoke(payload, config=config)
        except Exception as exc:
            if _is_provider_or_transport_error(exc):
                _record_handled_provider_error(exc)
                raise ProviderWorkflowError("provider failure") from exc
            raise
        record_model_call(
            stage=agent_name,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            message_count=count,
            input_chars=chars,
        )
        return result

    final_result = None
    model_starts: dict[str, tuple[float, int, int]] = {}
    record_selected_specialist(agent_name)
    early_completed = False
    stream = agent.astream_events(payload, version="v2", config=config)
    try:
        async for event in stream:
            event_type = event.get("event")
            run_id = str(event.get("run_id", ""))

            if event_type == "on_chat_model_start":
                messages = event.get("data", {}).get("input", {}).get("messages", [])
                count, chars = input_size(messages if isinstance(messages, list) else [])
                model_starts[run_id] = (time.perf_counter(), count, chars)

            if event_type == "on_chat_model_end" and run_id in model_starts:
                started, count, chars = model_starts.pop(run_id)
                record_model_call(
                    stage=agent_name,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    message_count=count,
                    input_chars=chars,
                )

            if event_type == "on_chain_end":
                output = event.get("data", {}).get("output")
                if event.get("name") == "LangGraph" or _result_contains_messages(output):
                    final_result = output

            if event_type == "on_tool_end":
                if await _continue_after_tool_evidence(
                    structured_intent=structured_intent,
                    agent_name=agent_name,
                ):
                    final_result = {"messages": []}
                    early_completed = True
                    break

            if event_type in {"on_tool_end", "on_chain_end"}:
                decision = can_finalize_from_evidence(
                    intent=structured_intent.request_type if structured_intent else "",
                    structured_intent=structured_intent,
                    evidence_entries=get_evidence_entries(),
                    tool_history=[],
                )
                if decision.complete:
                    diagnostics = get_diagnostics()
                    if diagnostics is not None:
                        diagnostics.record(
                            "evidence_early_completion",
                            0,
                            evidence_early_completion_used=True,
                            evidence_completion_domain=decision.responsible_domain,
                            evidence_completion_reason=decision.completion_reason,
                            model_calls_avoided=1,
                        )
                    final_result = {"messages": []}
                    early_completed = True
                    break
    except Exception as exc:
        if _is_provider_or_transport_error(exc):
            _record_handled_provider_error(exc)
            raise ProviderWorkflowError("provider failure") from exc
        raise
    finally:
        if early_completed and hasattr(stream, "aclose"):
            await stream.aclose()
            diagnostics = get_diagnostics()
            if diagnostics is not None:
                diagnostics.record(
                    "specialist_stream_cancelled",
                    0,
                    specialist_stream_cancelled=True,
                    cancellation_cleanup_completed=True,
                )

    return final_result


async def _run_direct_specialist_turn(app, history: list[dict], messages_before: int, sub_intent: str, specialist_name: str):
    specialists = _get_specialist_registry(app)
    specialist = specialists[specialist_name]
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "direct_routing",
            0,
            direct_route_used=True,
            selected_specialist=specialist_name,
            supervisor_invoked=False,
        )

    result = await _invoke_agent_with_timing(
        specialist,
        {"messages": _trim_history_for_model(history)},
        config={"recursion_limit": 15},
        agent_name=specialist_name,
        structured_intent=getattr(app, "_scout_structured_intent", None),
    )
    normalized = normalize_agent_result(
        result=result,
        input_message_count=messages_before,
        execution_mode="direct",
    )
    new_messages = normalized.messages

    if specialist_name == "recommend_agent" and normalized.routing_marker_present:
        external_agent = specialists.get("external_offer_agent")
        if external_agent is not None:
            diagnostics = get_diagnostics()
            if diagnostics is not None:
                diagnostics.record("external_fallback", 0, external_fallback_used=True)
            reset_guard(max_total_calls=10, max_identical_calls=1)
            external_result = await _invoke_agent_with_timing(
                external_agent,
                {"messages": [{"role": "user", "content": sub_intent}]},
                config={"recursion_limit": 15},
                agent_name="external_offer_agent",
                structured_intent=StructuredIntent(
                    text=sub_intent,
                    request_type="external_offer",
                    confidence=0.8,
                ),
            )
            external_normalized = normalize_agent_result(
                result=external_result,
                input_message_count=0,
                execution_mode="direct",
            )
            return external_normalized.messages, True

    return new_messages, False


async def _attempt_targeted_correction(
    app,
    *,
    initial: FinalizedResponse,
    customer_message: str,
    correction_attempted: bool,
    sub_intent_id: str,
    correction_deadline: float | None = None,
) -> FinalizedResponse:
    specialists = _get_specialist_registry(app)
    if not _should_attempt_targeted_correction(
        initial,
        correction_attempted=correction_attempted,
        specialists=specialists,
        customer_message=customer_message,
        correction_deadline=correction_deadline,
    ):
        return initial

    correction_agent = initial.verification_result.correction_agent
    mark_correction_ran()
    specialist = specialists[correction_agent]
    instruction = _build_correction_instruction(
        original_request=customer_message,
        finalized=initial,
    )

    start_evidence_context(sub_intent_id, attempt_number=1)
    reset_guard(max_total_calls=10, max_identical_calls=1)
    try:
        result = await _wait_for(
            specialist.ainvoke(
                {"messages": [{"role": "user", "content": instruction}]},
                config={"recursion_limit": 15},
            ),
            settings.SUB_INTENT_TIMEOUT_SECONDS,
            "targeted correction",
        )
    except (Exception, TimeoutError):
        return initial

    correction_normalized = normalize_agent_result(
        result=result,
        input_message_count=0,
        execution_mode="direct",
    )
    correction_messages = correction_normalized.messages
    corrected_reply = _get_final_reply_for_agent(correction_messages, correction_agent)
    combined_evidence = get_evidence_entries()
    if not combined_evidence:
        combined_evidence = evidence_entries_from_tool_messages(
            normalized_messages=correction_messages,
            sub_intent_id=sub_intent_id,
            attempt_number=1,
        )
    corrected_products = extract_product_candidates(
        normalized_messages=correction_messages,
        evidence_entries=combined_evidence,
    )
    corrected = _finalize_verified_response(
        original_reply=corrected_reply,
        products=corrected_products or initial.products,
        evidence_entries=combined_evidence,
        customer_message=customer_message,
    )
    return _choose_better_response(initial, corrected)


async def _run_single_intent_body(app, history: list[dict], sub_intent: str, debug: bool, conversation_context: dict | None = None):
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

    sub_intent_started = time.monotonic()
    correction_deadline = sub_intent_started + settings.SUB_INTENT_TIMEOUT_SECONDS
    sub_intent_id = make_sub_intent_id()
    start_evidence_context(sub_intent_id, attempt_number=0)
    try:
        reset_guard(max_total_calls=10, max_identical_calls=1)

        # Track how many messages existed BEFORE this turn, so we only ever
        # scan messages genuinely produced by THIS turn for products/replies —
        # never the full accumulated session history. Without this, product
        # recommendations from earlier questions in the same session (e.g.
        # dresses asked about earlier) would keep reappearing alongside
        # unrelated later answers (e.g. shoes).
        messages_before = len(history)
        history.append({"role": "user", "content": sub_intent})
        direct_specialist = getattr(app, "_scout_direct_specialist", None)
        structured_intent = getattr(app, "_scout_structured_intent", None)
        if direct_specialist:
            set_inventory_argument_constraints(
                _inventory_constraints_from_structured_intent(structured_intent)
            )
            if await _run_tool_first_if_possible(
                structured_intent=structured_intent,
                sub_intent=sub_intent,
                agent_name=direct_specialist,
            ):
                new_messages = []
                had_external_handoff = direct_specialist == "external_offer_agent"
            else:
                with timed_stage("direct_specialist_invocation", selected_specialist=direct_specialist):
                    new_messages, had_external_handoff = await _run_direct_specialist_turn(
                        app, history, messages_before, sub_intent, direct_specialist
                    )
        else:
            diagnostics = get_diagnostics()
            if diagnostics is not None:
                diagnostics.record("direct_routing", 0, direct_route_used=False, supervisor_invoked=True)
            graph_input = {"messages": _trim_history_for_model(history)}
            with timed_stage("supervisor_graph_invocation"):
                result = await _invoke_graph_with_timing(app, graph_input, config={"recursion_limit": 15})
            if result is None:
                return "Something went wrong processing that — could you try again?", []
            normalized = normalize_agent_result(
                result=result,
                input_message_count=messages_before,
                execution_mode="supervisor",
            )
            new_messages = normalized.messages
            had_external_handoff = normalized.routing_marker_present

        if debug:
            print(f"\n--- FULL MESSAGE TRACE (sub-intent: {sub_intent!r}) ---")
            for i, msg in enumerate(new_messages):
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
        _evidence_entries = get_evidence_entries()
        if not _evidence_entries:
            _evidence_entries = evidence_entries_from_tool_messages(
                normalized_messages=new_messages,
                sub_intent_id=sub_intent_id,
                attempt_number=0,
            )
        all_products = extract_product_candidates(
            normalized_messages=new_messages,
            evidence_entries=_evidence_entries,
        )

        # If a NEEDS_EXTERNAL_CHECK hand-off happened this turn, the internal
        # products from recommend_agent's abandoned attempt are stale — the
        # final reply is about the external results only, so only THOSE
        # should be shown. Without this, both the discarded internal
        # near-misses and the real external results get mixed into one array.
        if had_external_handoff:
            products = [p for p in all_products if p.get("source") == "external"]
        else:
            products = all_products

        finalized = _finalize_verified_response(
            original_reply=reply_text,
            products=products,
            evidence_entries=_evidence_entries,
            customer_message=sub_intent,
        )
        finalized = await _attempt_targeted_correction(
            app,
            initial=finalized,
            customer_message=sub_intent,
            correction_attempted=False,
            sub_intent_id=sub_intent_id,
            correction_deadline=correction_deadline,
        )
        _tool_call_records = get_tool_call_records()
        _update_context_from_verified_turn(
            conversation_context,
            finalized=finalized,
            structured_intent=structured_intent,
        )
        history.append({"role": "assistant", "content": finalized.reply})
        return finalized.reply, finalized.products
    except ProviderWorkflowError:
        history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
        return SAFE_TIMEOUT_REPLY, []
    finally:
        clear_inventory_argument_constraints()
        clear_evidence_context()


async def _run_single_intent(app, history: list[dict], sub_intent: str, debug: bool, conversation_context: dict | None = None):
    with timed_stage("complete_sub_intent"):
        try:
            return await _wait_for(
                _run_single_intent_body(app, history, sub_intent, debug, conversation_context),
                settings.SUB_INTENT_TIMEOUT_SECONDS,
                "sub-intent",
            )
        except TimeoutError:
            history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
            return SAFE_TIMEOUT_REPLY, []
        except ProviderWorkflowError:
            history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
            return SAFE_TIMEOUT_REPLY, []


def _new_multi_intent_state(plan: ExecutionPlan) -> MultiIntentExecutionState:
    return MultiIntentExecutionState(
        multi_intent=True,
        execution_plan=plan,
        selected_product_ids=[],
        selected_products=[],
        comparison_candidates=[],
        ranking_criteria=[],
        selected_best_product_id=None,
        requested_stores=[plan.requested_store] if plan.requested_store else [],
        budget_max=plan.budget_max,
        requested_size=plan.requested_size,
        requested_color=plan.requested_color,
        requested_store=plan.requested_store,
        completed_subgoals=[],
        pending_subgoals=[subgoal.intent for subgoal in plan.subgoals],
        evidence_by_specialist={},
    )


def _record_multi_intent_supervisor_path(plan: ExecutionPlan) -> None:
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "supervisor_graph_invocation",
            0,
            supervisor_graph_entered=True,
            supervisor_model_called=False,
            deterministic_plan_used=True,
            planned_subgoals=[subgoal.intent for subgoal in plan.subgoals],
        )


def _record_multi_subgoal(state: MultiIntentExecutionState, subgoal: PlannedSubIntent, completed: bool) -> None:
    if completed and subgoal.intent not in state.completed_subgoals:
        state.completed_subgoals.append(subgoal.intent)
    state.pending_subgoals = [intent for intent in state.pending_subgoals if intent != subgoal.intent] if completed else state.pending_subgoals
    diagnostics = get_diagnostics()
    if diagnostics is not None:
        diagnostics.record(
            "multi_intent_subgoal",
            0,
            current_subgoal=subgoal.intent,
            next_subgoal=state.next_subgoal,
            completed=completed,
            completed_subgoals=list(state.completed_subgoals),
            pending_subgoals=list(state.pending_subgoals),
            selected_product_ids=list(state.selected_product_ids),
        )


async def _run_multi_intent_plan(
    app,
    history: list[dict],
    message: str,
    plan: ExecutionPlan,
    debug: bool = False,
    conversation_context: dict | None = None,
) -> tuple[str, list[dict]]:
    _record_multi_intent_supervisor_path(plan)
    state = _new_multi_intent_state(plan)
    reused_context_products = _seed_comparison_products_from_context(state, message, conversation_context)
    history.append({"role": "user", "content": message})
    finalized_results: list[FinalizedResponse] = []
    all_products: list[dict] = []

    for index, subgoal in enumerate(plan.subgoals):
        state.current_subgoal = subgoal.intent
        state.next_subgoal = plan.subgoals[index + 1].intent if index + 1 < len(plan.subgoals) else None
        if reused_context_products and subgoal.intent == "product_recommendation":
            _record_multi_subgoal(state, subgoal, completed=True)
            continue
        if subgoal.conditional == "internal_insufficient" and _internal_products_satisfy_explicit_request(
            state.selected_products,
            StructuredIntent(
                text=message,
                request_type="product_recommendation",
                confidence=0.9,
                product_type=plan.product_type,
                budget_max=plan.budget_max,
                color=plan.requested_color,
            ),
        ):
            _record_multi_subgoal(state, subgoal, completed=False)
            continue

        record_selected_specialist(subgoal.specialist)
        reset_guard(max_total_calls=10, max_identical_calls=1)
        sub_intent_id = make_sub_intent_id()
        start_evidence_context(sub_intent_id, attempt_number=0)
        try:
            finalized = await _execute_multi_subgoal(state, subgoal, message)
            evidence_entries = get_evidence_entries()
            state.evidence_by_specialist.setdefault(subgoal.specialist, []).extend(evidence_entries)
            completed = finalized.reply != SAFE_TIMEOUT_REPLY and (
                bool(finalized.verification_result.approved_claim_ids) or bool(finalized.products)
            )
            if subgoal.intent == "policy_question" and "third-party retailer return-policy evidence" in finalized.reply:
                completed = True
            if subgoal.intent == "product_recommendation":
                _store_selected_products(state, finalized.products)
                if _has_pending_external_fallback(plan) and not _internal_products_satisfy_explicit_request(
                    state.selected_products,
                    StructuredIntent(
                        text=message,
                        request_type="product_recommendation",
                        confidence=0.9,
                        product_type=plan.product_type,
                        budget_max=plan.budget_max,
                        color=plan.requested_color,
                    ),
                ):
                    _record_multi_subgoal(state, subgoal, completed=True)
                    continue
            if completed:
                if subgoal.intent == "product_recommendation":
                    _update_context_from_verified_turn(
                        conversation_context,
                        finalized=finalized,
                        structured_intent=StructuredIntent(
                            text=subgoal.text,
                            request_type="product_recommendation",
                            confidence=0.9,
                            product_type=plan.product_type,
                            budget_max=plan.budget_max,
                            color=plan.requested_color,
                        ),
                    )
                finalized_results.append(finalized)
                all_products.extend(finalized.products)
            else:
                finalized_results.append(
                    FinalizedResponse(
                        reply=_subgoal_limitation(subgoal),
                        products=[],
                        proposed_claims=[],
                        verification_result=finalized.verification_result,
                    )
                )
            _record_multi_subgoal(state, subgoal, completed=completed)
        except ProviderWorkflowError:
            finalized_results.append(
                FinalizedResponse(
                    reply=_subgoal_limitation(subgoal),
                    products=[],
                    proposed_claims=[],
                    verification_result=type("Verification", (), {"approved_claim_ids": []})(),
                )
            )
            _record_multi_subgoal(state, subgoal, completed=False)
        finally:
            clear_inventory_argument_constraints()
            clear_evidence_context()

    reply = merge_answers(get_chat_model(), message, [result.reply for result in finalized_results if result.reply])
    if not reply:
        reply = SAFE_TIMEOUT_REPLY
    history.append({"role": "assistant", "content": reply})
    return reply, _dedupe_products_for_multi_intent(all_products)


async def _execute_multi_subgoal(
    state: MultiIntentExecutionState,
    subgoal: PlannedSubIntent,
    original_message: str,
) -> FinalizedResponse:
    if subgoal.intent == "product_recommendation":
        args = {"query": subgoal.text, "top_n": 3}
        if state.budget_max is not None:
            args["max_price"] = state.budget_max
        await _execute_read_only_tool("recommend_products", args, agent_name="recommend_agent")
        return _finalize_current_evidence(subgoal, original_message)

    if subgoal.intent == "product_comparison":
        return _finalize_product_comparison(state, subgoal)

    if subgoal.intent in {"inventory_availability", "store_availability"}:
        if not state.selected_product_ids:
            return _empty_subgoal_response(subgoal, original_message)
        for product_id in state.selected_product_ids:
            if state.requested_size:
                args = {"product_id": product_id, "size": state.requested_size}
                if state.requested_color:
                    args["color"] = state.requested_color
                await _execute_read_only_tool("stock", args, agent_name="inventory_agent")
            if state.requested_store or subgoal.intent == "store_availability":
                args = {"product_id": product_id}
                if state.requested_store:
                    args["store_name"] = state.requested_store
                await _execute_read_only_tool("stores", args, agent_name="inventory_agent")
        return _finalize_current_evidence(
            subgoal,
            _inventory_context_message(subgoal.text or original_message, state.selected_products),
            extra_evidence=state.evidence_by_specialist.get("recommend_agent", []),
            products=state.selected_products,
        )

    if subgoal.intent == "policy_question":
        if "third-party" in subgoal.text.lower():
            return FinalizedResponse(
                reply="I do not have verified third-party retailer return-policy evidence for those outside offers.",
                products=[],
                proposed_claims=[],
                verification_result=type("Verification", (), {"approved_claim_ids": []})(),
            )
        await _execute_read_only_tool("retrieve_policy_chunks", {"query": subgoal.text, "k": 3}, agent_name="policy_agent")
        return _finalize_current_evidence(subgoal, original_message)

    if subgoal.intent == "external_offer":
        args = _external_offer_args(
            state.execution_plan.subgoals[0].text,
            category=plan_category(state),
            budget_max=state.budget_max,
            color=state.requested_color,
        )
        await _execute_read_only_tool("search_external_offers", args, agent_name="external_offer_agent")
        return _finalize_current_evidence(subgoal, original_message)

    if subgoal.intent == "order_status":
        order_id = _extract_order_id_from_text(subgoal.text)
        if order_id:
            await _execute_read_only_tool("orders", {"order_id": order_id}, agent_name="order_agent")
        return _finalize_current_evidence(subgoal, original_message)

    if subgoal.intent == "return_eligibility":
        order_id = _extract_order_id_from_text(subgoal.text)
        if order_id:
            await _execute_read_only_tool("return_eligibility", {"order_id": order_id}, agent_name="order_agent")
        return _finalize_current_evidence(subgoal, original_message)

    return _empty_subgoal_response(subgoal, original_message)


def _finalize_current_evidence(
    subgoal: PlannedSubIntent,
    original_message: str,
    *,
    extra_evidence: list | None = None,
    products: list | None = None,
) -> FinalizedResponse:
    evidence_entries = [*(extra_evidence or []), *get_evidence_entries()]
    has_product_override = products is not None
    if subgoal.intent == "external_offer" and _external_no_match(evidence_entries):
        return FinalizedResponse(
            reply="Scout did not find an internal or verified third-party product matching all requested external-offer constraints.",
            products=[],
            proposed_claims=[],
            verification_result=type("Verification", (), {"approved_claim_ids": []})(),
        )
    products = products if products is not None else extract_product_candidates(normalized_messages=[], evidence_entries=evidence_entries)
    if subgoal.intent == "external_offer":
        products = [product for product in products if product.get("source") == "external"]
    elif subgoal.intent not in {"product_recommendation", "inventory_availability", "store_availability"}:
        products = []
    return _finalize_verified_response(
        original_reply="",
        products=products,
        evidence_entries=evidence_entries,
        customer_message=original_message if has_product_override else (subgoal.text or original_message),
    )


def _external_no_match(evidence_entries: list) -> bool:
    for entry in evidence_entries:
        if getattr(entry, "tool_name", None) != "search_external_offers" or not getattr(entry, "success", False):
            continue
        facts = getattr(entry, "normalized_facts", {})
        if isinstance(facts, dict) and facts.get("match_count") == 0 and facts.get("message"):
            return True
    return False


def _empty_subgoal_response(subgoal: PlannedSubIntent, original_message: str) -> FinalizedResponse:
    return _finalize_verified_response(
        original_reply="",
        products=[],
        evidence_entries=get_evidence_entries(),
        customer_message=subgoal.text or original_message,
    )


def _store_selected_products(state: MultiIntentExecutionState, products: list[dict]) -> None:
    state.selected_products = [product for product in products if isinstance(product, dict) and product.get("product_id")]
    state.selected_product_ids = [product["product_id"] for product in state.selected_products]


def _inventory_context_message(message: str, products: list[dict]) -> str:
    hints = [
        f"product_id {product['product_id']} ({product['name']})"
        for product in products
        if isinstance(product, dict)
        and isinstance(product.get("product_id"), str)
        and isinstance(product.get("name"), str)
    ]
    if not hints:
        return message
    return f"Check pickup availability for {', '.join(hints)}. {message}"


def _seed_comparison_products_from_context(
    state: MultiIntentExecutionState,
    message: str,
    conversation_context: dict | None,
) -> bool:
    if not _looks_like_contextual_comparison(message):
        return False
    if not any(subgoal.intent == "product_comparison" for subgoal in state.execution_plan.subgoals):
        return False
    context = _safe_conversation_context(conversation_context)
    if context is None:
        return False
    products = [
        product
        for product in context.get("active_selected_products") or []
        if isinstance(product, dict) and product.get("product_id")
    ]
    if len(products) < 2:
        return False
    _store_selected_products(state, products)
    return True


def _looks_like_contextual_comparison(message: str) -> bool:
    normalized = (message or "").lower()
    context_terms = {"compare these", "compare them", "compare those", "these three", "these 3", "pick the best"}
    comparison_terms = {"compare", "pick", "best", "rank"}
    return bool(
        any(term in normalized for term in context_terms)
        and any(term in normalized for term in comparison_terms)
    )


def _finalize_product_comparison(state: MultiIntentExecutionState, subgoal: PlannedSubIntent) -> FinalizedResponse:
    state.comparison_candidates = list(state.selected_products)
    state.ranking_criteria = _comparison_ranking_criteria(state)
    if not state.comparison_candidates:
        return FinalizedResponse(
            reply=_subgoal_limitation(subgoal),
            products=[],
            proposed_claims=[],
            verification_result=type("Verification", (), {"approved_claim_ids": []})(),
        )

    best = max(state.comparison_candidates, key=lambda product: _comparison_score(product, state))
    state.selected_best_product_id = best["product_id"]
    state.selected_products = [best]
    state.selected_product_ids = [best["product_id"]]
    return FinalizedResponse(
        reply=_comparison_reply(best, state),
        products=[best],
        proposed_claims=[],
        verification_result=type("Verification", (), {"approved_claim_ids": []})(),
    )


def _comparison_ranking_criteria(state: MultiIntentExecutionState) -> list[str]:
    criteria = ["verified product match"]
    if state.budget_max is not None:
        criteria.append("budget fit")
    if any(product.get("rating") is not None for product in state.selected_products):
        criteria.append("rating")
    if any(isinstance(product.get("promotion"), dict) for product in state.selected_products):
        criteria.append("sale price")
    criteria.append("lower price")
    return criteria


def _comparison_score(product: dict, state: MultiIntentExecutionState) -> tuple:
    price = _safe_float(product.get("price"))
    sale_price = _safe_float(product.get("promotion", {}).get("discounted_price")) if isinstance(product.get("promotion"), dict) else None
    effective_price = sale_price if sale_price is not None else price
    rating = _safe_float(product.get("rating")) or 0
    budget_limit = state.budget_max
    budget_fit = 1 if budget_limit is None or (effective_price is not None and effective_price <= budget_limit) or (price is not None and price <= budget_limit) else 0
    discount = (price - sale_price) if price is not None and sale_price is not None and sale_price < price else 0
    return (
        budget_fit,
        rating,
        discount,
        -(effective_price if effective_price is not None else 10_000),
        str(product.get("name", "")),
    )


def _comparison_reply(product: dict, state: MultiIntentExecutionState) -> str:
    name = product.get("name") or product["product_id"]
    scope = f"the {len(state.comparison_candidates)}" if len(state.comparison_candidates) > 1 else "the"
    price = _safe_float(product.get("price"))
    promotion = product.get("promotion")
    sale_price = _safe_float(promotion.get("discounted_price")) if isinstance(promotion, dict) else None
    if sale_price is not None:
        sale_phrase = f" and its sale price is ${sale_price:.2f}"
    else:
        sale_phrase = ""
    rating = _safe_float(product.get("rating"))
    rating_phrase = ""
    if rating is not None:
        if _has_highest_rating(product, state.comparison_candidates):
            rating_phrase = f", has the highest rating at {rating:.1f}"
        else:
            rating_phrase = f", has a {rating:.1f} rating"
    if price is not None:
        budget_phrase = f" under ${state.budget_max:.0f}" if state.budget_max is not None and price <= state.budget_max else ""
        return f"Of {scope}, I’d pick {name}. It’s{budget_phrase} at ${price:.2f}{rating_phrase}{sale_phrase}."
    basis = ", ".join(state.ranking_criteria)
    suffix = f" based on {basis}" if basis else ""
    return f"Of {scope}, I’d pick {name}{suffix}."


def _has_highest_rating(product: dict, candidates: list[dict]) -> bool:
    rating = _safe_float(product.get("rating"))
    candidate_ratings = [_safe_float(candidate.get("rating")) for candidate in candidates]
    numeric_ratings = [value for value in candidate_ratings if value is not None]
    return bool(numeric_ratings) and rating is not None and rating >= max(numeric_ratings)


def _safe_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _has_pending_external_fallback(plan: ExecutionPlan) -> bool:
    return any(subgoal.intent == "external_offer" and subgoal.conditional == "internal_insufficient" for subgoal in plan.subgoals)


def _subgoal_limitation(subgoal: PlannedSubIntent) -> str:
    labels = {
        "product_recommendation": "I couldn’t verify product recommendations for that part of the request.",
        "inventory_availability": "I couldn’t verify the requested inventory availability.",
        "store_availability": "I couldn’t verify the requested store availability.",
        "product_comparison": "I couldn’t compare verified products for that part of the request.",
        "policy_question": "I couldn’t verify the requested policy detail.",
        "order_status": "I couldn’t verify the requested order status.",
        "return_eligibility": "I couldn’t verify return eligibility for that order.",
        "external_offer": "I couldn’t verify a third-party offer for that part of the request.",
    }
    return labels.get(subgoal.intent, "I couldn’t verify that part of the request.")


def _dedupe_products_for_multi_intent(products: list[dict]) -> list[dict]:
    by_id = {}
    for product in products:
        key = product.get("product_id") or product.get("external_product_id")
        if key and key not in by_id:
            by_id[key] = product
    return list(by_id.values())


def _extract_order_id_from_text(text: str) -> str | None:
    match = re.search(r"\b(?:ORD[-\s]?\d{3,}|O\d{3,}|\d{3,})\b", text or "", re.IGNORECASE)
    return match.group(0).upper().replace(" ", "-") if match else None


def plan_category(state: MultiIntentExecutionState) -> str:
    category = state.execution_plan.product_type or "products"
    if category == "dress":
        return "dresses"
    if category == "shoe":
        return "shoes"
    return category


def _external_offer_args(
    query: str,
    *,
    category: str,
    budget_max: float | None = None,
    color: str | None = None,
) -> dict:
    lowered = (query or "").lower()
    args = {"query": query, "category": category}
    if budget_max is not None:
        args["budget_max"] = budget_max
    if color:
        args["color"] = color
    if "hiking" in lowered:
        args["use_case"] = "hiking"
    if "waterproof" in lowered:
        args["waterproof"] = True
        args["required_attributes"] = "waterproof"
    return args


async def _ask_body(app, history: list[dict], message: str, debug: bool = False, conversation_context: dict | None = None):
    """Splits the customer message into sub-intents (usually just one),
    runs each through the existing supervisor/specialist graph separately,
    then merges the answers into one natural reply. Products from ALL
    sub-intents are combined and returned. Returns
    (reply_text, updated_history, products)."""
    if not message or not message.strip():
        return "Please type a message.", history, []

    contextual_result = _contextual_split_result(message, conversation_context)
    splitter_model = get_chat_model()
    if contextual_result is not None:
        split_result = contextual_result
    else:
        with timed_stage("intent_splitting"):
            split_result = await _wait_for(
                asyncio.to_thread(_split_intents_for_supervisor, splitter_model, message),
                settings.MODEL_INVOCATION_TIMEOUT_SECONDS,
                "intent splitting",
            )
    sub_intents = split_result.sub_intents

    if split_result.execution_plan is not None and split_result.execution_plan.multi_intent:
        with timed_stage("multi_intent_supervisor_execution"):
            reply, products = await _run_multi_intent_plan(
                app,
                history,
                message,
                split_result.execution_plan,
                debug,
                conversation_context,
            )
        return reply, history, products

    direct_reply = _deterministic_conversational_reply(split_result.structured_intent, sub_intents)
    if direct_reply is not None:
        diagnostics = get_diagnostics()
        if diagnostics is not None and split_result.structured_intent is not None:
            if split_result.structured_intent.request_type == "purchase_execution":
                diagnostics.record("deterministic_purchase_boundary", 0, deterministic_purchase_boundary_used=True)
            if split_result.structured_intent.request_type == "shopping_clarification":
                diagnostics.record("deterministic_clarification", 0, deterministic_clarification_used=True)
        _update_context_for_clarification(conversation_context, split_result.structured_intent)
        history.append({"role": "user", "content": sub_intents[0]})
        history.append({"role": "assistant", "content": direct_reply})
        return direct_reply, history, []

    direct_specialist = _direct_route_agent(app, split_result.structured_intent, sub_intents)

    if debug and len(sub_intents) > 1:
        print(f"\n[Intent splitter] Detected {len(sub_intents)} sub-intents: {sub_intents}\n")

    all_products: list[dict] = []
    sub_answers: list[str] = []

    for sub_intent in sub_intents:
        if direct_specialist:
            setattr(app, "_scout_direct_specialist", direct_specialist)
            setattr(app, "_scout_structured_intent", split_result.structured_intent)
        try:
            if conversation_context is None:
                reply_text, products = await _run_single_intent(app, history, sub_intent, debug)
            else:
                reply_text, products = await _run_single_intent(app, history, sub_intent, debug, conversation_context)
        finally:
            if direct_specialist and hasattr(app, "_scout_direct_specialist"):
                delattr(app, "_scout_direct_specialist")
            if direct_specialist and hasattr(app, "_scout_structured_intent"):
                delattr(app, "_scout_structured_intent")
        all_products.extend(products)
        sub_answers.append(reply_text)

    final_reply = merge_answers(splitter_model, message, sub_answers)

    return final_reply, history, all_products


async def ask(app, history: list[dict], message: str, debug: bool = False, conversation_context: dict | None = None):
    start_diagnostics()
    try:
        with timed_stage("complete_chat_request"):
            return await _wait_for(
                _ask_body(app, history, message, debug, conversation_context),
                settings.CHAT_REQUEST_TIMEOUT_SECONDS,
                "chat request",
            )
    except TimeoutError:
        history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
        return SAFE_TIMEOUT_REPLY, history, []
    finally:
        log_request_complete()
        clear_evidence_context()
        clear_diagnostics()


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


async def _run_single_intent_streaming_body(
    app,
    history: list[dict],
    sub_intent: str,
    debug: bool = False,
    conversation_context: dict | None = None,
):
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

    sub_intent_started = time.monotonic()
    correction_deadline = sub_intent_started + settings.SUB_INTENT_TIMEOUT_SECONDS
    sub_intent_id = make_sub_intent_id()
    start_evidence_context(sub_intent_id, attempt_number=0)
    try:
        reset_guard(max_total_calls=10, max_identical_calls=1)

        # Track how many messages existed BEFORE this turn so we only ever
        # scan NEW messages for products/replies — never the full session
        # history. Without this, recommendations from earlier questions in
        # the same session keep reappearing alongside later, unrelated answers.
        messages_before = len(history)
        history.append({"role": "user", "content": sub_intent})

        yield ("progress", "understanding_request")

        direct_specialist = getattr(app, "_scout_direct_specialist", None)
        structured_intent = getattr(app, "_scout_structured_intent", None)
        if direct_specialist:
            set_inventory_argument_constraints(
                _inventory_constraints_from_structured_intent(structured_intent)
            )
            if direct_specialist in ("recommend_agent", "external_offer_agent"):
                yield ("progress", "searching_products")
            elif direct_specialist == "inventory_agent":
                yield ("progress", "checking_inventory")
            if await _run_tool_first_if_possible(
                structured_intent=structured_intent,
                sub_intent=sub_intent,
                agent_name=direct_specialist,
            ):
                new_messages = []
                had_external_handoff = direct_specialist == "external_offer_agent"
            else:
                with timed_stage("direct_specialist_invocation", selected_specialist=direct_specialist):
                    new_messages, had_external_handoff = await _run_direct_specialist_turn(
                        app, history, messages_before, sub_intent, direct_specialist
                    )
        else:
            diagnostics = get_diagnostics()
            if diagnostics is not None:
                diagnostics.record("direct_routing", 0, direct_route_used=False, supervisor_invoked=True)
            final_result = None
            seen_nodes: set[str] = set()
            model_starts: dict[str, tuple[float, int, int, str]] = {}

            async for event in app.astream_events(
                {"messages": _trim_history_for_model(history)}, version="v2", config={"recursion_limit": 15}
            ):
                node_name = event.get("metadata", {}).get("langgraph_node", "")
                ev_type = event.get("event")
                run_id = str(event.get("run_id", ""))

                if ev_type == "on_chain_start" and node_name and node_name not in seen_nodes:
                    seen_nodes.add(node_name)
                    if node_name in SPECIALIST_NAMES:
                        record_selected_specialist(node_name)
                    if node_name in ("recommend_agent", "external_offer_agent"):
                        yield ("progress", "searching_products")
                    elif node_name == "inventory_agent":
                        yield ("progress", "checking_inventory")

                if ev_type == "on_chat_model_start":
                    messages = event.get("data", {}).get("input", {}).get("messages", [])
                    count, chars = input_size(messages if isinstance(messages, list) else [])
                    model_starts[run_id] = (time.perf_counter(), count, chars, node_name or "unknown")

                if ev_type == "on_chat_model_end" and run_id in model_starts:
                    started, count, chars, node = model_starts.pop(run_id)
                    stage = "specialist_model_invocation" if node in SPECIALIST_NAMES else "supervisor_model_invocation"
                    record_model_call(
                        stage=stage,
                        elapsed_ms=(time.perf_counter() - started) * 1000,
                        message_count=count,
                        input_chars=chars,
                    )

                if ev_type == "on_chain_end":
                    output = event.get("data", {}).get("output")
                    if event.get("name") == "LangGraph" or _result_contains_messages(output):
                        final_result = output

            if final_result is None:
                yield ("result", ("Something went wrong processing that — could you try again?", []))
                return

            normalized = normalize_agent_result(
                result=final_result,
                input_message_count=messages_before,
                execution_mode="supervisor",
            )
            new_messages = normalized.messages
            had_external_handoff = normalized.routing_marker_present
        reply_text = get_final_reply(new_messages)
        _evidence_entries = get_evidence_entries()
        if not _evidence_entries:
            _evidence_entries = evidence_entries_from_tool_messages(
                normalized_messages=new_messages,
                sub_intent_id=sub_intent_id,
                attempt_number=0,
            )
        all_products = extract_product_candidates(
            normalized_messages=new_messages,
            evidence_entries=_evidence_entries,
        )

        products = (
            [p for p in all_products if p.get("source") == "external"]
            if had_external_handoff
            else all_products
        )

        yield ("progress", "verifying_claims")

        yield ("progress", "preparing_response")

        finalized = _finalize_verified_response(
            original_reply=reply_text,
            products=products,
            evidence_entries=_evidence_entries,
            customer_message=sub_intent,
        )
        finalized = await _attempt_targeted_correction(
            app,
            initial=finalized,
            customer_message=sub_intent,
            correction_attempted=False,
            sub_intent_id=sub_intent_id,
            correction_deadline=correction_deadline,
        )
        _tool_call_records = get_tool_call_records()
        _update_context_from_verified_turn(
            conversation_context,
            finalized=finalized,
            structured_intent=structured_intent,
        )
        history.append({"role": "assistant", "content": finalized.reply})
        yield ("result", (finalized.reply, finalized.products))
    finally:
        clear_inventory_argument_constraints()
        clear_evidence_context()


async def _run_single_intent_streaming(
    app,
    history: list[dict],
    sub_intent: str,
    debug: bool = False,
    conversation_context: dict | None = None,
):
    try:
        async with asyncio.timeout(settings.SUB_INTENT_TIMEOUT_SECONDS):
            with timed_stage("complete_sub_intent"):
                async for item in _run_single_intent_streaming_body(app, history, sub_intent, debug, conversation_context):
                    yield item
    except TimeoutError:
        mark_timeout()
        history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
        yield ("result", (SAFE_TIMEOUT_REPLY, []))
    except ProviderWorkflowError:
        history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
        yield ("result", (SAFE_TIMEOUT_REPLY, []))


async def ask_streaming(app, history: list[dict], message: str, debug: bool = False, conversation_context: dict | None = None):
    """Streaming-aware sibling of ask(). Yields ("progress", label) tuples
    as real steps occur across all sub-intents, then a final
    ("result", (final_reply, all_products)) tuple once everything has been
    processed and merged — same verification-gated pipeline as ask(),
    with progress visibility for the frontend.
    """
    start_diagnostics()
    try:
        async with asyncio.timeout(settings.CHAT_REQUEST_TIMEOUT_SECONDS):
            if not message or not message.strip():
                yield ("result", ("Please type a message.", []))
                return

            contextual_result = _contextual_split_result(message, conversation_context)
            splitter_model = get_chat_model()
            if contextual_result is not None:
                split_result = contextual_result
            else:
                with timed_stage("intent_splitting"):
                    split_result = await _wait_for(
                        asyncio.to_thread(_split_intents_for_supervisor, splitter_model, message),
                        settings.MODEL_INVOCATION_TIMEOUT_SECONDS,
                        "intent splitting",
                    )
            sub_intents = split_result.sub_intents

            if split_result.execution_plan is not None and split_result.execution_plan.multi_intent:
                yield ("progress", "understanding_request")
                with timed_stage("multi_intent_supervisor_execution"):
                    reply, products = await _run_multi_intent_plan(
                        app,
                        history,
                        message,
                        split_result.execution_plan,
                        debug,
                        conversation_context,
                    )
                yield ("result", (reply, products))
                return

            direct_reply = _deterministic_conversational_reply(split_result.structured_intent, sub_intents)
            if direct_reply is not None:
                diagnostics = get_diagnostics()
                if diagnostics is not None and split_result.structured_intent is not None:
                    if split_result.structured_intent.request_type == "purchase_execution":
                        diagnostics.record("deterministic_purchase_boundary", 0, deterministic_purchase_boundary_used=True)
                    if split_result.structured_intent.request_type == "shopping_clarification":
                        diagnostics.record("deterministic_clarification", 0, deterministic_clarification_used=True)
                _update_context_for_clarification(conversation_context, split_result.structured_intent)
                history.append({"role": "user", "content": sub_intents[0]})
                history.append({"role": "assistant", "content": direct_reply})
                yield ("result", (direct_reply, []))
                return

            direct_specialist = _direct_route_agent(app, split_result.structured_intent, sub_intents)

            all_products: list[dict] = []
            sub_answers: list[str] = []

            for sub_intent in sub_intents:
                if direct_specialist:
                    setattr(app, "_scout_direct_specialist", direct_specialist)
                    setattr(app, "_scout_structured_intent", split_result.structured_intent)
                try:
                    if conversation_context is None:
                        stream_items = _run_single_intent_streaming(app, history, sub_intent, debug)
                    else:
                        stream_items = _run_single_intent_streaming(app, history, sub_intent, debug, conversation_context)
                    async for kind, payload in stream_items:
                        if kind == "progress":
                            yield ("progress", payload)
                        else:
                            reply_text, products = payload
                            all_products.extend(products)
                            sub_answers.append(reply_text)
                finally:
                    if direct_specialist and hasattr(app, "_scout_direct_specialist"):
                        delattr(app, "_scout_direct_specialist")
                    if direct_specialist and hasattr(app, "_scout_structured_intent"):
                        delattr(app, "_scout_structured_intent")

            final_reply = merge_answers(splitter_model, message, sub_answers)
            yield ("result", (final_reply, all_products))
    except TimeoutError:
        mark_timeout()
        history.append({"role": "assistant", "content": SAFE_TIMEOUT_REPLY})
        yield ("result", (SAFE_TIMEOUT_REPLY, []))
    finally:
        log_request_complete()
        clear_evidence_context()
        clear_diagnostics()
