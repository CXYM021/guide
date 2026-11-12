# -*- coding: utf-8 -*-
"""账号、密码与会话。只用标准库，数据落在产品自己的 SQLite 库里。

安全边界（如实说明，不夸大）
----------------------------
- 密码用 PBKDF2-HMAC-SHA256 加盐存储，不存明文；比较用 hmac.compare_digest。
- 会话 token 只以 SHA-256 摘要入库，Cookie 用 HttpOnly + SameSite=Lax。
- 这是自托管小团队级别的实现，不是 OAuth / 企业 SSO，没有邮箱验证、
  没有多因素认证、没有邮件找回。忘记密码请在服务器上运行
  `python tools/reset_password.py 用户名`（本机可直接写库，不需要邮箱）。
  HTTPS 由部署方的反向代理负责。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path

USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fff]{2,32}$", re.UNICODE)
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128
PBKDF2_ROUNDS = 120_000
# 零宽空格/零宽连接符/方向标记/BOM：肉眼不可见，但会让「同一个用户名」变成另一个键。
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"), None)
_DDL = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_login_at INTEGER,
  username_key TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


class AuthError(ValueError):
    """可直接返回给客户端的认证错误。"""


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()


def clean_username(value: object) -> str:
    """把用户输入的用户名收成一个稳定写法：去首尾空白 + 全角转半角 + 去掉不可见格式字符。

    手机输入法、聊天软件复制、密码管理器自动填充都会带上 U+200B（零宽空格）、
    U+FEFF（零宽不换行）这类「看不见的字符」。它们在二进制比较里是另一个名字，
    于是出现「同一组账号密码，第二次登录却说密码不对」。NFKC 不会去掉它们，
    str.strip() 也不认，只能按 Unicode 格式类（Cf）显式清掉。
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(_INVISIBLE)
    return text.strip()


