"""
An optional AI helper that drafts an answer to a student's doubt for a teacher to read, edit and send.

It needs ANTHROPIC_API_KEY in the environment. Without it the helper says so and everything else works as before. The AI never
writes to a student on its own: a teacher always sends the reply. Tests replace `complete` with a fake.
"""
import json
import os
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.getenv("DOUBTS_AI_MODEL", "claude-sonnet-5-5")
TIMEOUT = 40


class AIUnavailable(Exception):
    """The helper is not set up, or did not answer. The message can be shown to the teacher."""


def configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def prompt_for(doubt) -> str:
    options = "\n".join(f"- {o.get('text', '')}{'  (correct)' if o.get('is_correct') else ''}" for o in doubt.options or [])
    thread = "\n".join(f"{'Student' if r.role == 'student' else 'Teacher'}: {r.body}" for r in doubt.replies.all())
    return (
        "A student of an Indian competitive-exam preparation app is confused about this question. Write a short, kind reply in simple "
        "English followed by the same reply in Hindi. Explain WHY the correct option is right and, if the student chose another option, "
        "why that one is wrong. Use only facts you are sure of; if the question itself looks wrong, say so.\n\n"
        f"Question: {doubt.question_text}\nOptions:\n{options}\n"
        f"The student chose: {doubt.selected_option_text or 'nothing'}\nExplanation in the test: {doubt.explanation}\n\n"
        f"Conversation so far:\n{thread}"
    )


def complete(prompt: str) -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise AIUnavailable("The AI helper is not set up. Add ANTHROPIC_API_KEY on the server to use it.")
    body = json.dumps({"model": MODEL, "max_tokens": 900, "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - fixed https URL
            data = json.loads(resp.read().decode())
        return "".join(part.get("text", "") for part in data.get("content", [])).strip()
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise AIUnavailable("The AI helper did not answer. Try again, or write the reply yourself.") from exc


def suggest(doubt) -> str:
    text = complete(prompt_for(doubt))
    if not text:
        raise AIUnavailable("The AI helper gave an empty answer. Try again, or write the reply yourself.")
    return text
