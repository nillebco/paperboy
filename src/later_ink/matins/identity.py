"""The identity behind an email address, for limits and lookups.

One mailbox can be written many ways. Gmail ignores dots in the local part, and
every provider that supports subaddressing (RFC 5233) ignores a `+tag`. So
`a.b+matins@gmail.com`, `ab@gmail.com` and `a.b@gmail.com` are one inbox — and a
per-address limit keyed on the spelling is a suggestion rather than a limit.

That is not hypothetical: the OTP table on 6-7 November 2026 held
`af.u.c.aj.a.xor.5.84@gmail.com` and `niy.i.ce.bit.a.g0.1@gmail.com`, which is
what a script sends when it wants an unlimited budget from one address.

`identity_key` is what a limit or a lookup should be keyed on. The address the
user typed is still what mail is sent to: normalising for delivery would change
the recipient, and `+` is a legal local-part character outside subaddressing.
"""

from __future__ import annotations

# Providers that treat dots in the local part as decoration.
DOT_INSENSITIVE_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def identity_key(address: str) -> str:
    """The mailbox an address belongs to, as a stable key.

    Lowercase, no `+tag`, and no dots for the providers that ignore them. An
    address that cannot be split is returned lowercased and otherwise untouched,
    so an odd row in the database still gets a key of its own."""
    cleaned = (address or "").strip().lower()
    local, _, domain = cleaned.partition("@")
    if not domain or not local:
        return cleaned
    untagged = local.split("+", 1)[0]
    if not untagged:
        # A local part that is only "+something" is not subaddressing.
        return cleaned
    if domain in DOT_INSENSITIVE_DOMAINS:
        untagged = untagged.replace(".", "")
    return f"{untagged}@{domain}"
