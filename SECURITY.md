# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Worldwave, please **do not** open a
public issue.

Instead, report it privately via one of these channels:

1. **GitHub Security Advisory** — use the
   [Private Vulnerability Reporting](https://github.com/Clean-Dust/worldwave/security/advisories/new)
   feature on the repository.

2. **Email** — send details to the repository owner.

Please include:

- A clear description of the vulnerability.
- Steps to reproduce.
- Affected versions.
- Any potential mitigations you've identified.

## Response Timeline

- **Acknowledgment**: within 48 hours.
- **Initial assessment**: within 5 business days.
- **Fix timeline**: depends on severity; critical issues addressed as quickly as
  possible.

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.5.x   | :white_check_mark: |
| < 0.5.0 | :x:                |

## Security Best Practices for Users

- Keep your API keys and tokens in `.env` files — never commit them (`chmod 600 .env`).
- Use a long random `WW_API_KEY` (≥32 bytes urlsafe) to protect the HTTP API.
- Never set `WW_PAIRING_AUTO_APPROVE=true` on internet-facing Telegram bots; approve DMs with `ww pairing approve <CODE>`.
- Prefer `WW_HOST=127.0.0.1` when the API is only used locally or via a tunnel.
- Protect the public P2P bootstrap tracker with `WW_TRACKER_TOKEN` so `/p2p/register` is not open to the world.
- Review the P2P consent settings in `~/.worldwave/consent.json` before enabling
  decentralized features.

## P2P tracker auth (v1.1 minimal)

When `WW_TRACKER_TOKEN` is set, `POST /p2p/register` and `DELETE /p2p/register`
require `Authorization: Bearer <token>` (or `X-Tracker-Token`). Public
`GET /p2p/peers` returns only peers that registered with `public=true`.

**Follow-up (not yet implemented):** mutual peer attestation, signed node IDs,
rate limits, and IP redaction for whois remain deferred.
