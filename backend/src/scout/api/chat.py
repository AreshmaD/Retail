import json
import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from scout.agents.supervisor import ask, ask_streaming, SPECIALIST_NAMES

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
    """Streams real progress events (understanding_request,
    searching_products, checking_inventory, verifying_claims,
    preparing_response) as the request genuinely moves through the
    supervisor/specialist graph, then emits the final, verification-gated
    reply in one 'done' event. No partial/unverified answer text is ever
    streamed — only real progress signals plus the finished, checked
    answer, matching the same verification pipeline the non-streaming
    /chat endpoint uses via ask()."""
    session_id = body.session_id or str(uuid.uuid4())
    history = SESSION_HISTORIES.get(session_id, [])

    supervisor_app = request.app.state.supervisor_app

    async def event_generator():
        yield {"event": "session", "data": session_id}

        try:
            async for kind, payload in ask_streaming(supervisor_app, history, body.message):
                if kind == "progress":
                    yield {"event": "progress", "data": payload}
                else:
                    reply_text, products = payload
                    SESSION_HISTORIES[session_id] = history
                    yield {
                        "event": "done",
                        "data": json.dumps({"reply": reply_text, "products": products}),
                    }
        except Exception as e:
            yield {"event": "error", "data": str(e)}
            return

    return EventSourceResponse(event_generator())
