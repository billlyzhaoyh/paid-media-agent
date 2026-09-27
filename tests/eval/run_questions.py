"""Ask each question in a fresh thread on the sample fixtures; record tools, timing, and the answer.

Runs the agent in-process with the configured model (`PAID_MEDIA_MODEL` and its key).

Usage: run_questions.py <out.jsonl> [comma-separated question ids]
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from paid_media_agent.config import Settings, project_root
from paid_media_agent.harness.messages import AssistantMessage
from paid_media_agent.harness.models import resolve_model
from paid_media_agent.runtime.local import build_local_runtime

QUESTIONS = Path(__file__).with_name("questions.json")


async def main(out_path: str, only: set[str] | None) -> None:
    settings = Settings().model_copy(update={"paid_media_data_mode": "sample"})
    model = resolve_model(
        settings.model_settings(),
        api_key_env=settings.paid_media_model_api_key_env,
        timeout_seconds=settings.paid_media_model_timeout_seconds,
        zero_data_retention=settings.paid_media_model_zero_data_retention,
    )
    runtime = build_local_runtime(settings, project_root=project_root(), model=model)
    with open(out_path, "a", encoding="utf-8") as out:
        for question in json.loads(QUESTIONS.read_text()):
            if only and question["id"] not in only:
                continue
            started = time.time()
            record: dict[str, object] = {"id": question["id"], "question": question["text"]}
            try:
                conversation = await runtime.agent.send(
                    f"eval-{question['id']}", "eval", question["text"]
                )
                messages = conversation.messages
                record["tools"] = [
                    call.name
                    for m in messages
                    if isinstance(m, AssistantMessage)
                    for call in m.tool_calls
                ]
                record["answer"] = messages[-1].content if messages else ""
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            record["seconds"] = round(time.time() - started, 1)
            out.write(json.dumps(record) + "\n")
            out.flush()
            print(f"{question['id']:18} {record['seconds']:7.1f}s tools={record.get('tools')}")


if __name__ == "__main__":
    ids = set(sys.argv[2].split(",")) if len(sys.argv) > 2 else None
    asyncio.run(main(sys.argv[1], ids))
