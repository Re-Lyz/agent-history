# Security reporting

The current beta candidate is the only maintained release line. Agent History works with local conversation files and can prepare native Agent resume records; it does not provide authentication or a network service. Treat databases, backups, trash, launch settings and logs as private local data.

Private vulnerability reporting is enabled on [the public repository](https://github.com/Re-Lyz/agent-history/security). For a suspected vulnerability, use GitHub **Security → Report a vulnerability**. If that option is unavailable, open a minimal issue asking the maintainer to establish a private channel; omit exploit details and confidential data until a private channel exists. There is no dedicated security email or guaranteed response time yet.

Include affected version, impact, reproducible steps using synthetic history and an optional fix. Never send API keys, auth.json, provider configuration containing secrets, real conversation transcripts or history.db. Review diagnostic output before attaching it. Regular functional bugs can use the bug issue template.
