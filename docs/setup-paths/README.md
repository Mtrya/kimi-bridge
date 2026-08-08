# Verified setup paths

These paths supplement the stable setup contract in [INSTALL_AI.md](../../INSTALL_AI.md) with dated, live-tested procedures. They are operational evidence, not substitutes for the contract, current official documentation, or observed platform behavior.

## Available paths

- [QQ C2C over WebSocket](qq.md)
- [WeChat iLink private chat](wechat.md)

## Freshness and use

Every path must state:

- what platform mode and preconditions it covers;
- the date of its last complete live verification;
- a `reverify_after` date 90 days later;
- the public evidence from which it was documented.

Use a path directly only when its preconditions match and the current date is not later than `reverify_after`. If the path is stale, first research current official sources and confirm the platform-side actions before guiding the user. Any observed mismatch makes the path stale immediately, regardless of its date.

Follow the path one checkpoint at a time. If a checkpoint diverges, stop applying later steps and diagnose that boundary. Do not combine the path with every optional control mentioned by official documentation or other integrations.

A path may be reverified only by completing a real allowlisted inbound message and a real complete bridge reply. Fake-backed tests, `doctor`, token exchange, gateway connection, and official-document review are useful evidence but cannot advance the dates by themselves. Remove a path from the active index when it is known to be wrong; Git history and the linked issue retain its provenance.

## Contributing setup evidence

An agent may offer to open a GitHub issue describing useful evidence from a setup attempt, whether successful or not. Create the issue only with the user's permission and submit a redacted summary rather than a raw transcript.

Include:

- bridge version or revision when known;
- platform mode and sandbox or production context when known;
- the path followed and its verification date;
- the first checkpoint that diverged, or the completed live checks;
- relevant error codes or interface labels;
- the recovery that worked, if any.

Never include credentials, complete allowlist identities, tenant or application identifiers, signed URLs, private local paths, or unredacted logs. A report becomes a verified path only after its claims agree with the implementation and it records a complete live round trip.
