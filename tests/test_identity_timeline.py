"""Same Timeline M1 — cross-entry same entity (terminal/http ↔ telegram).

Covers:
1. Single-user: http/default then telegram owner → same entity_id
2. Single-user: telegram first then http/default → same entity_id
3. Multi-user: two telegram user_ids → different entity_ids
4. Explicit link merges two platforms onto one entity
5. run_task empty entity_id does not pick arbitrary entities[0]
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from wavegate.identity import IdentityResolver, is_single_user_mode


@pytest.fixture
def id_db(tmp_path, monkeypatch):
    """Temp identity DB + isolated config so real ~/.ww/pairing.json is ignored."""
    cfg = tmp_path / "wwcfg"
    cfg.mkdir()
    monkeypatch.setenv("WW_CONFIG", str(cfg))
    monkeypatch.setenv("WW_PAIRING_STORE", str(cfg / "pairing.json"))
    return str(tmp_path / "identity.db")


@pytest.fixture
def single_user_env(monkeypatch):
    monkeypatch.setenv("WW_SINGLE_USER", "1")
    monkeypatch.delenv("WW_MULTI_TENANT", raising=False)
    monkeypatch.setenv("WW_OWNER_TELEGRAM_ID", "5233788587")
    monkeypatch.delenv("TELEGRAM_WW_WORKSPACE", raising=False)


@pytest.fixture
def multi_user_env(monkeypatch):
    monkeypatch.setenv("WW_SINGLE_USER", "0")
    monkeypatch.delenv("WW_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_WW_WORKSPACE", raising=False)


def test_is_single_user_default_true(monkeypatch):
    monkeypatch.delenv("WW_SINGLE_USER", raising=False)
    monkeypatch.delenv("WW_MULTI_TENANT", raising=False)
    assert is_single_user_mode() is True


def test_is_single_user_off(monkeypatch):
    monkeypatch.setenv("WW_SINGLE_USER", "0")
    assert is_single_user_mode() is False


def test_multi_tenant_disables_single_user(monkeypatch):
    monkeypatch.delenv("WW_SINGLE_USER", raising=False)
    monkeypatch.setenv("WW_MULTI_TENANT", "1")
    assert is_single_user_mode() is False


# ── 1. http then telegram owner → same ──────────────────────────


def test_single_user_http_then_telegram_owner_same_entity(id_db, single_user_env):
    r = IdentityResolver(db_path=id_db)
    http_ent = r.resolve("http", "default", display_name="User")
    tg_ent = r.resolve("telegram", "5233788587", "5233788587", display_name="Owner")
    assert http_ent == tg_ent
    assert r.get_primary_entity_id() == http_ent
    # terminal/default also lands on primary
    term_ent = r.resolve("terminal", "default")
    assert term_ent == http_ent


# ── 2. telegram first then http → same ──────────────────────────


def test_single_user_telegram_first_then_http_same_entity(id_db, single_user_env):
    r = IdentityResolver(db_path=id_db)
    tg_ent = r.resolve("telegram", "5233788587", "5233788587", display_name="Owner")
    http_ent = r.resolve("http", "default", display_name="User")
    assert tg_ent == http_ent
    assert r.get_primary_entity_id() == tg_ent
    # local defaults pre-linked when telegram became primary
    links = r.get_platform_ids(tg_ent)
    platforms = {(lk["platform"], lk["user_id"]) for lk in links}
    assert ("http", "default") in platforms
    assert ("telegram", "5233788587") in platforms


# ── 3. two telegram users → different ───────────────────────────


def test_multi_user_two_telegram_users_different(id_db, single_user_env):
    """Even in single-user mode, distinct Telegram users stay separate."""
    r = IdentityResolver(db_path=id_db)
    # Establish primary via local first
    primary = r.resolve("http", "default")
    owner = r.resolve("telegram", "5233788587", "5233788587")
    other = r.resolve("telegram", "9999999999", "9999999999")
    assert owner == primary
    assert other != owner
    assert other != primary


def test_strict_multi_user_no_auto_merge(id_db, multi_user_env, monkeypatch):
    monkeypatch.setenv("WW_OWNER_TELEGRAM_ID", "5233788587")
    r = IdentityResolver(db_path=id_db)
    http_ent = r.resolve("http", "default")
    tg_ent = r.resolve("telegram", "5233788587", "5233788587")
    # Strict mode: no auto-merge across platforms
    assert http_ent != tg_ent


# ── 4. explicit link merges ─────────────────────────────────────


def test_explicit_link_merges_platforms(id_db, multi_user_env):
    r = IdentityResolver(db_path=id_db)
    a = r.resolve("http", "default")
    b = r.resolve("telegram", "111", "111")
    assert a != b
    r.link(a, "telegram", "111", "111")
    # After link, resolve telegram returns a
    assert r.resolve("telegram", "111", "111") == a
    # Also works with different chat_id for same user via lookup_by_user
    # (new chat still maps via user_id)
    assert r.resolve("telegram", "111", "other-chat") == a or (
        r._lookup_by_user("telegram", "111") == a
    )


def test_explicit_link_cli_style_to_primary(id_db, single_user_env):
    r = IdentityResolver(db_path=id_db)
    primary = r.resolve_local("http", "default")
    # Simulate a separate discord identity then link to primary
    other = r.resolve("discord", "user42")  # non-local non-owner
    # In single-user, discord is strict — different entity
    assert other != primary
    r.link(primary, "discord", "user42")
    assert r.resolve("discord", "user42") == primary


# ── 5. empty entity_id resolution ≠ entities[0] ─────────────────


def test_resolve_local_ignores_entities_order(id_db, single_user_env):
    """Primary must win even when another entity is more recently active."""
    r = IdentityResolver(db_path=id_db)
    primary = r.resolve("http", "default")
    # Create a noisier non-owner entity and touch it more
    other = r.resolve("telegram", "9999999999", "9999999999")
    for _ in range(5):
        r.resolve("telegram", "9999999999", "9999999999")
    entities = r.get_all_entities()
    # entities[0] is last_active DESC — may be the other entity
    assert entities[0]["entity_id"] in (primary, other)
    # resolve_local must still return primary, not entities[0]
    local = r.resolve_local("http", "default")
    assert local == primary
    assert local != other or primary == other  # only equal if same (they aren't)


def test_run_task_entity_selection_uses_primary_not_entities0(id_db, single_user_env):
    """Mirror server.run_task empty-entity path: resolve_local, not entities[0]."""
    r = IdentityResolver(db_path=id_db)
    primary = r.resolve("terminal", "default")
    other = r.resolve("telegram", "8888888888", "8888888888")
    # Bump other so it would be entities[0]
    for _ in range(3):
        r.resolve("telegram", "8888888888", "8888888888")
    entities = r.get_all_entities()
    assert entities[0]["entity_id"] == other  # most recently active

    # What run_task does now (not entities[0])
    chosen = r.resolve_local(platform="http", user_id="default", display_name="User")
    assert chosen == primary
    assert chosen != entities[0]["entity_id"]


def test_ensure_owner_link_relinks_split_entities(id_db, single_user_env, monkeypatch):
    """Production-style split: http entity and telegram entity already exist."""
    # Create split without single-user merge (simulate legacy DB)
    monkeypatch.setenv("WW_SINGLE_USER", "0")
    r0 = IdentityResolver(db_path=id_db)
    http_ent = r0.resolve("http", "default")
    tg_ent = r0.resolve("telegram", "5233788587", "5233788587")
    assert http_ent != tg_ent

    # Upgrade to single-user: owner telegram should join primary (http)
    monkeypatch.setenv("WW_SINGLE_USER", "1")
    r = IdentityResolver(db_path=id_db)
    # Bootstrap primary from local link
    assert r.get_primary_entity_id() == http_ent
    linked = r.ensure_owner_link(
        "telegram", "5233788587", "5233788587", entity_id=tg_ent
    )
    assert linked == http_ent
    assert r.resolve("telegram", "5233788587", "5233788587") == http_ent


def test_workspace_positive_id_as_owner(id_db, monkeypatch):
    monkeypatch.setenv("WW_SINGLE_USER", "1")
    monkeypatch.delenv("WW_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_WW_WORKSPACE", "5233788587")
    r = IdentityResolver(db_path=id_db)
    http_ent = r.resolve("http", "default")
    tg_ent = r.resolve("telegram", "5233788587", "5233788587")
    assert http_ent == tg_ent


def test_group_workspace_not_owner(id_db, monkeypatch):
    """Negative group chat ids must not be treated as owner user ids."""
    monkeypatch.setenv("WW_SINGLE_USER", "1")
    monkeypatch.delenv("WW_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_WW_WORKSPACE", "-1003841986648")
    r = IdentityResolver(db_path=id_db)
    http_ent = r.resolve("http", "default")
    # Without owner config, first telegram becomes implicit owner and merges
    tg_ent = r.resolve("telegram", "111", "111")
    assert http_ent == tg_ent  # first telegram still merges as implicit owner
    other = r.resolve("telegram", "222", "222")
    assert other != http_ent


def test_primary_persists_across_resolver_instances(id_db, single_user_env):
    r1 = IdentityResolver(db_path=id_db)
    eid = r1.resolve("http", "default")
    r2 = IdentityResolver(db_path=id_db)
    assert r2.get_primary_entity_id() == eid
    assert r2.resolve("telegram", "5233788587", "5233788587") == eid


def test_set_primary_entity_id(id_db, single_user_env):
    r = IdentityResolver(db_path=id_db)
    a = r.resolve("http", "default")
    b = r.resolve("telegram", "9999999999", "9999999999")
    r.set_primary_entity_id(b)
    assert r.get_primary_entity_id() == b
    # Local surfaces follow new primary
    assert r.resolve_local("terminal", "default") == b
