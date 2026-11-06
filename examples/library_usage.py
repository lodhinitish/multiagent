"""Use the system as a library (no HTTP). Needs ANTHROPIC_API_KEY / OPENAI_API_KEY."""
import asyncio

import multiagent.builtin_tools  # noqa: F401  registers calculator, current_time, http_get
from multiagent import AgentConfig, AgentStore, MultiAgentSystem, tool


@tool
def word_count(text: str) -> int:
    """Count words in a piece of text."""
    return len(text.split())


async def main() -> None:
    system = MultiAgentSystem(store=AgentStore("agents.json"))

    system.add_agent("analyst", AgentConfig(
        description="Does arithmetic and text statistics",
        system_prompt="You are precise. Always use tools for numbers.",
        provider="anthropic", model="claude-sonnet-4-5",
        tools=["calculator", "word_count"],
    ), replace=True)

    system.add_agent("lead", AgentConfig(
        system_prompt="You coordinate. Delegate numeric work to the analyst, then answer concisely.",
        provider="openai", model="gpt-5.6-luna",
        tools=["current_time"], sub_agents=["analyst"],
    ), replace=True)

    result = await system.run("lead", "What time is it in Asia/Kolkata, and what is 17% of 2,340?")
    print(result.output)
    for e in result.trace:
        print(f"{'  ' * e.depth}[{e.agent}] {e.type}: {e.data.get('name', '')}")
    print(result.usage)


if __name__ == "__main__":
    asyncio.run(main())
