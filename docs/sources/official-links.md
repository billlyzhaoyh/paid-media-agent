# Official engineering sources

Check the relevant primary source before changing an external contract. Update the owning
architecture page when the supported behavior changes.

## Models and state

- [OpenAI Chat Completions function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Anthropic OpenAI SDK compatibility](https://docs.anthropic.com/en/api/openai-sdk)
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [OpenRouter reasoning tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
- [OpenRouter zero data retention](https://openrouter.ai/docs/guides/features/zdr)
- [MCP Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [DuckDB concurrency](https://duckdb.org/docs/stable/connect/concurrency)

The loop sends every provider the Chat Completions shape and replays `reasoning_details` unchanged
after tool results, which Gemini and Claude require to continue reasoning. Check a provider's
compatibility page before relying on a feature beyond tool calling and text.

## Pipeboard

- [Pipeboard integrations](https://pipeboard.co/integrations)
- [Pipeboard MCP product](https://pipeboard.co/products/mcps)
- [Pipeboard Google integration and permissions](https://pipeboard.co/google-mcp)
- [Pipeboard API tokens](https://pipeboard.co/api-tokens)
- [Pipeboard connections](https://pipeboard.co/connections)
- [TikTok Ads MCP](https://pipeboard.co/guides/tiktok-ads-mcp)
- [Pinterest Ads MCP](https://pipeboard.co/guides/pinterest-ads-mcp)
- [Snap Ads MCP](https://pipeboard.co/guides/snap-ads-mcp)
- [Google Analytics MCP](https://pipeboard.co/guides/google-analytics-mcp)
- [LinkedIn Ads MCP](https://pipeboard.co/guides/linkedin-ads-mcp)

Pipeboard's public pages establish its MCP and OAuth product surface. Only the authenticated live
catalog establishes the exact tools and annotations available to a configured user.

## Slack and UI

- [Slack Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/)
- [Slack Bolt for Python Socket Mode](https://docs.slack.dev/tools/bolt-python/concepts/socket-mode/)
- [Slack Block Kit](https://docs.slack.dev/block-kit/)
- [Slack request verification](https://docs.slack.dev/authentication/verifying-requests-from-slack/)

Socket Mode removes the need for a public event URL but is not eligible for the public Slack
Marketplace. Signed HTTP events are the hosted alternative. Block Kit content must include an
accessible top-level message.
