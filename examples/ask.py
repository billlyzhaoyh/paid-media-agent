"""Ask the agent one question with the configured model, locally, through the server's profile."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from paid_media_agent.config import Settings
from paid_media_agent.runtime.local import build_configured_runtime


async def main(question: str) -> None:
    settings = Settings()
    root = Path(__file__).resolve().parents[1]
    runtime = build_configured_runtime(settings, project_root=root)
    conversation = await runtime.agent.send("example", "local-user", question)
    print(conversation.messages[-1].content)
    print(f"\ncatalog={runtime.catalog.revision} model={runtime.components.metadata.model_spec}")


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or "Compare the last two weeks with the prior two weeks."
    asyncio.run(main(prompt))
