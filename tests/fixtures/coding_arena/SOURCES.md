# Coding arena task sources (inspired patterns)

Offline-generated multi-file tasks. No third-party repository commits are vendored.

| Task | Pattern inspiration (public knowledge) |
|------|----------------------------------------|
| t09_path_join_safety | Path traversal via unsanitized join / static file resolve |
| t10_rate_limit_inclusive | API gateway rate-limit inclusive boundary off-by-one |
| t14_retry_backoff | HTTP client exponential backoff stuck at base delay |
| t15_pagination_cursor | Cursor pagination inclusive vs exclusive |
| t21_auth_expiry | Token/JWT `exp` boundary at exact timestamp |

All code under `tasks/*/scaffold` is original synthetic fixtures for WW arena.
