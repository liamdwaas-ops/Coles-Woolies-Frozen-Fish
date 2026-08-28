import re


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def is_wanted_name(name):
    return bool(normalize(name))


def keyword_group(name):
    """Group category-page SKUs for readable reports; never decide inclusion."""
    words = set(re.findall(r"[a-z]+", normalize(name).lower()))
    if words.intersection({"prawn", "prawns", "shrimp"}):
        return "Prawns & Shrimp"
    if words.intersection({"squid", "calamari", "octopus"}):
        return "Squid & Calamari"
    if words.intersection({"mussel", "mussels", "scallop", "scallops", "oyster", "oysters"}):
        return "Shellfish"
    return "Fish & Other Seafood"


def is_allowed_product(name, brand=""):
    # Membership of the supplied retailer category pages is the inclusion rule.
    return bool(normalize(name))


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
