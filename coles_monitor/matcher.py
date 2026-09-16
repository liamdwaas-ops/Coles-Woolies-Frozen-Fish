import re


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def is_wanted_name(name):
    return bool(normalize(name))


def keyword_group(name):
    """Return the single report group for any SKU on the configured pages."""
    return "Frozen Seafood" if normalize(name) else None


def category_group(name, category_hint=""):
    """Classify every product returned by either configured category page."""
    return keyword_group(name)


def is_allowed_product(name, brand="", category_hint=""):
    # Category-page membership is the complete inclusion rule.
    return category_group(name, category_hint) is not None


def split_name_size(name, explicit_size=""):
    name = normalize(name)
    explicit_size = normalize(explicit_size)
    if explicit_size:
        suffix = re.compile(r"\s*\|?\s*" + re.escape(explicit_size) + r"\s*$", re.I)
        return normalize(suffix.sub("", name)), explicit_size
    match = re.search(r"\s*\|\s*([^|]+)$", name)
    if match:
        return normalize(name[:match.start()]), normalize(match.group(1))
    return name, ""
