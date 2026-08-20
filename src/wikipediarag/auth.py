from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PlatformRole(StrEnum):
    user = "USER"
    platform_admin = "PLATFORM_ADMIN"


class GroupType(StrEnum):
    local = "LOCAL"
    oidc = "OIDC"


class AuthenticationMethod(StrEnum):
    local = "local"
    oidc = "oidc"
    test = "test"


@dataclass(frozen=True, slots=True)
class ActorContext:
    user_id: str
    platform_role: PlatformRole
    session_id: str
    authentication_method: AuthenticationMethod
    request_id: str
    trace_id: str


class AuthorizationError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def require_active_tenant(actor: ActorContext) -> str:
    """Temporary storage-call compatibility shim during the clean cutover.

    It is deliberately not derived from an actor or session and therefore
    cannot act as authorization.  Repository calls are being reduced to their
    KB/document scope and ignore this value until their signatures are removed.
    """
    del actor
    return ""
