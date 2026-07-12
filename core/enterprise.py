"""
Enterprise Features — OAuth, RBAC, Audit Logging.

Production-grade security for team deployments:
  1. OAuth 2.0 / OIDC authentication (Google, GitHub, custom)
  2. Role-Based Access Control (admin, developer, viewer)
  3. Audit logging (all tool calls, config changes, auth events)
  4. Session management with JWT

Config:
  WW_OAUTH_PROVIDERS = "google,github"
  WW_OAUTH_GOOGLE_CLIENT_ID = "..."
  WW_OAUTH_GITHUB_CLIENT_ID = "..."
  WW_JWT_SECRET = "..."
  WW_AUDIT_LOG_PATH = "~/.ww/audit.log"
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger("ww.enterprise")


# ════════════════════════════════════════════════════════════════
# RBAC (Role-Based Access Control)
# ════════════════════════════════════════════════════════════════

class Role:
    """Predefined roles with permission sets."""

    ADMIN = "admin"
    DEVELOPER = "developer"
    VIEWER = "viewer"

    # Permission constants
    PERM_READ = "read"
    PERM_WRITE = "write"
    PERM_EXEC = "exec"
    PERM_ADMIN = "admin"
    PERM_DEPLOY = "deploy"
    PERM_AUDIT = "audit"

    # Role → permissions mapping
    PERMISSIONS = {
        ADMIN: {PERM_READ, PERM_WRITE, PERM_EXEC, PERM_ADMIN, PERM_DEPLOY, PERM_AUDIT},
        DEVELOPER: {PERM_READ, PERM_WRITE, PERM_EXEC, PERM_DEPLOY},
        VIEWER: {PERM_READ},
    }

    ALL = {ADMIN, DEVELOPER, VIEWER}

    @classmethod
    def has_permission(cls, role: str, permission: str) -> bool:
        """Check if a role has a specific permission."""
        perms = cls.PERMISSIONS.get(role, set())
        return permission in perms

    @classmethod
    def get_permissions(cls, role: str) -> Set[str]:
        """Get all permissions for a role."""
        return cls.PERMISSIONS.get(role, set())


@dataclass
class User:
    """Enterprise user."""
    id: str
    email: str
    name: str = ""
    role: str = Role.VIEWER
    provider: str = ""       # "google", "github", "local"
    provider_id: str = ""    # OAuth provider's user ID
    avatar_url: str = ""
    created_at: str = ""
    last_login: str = ""
    active: bool = True

    def can(self, permission: str) -> bool:
        return Role.has_permission(self.role, permission)

    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "provider": self.provider,
            "active": self.active,
            "last_login": self.last_login,
        }


# ════════════════════════════════════════════════════════════════
# RBAC Manager
# ════════════════════════════════════════════════════════════════

class RBACManager:
    """User and role management with SQLite backend."""

    def __init__(self, db_path: str = ""):
        if not db_path:
            db_path = os.path.join(os.path.expanduser("~/.ww"), "users.db")
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT DEFAULT '',
                role TEXT DEFAULT 'viewer',
                provider TEXT DEFAULT 'local',
                provider_id TEXT DEFAULT '',
                avatar_url TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                last_login TEXT DEFAULT '',
                active INTEGER DEFAULT 1
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")
            conn.commit()
            conn.close()

    def create_user(self, email: str, name: str = "", role: str = Role.VIEWER,
                    provider: str = "local", provider_id: str = "") -> User:
        """Create a new user."""
        user_id = hashlib.sha256(f"{provider}:{email}".encode()).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                """INSERT OR REPLACE INTO users
                (id, email, name, role, provider, provider_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, email, name, role, provider, provider_id, now),
            )
            conn.commit()
            conn.close()
        return User(id=user_id, email=email, name=name, role=role,
                    provider=provider, provider_id=provider_id, created_at=now)

    def get_user(self, email: str = "", user_id: str = "") -> Optional[User]:
        """Get user by email or ID."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            if email:
                row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            elif user_id:
                row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            else:
                conn.close()
                return None
            conn.close()
            if row:
                return self._row_to_user(dict(row))
        return None

    def list_users(self, role: str = "") -> List[User]:
        """List all users, optionally filtered by role."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            if role:
                rows = conn.execute(
                    "SELECT * FROM users WHERE role=? AND active=1", (role,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM users WHERE active=1").fetchall()
            conn.close()
            return [self._row_to_user(dict(r)) for r in rows]

    def update_role(self, email: str, role: str) -> bool:
        """Change a user's role."""
        if role not in Role.ALL:
            return False
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute("UPDATE users SET role=? WHERE email=?", (role, email))
            conn.commit()
            conn.close()
        return True

    def deactivate(self, email: str) -> bool:
        """Deactivate a user (soft delete)."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute("UPDATE users SET active=0 WHERE email=?", (email,))
            conn.commit()
            conn.close()
        return True

    def record_login(self, email: str):
        """Record a user login timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute("UPDATE users SET last_login=? WHERE email=?", (now, email))
            conn.commit()
            conn.close()

    def _row_to_user(self, row: dict) -> User:
        return User(
            id=row.get("id", ""),
            email=row.get("email", ""),
            name=row.get("name", ""),
            role=row.get("role", Role.VIEWER),
            provider=row.get("provider", "local"),
            provider_id=row.get("provider_id", ""),
            avatar_url=row.get("avatar_url", ""),
            created_at=row.get("created_at", ""),
            last_login=row.get("last_login", ""),
            active=bool(row.get("active", 1)),
        )


# ════════════════════════════════════════════════════════════════
# Audit Logging
# ════════════════════════════════════════════════════════════════

@dataclass
class AuditEvent:
    """A single audit log entry."""
    timestamp: str
    event_type: str        # "tool_call", "config_change", "auth", "permission_denied"
    user_id: str = ""
    user_email: str = ""
    action: str = ""       # e.g. "execute_python", "config.set"
    resource: str = ""     # e.g. file path, config key
    details: str = ""      # Additional context (truncated)
    ip_address: str = ""
    success: bool = True
    session_id: str = ""

    def to_line(self) -> str:
        """Serialize as JSONL line."""
        return json.dumps({
            "ts": self.timestamp,
            "type": self.event_type,
            "user": self.user_email,
            "user_id": self.user_id,
            "action": self.action,
            "resource": self.resource,
            "details": self.details[:500],
            "ip": self.ip_address,
            "success": self.success,
            "session": self.session_id,
        }, ensure_ascii=False)


class AuditLogger:
    """Append-only audit log (JSONL format)."""

    def __init__(self, log_path: str = ""):
        self._log_path = log_path or os.path.join(
            os.path.expanduser("~/.ww"), "audit.log"
        )
        os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
        self._lock = threading.Lock()

    def log(self, event: AuditEvent):
        """Write an audit event."""
        line = event.to_line() + "\n"
        with self._lock:
            with open(self._log_path, "a") as f:
                f.write(line)

    def log_tool_call(self, tool_name: str, user_email: str = "",
                      user_id: str = "", resource: str = "",
                      details: str = "", success: bool = True):
        """Log a tool call."""
        self.log(AuditEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type="tool_call",
            user_email=user_email,
            user_id=user_id,
            action=tool_name,
            resource=resource,
            details=details,
            success=success,
        ))

    def log_config_change(self, key: str, user_email: str = "",
                          old_value: str = "", new_value: str = ""):
        """Log a configuration change."""
        self.log(AuditEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type="config_change",
            user_email=user_email,
            action="config.set",
            resource=key,
            details=f"{old_value} → {new_value}"[:200],
        ))

    def log_auth(self, user_email: str, success: bool, ip: str = ""):
        """Log an authentication event."""
        self.log(AuditEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type="auth",
            user_email=user_email,
            action="login" if success else "login_failed",
            ip_address=ip,
            success=success,
        ))

    def log_permission_denied(self, user_email: str, action: str, resource: str = ""):
        """Log a permission denial."""
        self.log(AuditEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type="permission_denied",
            user_email=user_email,
            action=action,
            resource=resource,
            success=False,
        ))

    def query(self, event_type: str = "", user_email: str = "",
              limit: int = 100, offset: int = 0) -> List[dict]:
        """Query recent audit events."""
        results = []
        if not os.path.exists(self._log_path):
            return results

        with open(self._log_path) as f:
            lines = f.readlines()

        # Read from end (newest first)
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event_type and event.get("type") != event_type:
                    continue
                if user_email and event.get("user") != user_email:
                    continue
                results.append(event)
                if len(results) >= limit:
                    break
            except json.JSONDecodeError:
                continue

        return results

    def get_stats(self) -> dict:
        """Get audit log statistics."""
        if not os.path.exists(self._log_path):
            return {"total_events": 0, "size_bytes": 0}

        with open(self._log_path) as f:
            lines = f.readlines()

        stats = {
            "total_events": len([l for l in lines if l.strip()]),
            "size_bytes": os.path.getsize(self._log_path),
            "by_type": {},
        }

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                etype = event.get("type", "unknown")
                stats["by_type"][etype] = stats["by_type"].get(etype, 0) + 1
            except json.JSONDecodeError:
                continue

        return stats


