"""
The tabs a student sees inside one exam, as the admin set them up. Four are built in (Full tests, Mini mocks, Previous year, Subject
wise) and each holds one type of test. The admin can also add tabs of their own (for example "Current affairs") and put tests in them.
"""
import re

# key: default label. The built-in keys are fixed; the admin chooses which show, their names and their order.
TAB_KEYS = {"full": "Full tests", "mini": "Mini mocks", "previous": "Previous year", "subject": "Subject wise"}
LABEL_MAX = 30
MAX_TABS = 12
CUSTOM_KEY = re.compile(r"^c[a-z0-9]{1,15}$")  # the admin screen makes these, for example "c1k9x2"


def is_custom(key) -> bool:
    return isinstance(key, str) and bool(CUSTOM_KEY.match(key))


def default_tabs() -> list[dict]:
    return [{"key": k, "label": label, "visible": True, "custom": False} for k, label in TAB_KEYS.items()]


def resolve_tabs(stored) -> list[dict]:
    """
    The tabs in the admin's order, with their names and whether they show. Anything missing or damaged falls back to the default,
    a built-in tab the admin never mentioned is added at the end, and a saved list can never hide the whole exam by accident.
    """
    out, seen = [], set()
    for item in stored if isinstance(stored, list) else []:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        custom = is_custom(key)
        if (key not in TAB_KEYS and not custom) or key in seen:
            continue
        label = str(item.get("label") or "").strip()[:LABEL_MAX]
        if custom and not label:
            continue  # a tab of the admin's own needs a name
        seen.add(key)
        out.append({"key": key, "label": label or TAB_KEYS[key], "visible": item.get("visible") is not False, "custom": custom})
    out += [t for t in default_tabs() if t["key"] not in seen]
    if not any(t["visible"] for t in out):
        out[0]["visible"] = True
    return out


def custom_keys(stored) -> set[str]:
    """The keys of the admin's own tabs, which a test may be put in."""
    return {t["key"] for t in resolve_tabs(stored) if t["custom"]}
