"""Set while a visitor registers through a spent one-time invite link.

The link was made to say who invited whom, so the referral is recorded even
with the referral system switched off; rewards still follow its switch.
"""

from contextvars import ContextVar

INVITED_BY_LINK: ContextVar[bool] = ContextVar("s_invited_by_link", default=False)
