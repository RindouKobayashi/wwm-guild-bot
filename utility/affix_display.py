"""Display affix reference ranges without claiming unrelated tiers are upgrade caps."""
import math


def format_affix_range(info, value, fallback=str):
    if not isinstance(info, dict):
        return ""
    exact = info.get("min") is not None and info.get("max") is not None
    low, high = (info.get("min"), info.get("max")) if exact else (info.get("name_min"), info.get("name_max"))
    try:
        current, low, high = float(value), float(low), float(high)
        if not all(math.isfinite(v) for v in (current, low, high)) or low > high:
            return ""
        fmt = info.get("format") or ""
        def show(v):
            return fmt.format(v) if fmt else fallback(v)
        label = "ID reference" if exact else "name reference across IDs"
        suffix = ""
        # Out-of-range values can have different scaling; never call them MAX.
        if exact and low <= current <= high and low < high:
            suffix = ", **MAX**" if math.isclose(current, high, rel_tol=1e-7, abs_tol=1e-8) else f", +{show(high-current)} to reference max"
        return f"**{show(current)}** ({label}: {show(low)}–{show(high)}{suffix})"
    except (ValueError, TypeError, IndexError, KeyError, OverflowError):
        return ""
