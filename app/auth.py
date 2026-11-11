from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .settings import get_settings

_security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_security)) -> None:
    """HTTP basic auth, enabled when STEUER_USER is set. Put Caddy/TLS in front when you use it."""
    st = get_settings()
    if not st.auth_user:
        return
    ok = (
        credentials is not None
        and secrets.compare_digest(credentials.username.encode(), st.auth_user.encode())
        and secrets.compare_digest(credentials.password.encode(), (st.auth_password or "").encode())
    )
    if not ok:
        raise HTTPException(status_code=401, detail="Authentication required",
                            headers={"WWW-Authenticate": 'Basic realm="Steuer Hub"'})
