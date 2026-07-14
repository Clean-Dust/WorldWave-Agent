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

- Keep your API keys and tokens in `.env` files — never commit them.
- Use `WW_API_KEY` to protect your Worldwave server.
- Review the P2P consent settings in `~/.worldwave/consent.json` before enabling
  decentralized features.
