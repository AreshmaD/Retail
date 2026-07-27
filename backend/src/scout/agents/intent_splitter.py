import json

from scout.agents.model_provider import get_chat_model, with_no_think

SPLIT_PROMPT = with_no_think("""...""")  # unchanged
MERGE_PROMPT_TEMPLATE = with_no_think("""...""")  # unchanged


def split_intents(model, message: str) -> list[str]:
    """Splits a message into sub-intents. Falls back to treating the
    whole message as one intent if the model call fails OUTRIGHT (API
    error, empty input) or if its output can't be parsed as a valid JSON
    list — never lets a splitter failure block the conversation."""
    if not message or not message.strip():
        return [message]

    try:
        response = model.invoke([
            {"role": "system", "content": SPLIT_PROMPT},
            {"role": "user", "content": message},
        ])
    except Exception:
        return [message]

    text = response.content if isinstance(response.content, str) else str(response.content)

    try:
        parsed = json.loads(text.strip())
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed) and parsed:
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    return [message]


def merge_answers(model, original_message: str, answers: list[str]) -> str:
    """Combines multiple specialist answers into one natural reply. If
    the merge call itself fails, falls back to simple concatenation
    rather than losing already-computed answers to a fragile final step."""
    if len(answers) == 1:
        return answers[0]

    answers_block = "\n\n".join(f"- {a}" for a in answers)
    prompt = MERGE_PROMPT_TEMPLATE.format(
        original_message=original_message or "", answers_block=answers_block
    )

    try:
        response = model.invoke([{"role": "user", "content": prompt}])
        return response.content if isinstance(response.content, str) else str(response.content)
    except Exception:
        return "\n\n".join(answers)