# ════════════════════════════════════════════════════════════════
# Simple JWT (HMAC-based, zero deps)
# ════════════════════════════════════════════════════════════════

class SimpleJWT:
    """Minimal JWT implementation — HMAC-SHA256, zero dependencies."""

    def __init__(self, secret: str = ""):
        self._secret = secret or os.environ.get("WW_JWT_SECRET", os.urandom(32).hex())

    def encode(self, payload: dict, expiry_minutes: int = 1440) -> str:
        """Create a signed JWT token."""
        import base64

        header = {"alg": "HS256", "typ": "JWT"}
        body = {
            **payload,
            "exp": int(time.time()) + expiry_minutes * 60,
            "iat": int(time.time()),
        }

        # Encode header and body
        h = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
        b = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()

        # Sign
        signing_input = f"{h}.{b}"
        sig = hmac.new(
            self._secret.encode(),
            signing_input.encode(),
            hashlib.sha256,
        ).digest()
        s = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()

        return f"{h}.{b}.{s}"

    def decode(self, token: str) -> Optional[dict]:
        """Verify and decode a JWT token."""
        import base64

        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None

            h, b, s = parts

            # Verify signature
            signing_input = f"{h}.{b}"
            expected_sig = hmac.new(
                self._secret.encode(),
                signing_input.encode(),
                hashlib.sha256,
            ).digest()
            actual_sig = base64.urlsafe_b64decode(s + "==")
            if not hmac.compare_digest(expected_sig, actual_sig):
                return None

            # Decode body
            body_bytes = base64.urlsafe_b64decode(b + "==")
            body = json.loads(body_bytes)

            # Check expiry
            if body.get("exp", 0) < time.time():
                return None

            return body
        except Exception:
            return None