def username_key(username: object) -> str:
    """登录名规范化：清理不可见字符后做大小写折叠，只用于查找与查重。

    显示名仍保留注册时的写法（大小写），密码不做任何规范化。
    """
    return clean_username(username).casefold()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    """用户与会话的持久化；所有写操作串行化，SQLite 每次新建连接。"""

    def __init__(self, data_dir: str | Path, *, session_days: int = 30,
                 max_users: int = 0, invite_code: str = "",
                 rate_limit_window: int = 600) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "app.db"
        self.session_seconds = max(1, int(session_days)) * 86_400
        self.max_users = max(0, int(max_users))
        self.invite_code = str(invite_code or "")
        self._lock = threading.RLock()
        self._hits: dict[str, deque] = defaultdict(deque)
        self._rate_window = rate_limit_window
        with self._session() as conn:
            conn.executescript(_DDL)
            self._migrate_username_key(conn)

    def _migrate_username_key(self, conn: sqlite3.Connection) -> None:
        """老库没有 username_key 列：补列并回填，不改动任何密码哈希。

        老库若同时存在仅大小写不同的账号，规范化 key 会撞车；给后注册者
        加唯一后缀，登录时「精确用户名优先」保证两者都还能用自己的原名登录。
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "username_key" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN username_key TEXT")
        rows = conn.execute(
            "SELECT id, username, username_key FROM users ORDER BY created_at, id"
        ).fetchall()
        # 重建条件：没键，或者键是旧算法留下的（含不可见字符/尾随空白）。
        # 带 "#" 的是撞车时加的后缀，属于正常键，不重建。
        stale = [r for r in rows
                 if not r[2] or (r[2] != username_key(r[1]) and "#" not in r[2])]
        if stale:
            conn.execute("UPDATE users SET username_key=NULL")
            used: set[str] = set()
            for uid, name, _current in rows:
                key = username_key(name)
                if not key:
                    continue
                if key in used:
                    key = f"{key}#{uid[:8]}"
                used.add(key)
                conn.execute("UPDATE users SET username_key=? WHERE id=?",
                             (key, uid))
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_key "
            "ON users(username_key) WHERE username_key IS NOT NULL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _session(self):
        """提交/回滚后必须 close()，否则 Windows 上文件句柄不释放。"""
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---------- 限流 ----------
    def _check_rate(self, key: str, limit: int) -> None:
        now = time.time()
        q = self._hits[key]
        while q and now - q[0] > self._rate_window:
            q.popleft()
        if len(q) >= limit:
            raise AuthError("操作太频繁，请稍后重试")
        q.append(now)

    # ---------- 账号 ----------
    def register(self, username: str, password: str,
                 invite_code: str = "", *, client_ip: str = "") -> dict:
        # 同一 NAT 下可能是一家人在同一台机器上注册，限流放宽到 30；
        # 防止批量刷号的主要防线是邀请码与 max_users，不是这里。
        self._check_rate(f"reg:{client_ip}", 30)
        username = clean_username(username)
        if not USERNAME_RE.match(username):
            raise AuthError("用户名需 2–32 位，仅允许中英文、数字与下划线")
        if not isinstance(password, str) or not (
                MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN):
            raise AuthError(
                f"密码长度需 {MIN_PASSWORD_LEN}–{MAX_PASSWORD_LEN} 位")
        if self.invite_code and not hmac.compare_digest(
                str(invite_code or ""), self.invite_code):
            raise AuthError("邀请码不正确")
        key = username_key(username)
        salt = secrets.token_bytes(16)
        uid = secrets.token_hex(16)
        with self._lock, self._session() as conn:
            if self.max_users and self.user_count_unlocked(conn) >= self.max_users:
                raise AuthError("已达到站点用户上限")
            if conn.execute("SELECT 1 FROM users WHERE username=?",
                            (username,)).fetchone() or conn.execute(
                                "SELECT 1 FROM users WHERE username_key=?",
                                (key,)).fetchone():
                raise AuthError("用户名已被注册")
            try:
                conn.execute(
                    "INSERT INTO users"
                    "(id, username, password_hash, salt, created_at, username_key)"
                    " VALUES(?,?,?,?,?,?)",
                    (uid, username, _hash_password(password, salt),
                     salt.hex(), int(time.time()), key))
            except sqlite3.IntegrityError as exc:
                raise AuthError("用户名已被注册") from exc
        return {"id": uid, "username": username}

    def user_count_unlocked(self, conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def user_count(self) -> int:
        with self._session() as conn:
            return self.user_count_unlocked(conn)

    def set_password(self, username: str, password: str) -> dict:
        """本机重置密码（没有邮箱验证，只能由能访问 data/ 的人执行）。

        重置后删掉该账号的全部会话：如果密码是「忘了」而不是「要换」，
        旧会话很可能在别人手里。
        """
        username = clean_username(username)
        if not isinstance(password, str) or not (
                MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN):
            raise AuthError(
                f"密码长度需 {MIN_PASSWORD_LEN}–{MAX_PASSWORD_LEN} 位")
        key = username_key(username)
        salt = secrets.token_bytes(16)
        with self._lock, self._session() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE username_key=? ORDER BY created_at LIMIT 1",
                (key,)).fetchone()
            if row is None:
                raise AuthError("用户名不存在")
            uid = row[0]
            conn.execute("UPDATE users SET password_hash=?, salt=? WHERE id=?",
                         (_hash_password(password, salt), salt.hex(), uid))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        return {"id": uid, "username": username}

    def login(self, username: str, password: str, *,
              client_ip: str = "") -> str:
        username = clean_username(username)
        key = username_key(username)
        # 限流按「来源 + 账号」而不是只按来源：同一 WiFi/NAT 下的多个用户
        # 不能因为别人反复登录就被一起挡住。
        self._check_rate(f"login:{client_ip}:{key[:64]}", 20)
        if not isinstance(password, str) or not password:
            raise AuthError("用户名或密码不正确")
        with self._lock, self._session() as conn:
            # 精确名优先：老库里可能同时存在 Alice 与 alice 两个账号，
            # 不能让规范化查询把后者劫持到前者。查不到再按规范化名找。
            row = conn.execute(
                "SELECT id, password_hash, salt FROM users WHERE username=?",
                (username,)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT id, password_hash, salt FROM users"
                    " WHERE username_key=? ORDER BY created_at LIMIT 1",
                    (key,)).fetchone()
            if row is None:
                raise AuthError("用户名或密码不正确")
            uid, stored, salt_hex = row
            if not hmac.compare_digest(
                    _hash_password(password, bytes.fromhex(salt_hex)), stored):
                raise AuthError("用户名或密码不正确")
            token = secrets.token_urlsafe(32)
            now = int(time.time())
            conn.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, expires_at)"
                " VALUES(?,?,?,?)",
                (_token_hash(token), uid, now, now + self.session_seconds))
            conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now, uid))
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        return token

    def user_for_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        now = int(time.time())
        with self._session() as conn:
            row = conn.execute(
                "SELECT u.id, u.username, u.created_at, s.expires_at"
                " FROM sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash=? AND s.expires_at>?",
                (_token_hash(token), now)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "username": row[1],
                "created_at": row[2], "expires_at": row[3]}

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock, self._session() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?",
                         (_token_hash(token),))

    @property
    def cookie_max_age(self) -> int:
        return self.session_seconds
