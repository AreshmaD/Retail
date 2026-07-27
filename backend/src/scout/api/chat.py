import json
import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from scout.agents.supervisor import ask, extract_products, extract_text, SPECIALIST_NAMES

router = APIRouter()

# In-memory session store: session_id -> message history.
# Dev-only simplification — resets on restart, doesn't scale across
# multiple server processes.
SESSION_HISTORIES: dict[str, list[dict]] = {}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    products: list[dict] = []


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    session_id = body.session_id or str(uuid.uuid4())
    history = SESSION_HISTORIES.get(session_id, [])

    supervisor_app = request.app.state.supervisor_app
    reply, updated_history, products = await ask(supervisor_app, history, body.message)

    SESSION_HISTORIES[session_id] = updated_history

    return ChatResponse(session_id=session_id, reply=reply, products=products)


@router.post("/chat/stream")
async def chat_stream(request: Request, body: ChatRequest):
    session_id = body.session_id or str(uuid.uuid4())
    history = SESSION_HISTORIES.get(session_id, [])
    history.append({"role": "user", "content": body.message})

    supervisor_app = request.app.state.supervisor_app

    async def event_generator():
        yield {"event": "session", "data": session_id}

        full_text_parts: list[str] = []
        final_result = None
        try:
            async for event in supervisor_app.astream_events(
                {"messages": history},
                version="v2",
                config={"recursion_limit": 10},
            ):
                if event.get("event") == "on_chain_end" and event.get("name") == "LangGraph":
                    final_result = event.get("data", {}).get("output")

                if event.get("event") != "on_chat_model_stream":
                    continue

                node_name = event.get("metadata", {}).get("langgraph_node", "")
                if node_name not in SPECIALIST_NAMES:
                    continue

                chunk = event["data"]["chunk"]
                text = extract_text(getattr(chunk, "content", ""))
                if text:
                    full_text_parts.append(text)
                    yield {"event": "token", "data": text}
        except Exception as e:
            yield {"event": "error", "data": str(e)}
            return

        full_reply = "".join(full_text_parts)
        history.append({"role": "assistant", "content": full_reply})
        SESSION_HISTORIES[session_id] = history

        products = []
        if final_result and "messages" in final_result:
            products = extract_products(final_result["messages"])

        yield {"event": "done", "data": json.dumps({"reply": full_reply, "products": products})}

    return EventSourceResponse(event_generator())