# ── Enterprise System ────────────────────────────────────────────

class EnterpriseSystem:
    """Unified enterprise features — RBAC + Audit + JWT."""

    def __init__(self, jwt_secret: str = "", audit_path: str = ""):
        self.rbac = RBACManager()
        self.audit = AuditLogger(audit_path)
        self.jwt = SimpleJWT(jwt_secret)

    def authenticate(self, email: str) -> Optional[str]:
        """Authenticate a user and return a JWT token."""
        user = self.rbac.get_user(email=email)
        if not user or not user.active:
            self.audit.log_auth(email, False)
            return None

        self.rbac.record_login(email)
        self.audit.log_auth(email, True)
        return self.jwt.encode({"sub": user.id, "email": user.email, "role": user.role})

    def authorize(self, token: str, permission: str) -> Optional[User]:
        """Verify JWT and check permission. Returns User if authorized."""
        payload = self.jwt.decode(token)
        if not payload:
            return None

        user = self.rbac.get_user(user_id=payload.get("sub", ""))
        if not user:
            return None

        if not user.can(permission):
            self.audit.log_permission_denied(user.email, permission)
            return None

        return user

    def get_user_from_token(self, token: str) -> Optional[User]:
        """Get user from a JWT token (without permission check)."""
        payload = self.jwt.decode(token)
        if not payload:
            return None
        return self.rbac.get_user(user_id=payload.get("sub", ""))


# ════════════════════════════════════════════════════════════════
# Approval Gating — Per-Action Permission System
# ════════════════════════════════════════════════════════════════

class ApprovalMode:
    """Approval modes for action gating."""
    AUTO = "auto"       # Execute automatically (no prompt)
    ASK = "ask"         # Ask user for confirmation
    DENY = "deny"       # Always deny (block execution)
    SANDBOX = "sandbox" # Run in sandbox only


