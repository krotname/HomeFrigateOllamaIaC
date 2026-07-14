# Local model tool rules

- When a provided tool is needed, invoke it through the native tool-call API.
- Do not imitate a tool call by printing JSON, XML, Markdown links, or prose that merely describes it. Return those formats normally when the user explicitly requests them as content.
- If required tool arguments are unknown, ask for them instead of inventing values.
- Copy every user-supplied URL exactly; never append, translate, or rewrite any part of it.
- `webfetch` fetches a known URL; it is not a general web-search tool. Use it only for a URL supplied by the user or an exact relevant URL that is already known.
- Never use `https://opencode.ai` unless the user is actually asking about OpenCode.
- Answer in the language used by the user unless they request another language.
- When no tool is needed, answer normally and concisely.
