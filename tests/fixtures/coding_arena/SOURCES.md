# Coding arena task sources (inspired patterns)

Offline-generated multi-file tasks. No third-party repository commits are vendored.
Hard Arena v2 (PM 0.13): ≥30 tasks; foundation suite t01–t22 retained; t23–t33 added.

| Task | Pattern inspiration (public knowledge) |
|------|----------------------------------------|
| t09_path_join_safety | Path traversal via unsanitized join / static file resolve |
| t10_rate_limit_inclusive | API gateway rate-limit inclusive boundary off-by-one |
| t14_retry_backoff | HTTP client exponential backoff stuck at base delay |
| t15_pagination_cursor | Cursor pagination inclusive vs exclusive |
| t16_timezone_naive | Naive datetime treated as local instead of UTC |
| t21_auth_expiry | Token/JWT `exp` boundary at exact timestamp |
| t23_multifile_rename_refactor | Multi-file call-graph discount logic fix |
| t24_path_symlink_escape | Absolute path + `..` escape under root |
| t25_timezone_compare | Aware/naive datetime compare as UTC |
| t26_test_driven_clamp | TDD clamp with agent-visible stub tests |
| t28_redirect_div_focus | Redirect mid-task focus swap |
| t29_samples_hard_json_merge | Deep merge JSON (samples path) |
| t30_adversarial_default_mut | Mutable default argument footgun |
| t31_realrepo_urljoin | urllib-style join hygiene (synthetic realrepo) |
| t32_realrepo_stable_unique | Ordered unique (synthetic realrepo) |
| t33_realrepo_semver_parse | Simple semver parse (synthetic realrepo) |

All code under `tasks/*/scaffold` is original synthetic fixtures for WW arena.
Real public repos for large-repo pressure clone only into `~/.cache/worldwave/coding_corpus` (allowlist).