class ApprovalPolicy:
    """Policy rule: determines approval mode for a tool category."""

    def __init__(
        self,
        name: str,
        tool_categories: List[str] = None,
        tool_names: List[str] = None,
        mode: str = ApprovalMode.AUTO,
        entities: List[str] = None,    # Apply to specific entities only
        time_window: tuple = None,     # (start_hour, end_hour) restriction
        max_per_hour: int = 0,         # Rate limit
    ):
        self.name = name
        self.tool_categories = tool_categories or []
        self.tool_names = tool_names or []
        self.mode = mode
        self.entities = entities or []   # Empty = all entities
        self.time_window = time_window
        self.max_per_hour = max_per_hour
        self._counter: Dict[str, int] = {}  # entity_id → count
        self._counter_reset = time.time()

    def matches(self, tool_name: str, tool_category: str,
                entity_id: str = "") -> bool:
        """Check if this policy applies to a tool call."""
        # Entity filter
        if self.entities and entity_id not in self.entities:
            return False

        # Tool match
        if tool_name in self.tool_names:
            return True
        if tool_category in self.tool_categories:
            return True

        return False

    def check_rate_limit(self, entity_id: str) -> bool:
        """Check if rate limit is exceeded. Returns True if allowed."""
        if self.max_per_hour <= 0:
            return True

        now = time.time()
        if now - self._counter_reset > 3600:
            self._counter = {}
            self._counter_reset = now

        count = self._counter.get(entity_id, 0)
        return count < self.max_per_hour

    def record_use(self, entity_id: str):
        """Record a tool use for rate limiting."""
        if self.max_per_hour <= 0:
            return
        self._counter[entity_id] = self._counter.get(entity_id, 0) + 1


