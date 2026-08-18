"""Model aliases, families and resolution.

There is still no `claude model list` (open feature request), so the
bridge learns concrete versions from two honest sources: the status line
of every live session (model.id arrives with each tick, free) and an
optional one-token probe (`claude -p ok --model X --output-format json`,
whose modelUsage is keyed by the id that actually served). Family
aliases resolve server-side to the newest release, so chains written as
aliases never need rewriting when a new model ships.
"""

ALIASES = ["fable", "opus", "sonnet", "haiku", "opusplan",
           "opus[1m]", "sonnet[1m]", "best"]

FAMILIES = ("fable", "opus", "sonnet", "haiku")


def family_of(name):
    low = (name or "").lower()
    for f in FAMILIES:
        if f in low:
            return f
    return None


def known(cfg, registry=None):
    opts = (registry or {}).get("opts", {})
    out = [a for a in ALIASES
           if a != "best" or opts.get("allow_best")]
    for m in cfg.get("custom_models") or []:
        if m not in out:
            out.append(m)
    return out


def resolve(alias, registry):
    """What to pass to --model, honouring the pin-vs-alias option."""
    if not alias:
        return None
    opts = (registry or {}).get("opts", {})
    if opts.get("prefer_aliases", True):
        return alias
    entry = (registry or {}).get("map", {}).get(alias.lower()) or {}
    return entry.get("id") or alias


def label(alias, registry):
    entry = (registry or {}).get("map", {}).get((alias or "").lower()) or {}
    return entry.get("display") or ""


def next_in_chain(chain, current):
    """The model after `current` in the chain, or None at the end."""
    cur_fam = family_of(current)
    try:
        i = chain.index(current)
    except ValueError:
        i = next((k for k, c in enumerate(chain)
                  if family_of(c) == cur_fam), -1)
        if i < 0:
            return chain[0] if chain else None
    return chain[i + 1] if i + 1 < len(chain) else None