class ApprovalGating:
    """Per-action approval gating system.

    Layers on top of RBAC. Each tool call goes through:
    1. RBAC check — does user have permission?
    2. Policy check — what approval mode applies?
    3. Rate limit check — is usage within limits?

    Config via WW_APPROVAL_DEFAULT_MODE (default: "auto").
    """

    # Default policies for common dangerous tool categories
    DEFAULT_POLICIES = [
        ApprovalPolicy(
            name="destructive-filesystem",
            tool_names=["rm", "delete", "format", "mkfs", "dd"],
            mode=ApprovalMode.ASK,
        ),
        ApprovalPolicy(
            name="system-commands",
            tool_categories=["system"],
            mode=ApprovalMode.ASK,
        ),
        ApprovalPolicy(
            name="network-egress",
            tool_categories=["network", "web"],
            mode=ApprovalMode.AUTO,
            max_per_hour=60,
        ),
        ApprovalPolicy(
            name="file-writes",
            tool_categories=["file_write"],
            mode=ApprovalMode.AUTO,
            max_per_hour=200,
        ),
        ApprovalPolicy(
            name="code-execution",
            tool_categories=["code", "exec"],
            mode=ApprovalMode.SANDBOX,
        ),
    ]

    def __init__(
        self,
        default_mode: str = "",
        policies: List[ApprovalPolicy] = None,
        enterprise: EnterpriseSystem = None,
    ):
        self.default_mode = default_mode or os.environ.get(
            "WW_APPROVAL_DEFAULT_MODE", ApprovalMode.AUTO
        )
        self._policies: List[ApprovalPolicy] = policies or list(self.DEFAULT_POLICIES)
        self._enterprise = enterprise or get_enterprise()
        self._pending_approvals: Dict[str, Dict] = {}  # approval_id → context
        self._lock = threading.Lock()

    def add_policy(self, policy: ApprovalPolicy):
        """Add a custom approval policy."""
        self._policies.append(policy)

    def remove_policy(self, name: str) -> bool:
        """Remove a policy by name."""
        for i, p in enumerate(self._policies):
            if p.name == name:
                self._policies.pop(i)
                return True
        return False

    def list_policies(self) -> List[Dict]:
        """List all active policies."""
        return [
            {
                "name": p.name,
                "mode": p.mode,
                "categories": p.tool_categories,
                "tools": p.tool_names,
                "rate_limit": p.max_per_hour,
                "entities": p.entities or ["all"],
            }
            for p in self._policies
        ]

    def check(self, tool_name: str, tool_category: str = "general",
              entity_id: str = "") -> Dict:
        """Check if a tool call is allowed.

        Returns:
            {"allowed": bool, "mode": str, "reason": str, "approval_id": str or None}
        """
        # Find matching policies (most restrictive wins)
        applicable = [p for p in self._policies
                      if p.matches(tool_name, tool_category, entity_id)]

        if not applicable:
            return {"allowed": True, "mode": self.default_mode, "reason": "default"}

        # Most restrictive mode
        mode_priority = {
            ApprovalMode.AUTO: 0,
            ApprovalMode.SANDBOX: 1,
            ApprovalMode.ASK: 2,
            ApprovalMode.DENY: 3,
        }
        most_restrictive = max(applicable, key=lambda p: mode_priority.get(p.mode, 0))

        mode = most_restrictive.mode

        if mode == ApprovalMode.DENY:
            return {
                "allowed": False,
                "mode": mode,
                "reason": f"Blocked by policy: {most_restrictive.name}",
            }

        # Rate limit check
        if not most_restrictive.check_rate_limit(entity_id):
            return {
                "allowed": False,
                "mode": ApprovalMode.DENY,
                "reason": f"Rate limit exceeded for {most_restrictive.name}",
            }

        if mode == ApprovalMode.AUTO:
            most_restrictive.record_use(entity_id)
            return {"allowed": True, "mode": mode, "reason": "auto-approved"}

        if mode == ApprovalMode.ASK:
            # Create pending approval
            import uuid
            approval_id = uuid.uuid4().hex[:12]
            with self._lock:
                self._pending_approvals[approval_id] = {
                    "tool": tool_name,
                    "category": tool_category,
                    "entity_id": entity_id,
                    "policy": most_restrictive.name,
                    "timestamp": time.time(),
                    "status": "pending",
                }
            return {
                "allowed": False,
                "mode": mode,
                "reason": f"Requires approval: {most_restrictive.name}",
                "approval_id": approval_id,
            }

        # SANDBOX
        most_restrictive.record_use(entity_id)
        return {
            "allowed": True,
            "mode": mode,
            "reason": "running in sandbox",
            "sandbox": True,
        }

    def approve(self, approval_id: str) -> bool:
        """Approve a pending action."""
        with self._lock:
            if approval_id in self._pending_approvals:
                self._pending_approvals[approval_id]["status"] = "approved"
                # Record for audit
                ctx = self._pending_approvals[approval_id]
                self._enterprise.audit.log_tool_call(
                    tool_name=ctx["tool"],
                    user_email=ctx.get("entity_id", ""),
                    success=True,
                    details=f"User approved: {ctx['policy']}",
                )
                return True
        return False

    def deny(self, approval_id: str) -> bool:
        """Deny a pending action."""
        with self._lock:
            if approval_id in self._pending_approvals:
                self._pending_approvals[approval_id]["status"] = "denied"
                ctx = self._pending_approvals[approval_id]
                self._enterprise.audit.log_tool_call(
                    tool_name=ctx["tool"],
                    user_email=ctx.get("entity_id", ""),
                    success=False,
                    details=f"User denied: {ctx['policy']}",
                )
                return True
        return False

    def get_pending(self) -> List[Dict]:
        """List pending approval requests."""
        with self._lock:
            return [
                {"approval_id": aid, **ctx}
                for aid, ctx in self._pending_approvals.items()
                if ctx["status"] == "pending"
            ]

    def cleanup_expired(self, max_age_seconds: int = 300):
        """Remove expired pending approvals."""
        now = time.time()
        with self._lock:
            expired = [
                aid for aid, ctx in self._pending_approvals.items()
                if now - ctx["timestamp"] > max_age_seconds
            ]
            for aid in expired:
                del self._pending_approvals[aid]

    def stats(self) -> Dict:
        """Approval gating statistics."""
        return {
            "policies": len(self._policies),
            "pending_approvals": len([
                a for a in self._pending_approvals.values()
                if a["status"] == "pending"
            ]),
            "default_mode": self.default_mode,
        }


# ── Singleton ────────────────────────────────────────────────────

_enterprise: Optional[EnterpriseSystem] = None
_approval_gating: Optional[ApprovalGating] = None


def get_enterprise() -> EnterpriseSystem:
    global _enterprise
    if _enterprise is None:
        _enterprise = EnterpriseSystem()
    return _enterprise


def get_approval_gating() -> ApprovalGating:
    global _approval_gating
    if _approval_gating is None:
        _approval_gating = ApprovalGating(enterprise=get_enterprise())
    return _approval_gating
