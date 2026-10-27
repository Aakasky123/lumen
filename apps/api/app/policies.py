from dataclasses import dataclass


@dataclass(frozen=True)
class SafetyPolicy:
    require_human_final_submit: bool = True
    unsupported_sites: tuple[str, ...] = ("workday", "linkedin", "indeed", "taleo", "governmentjobs")


def classify_site_risk(url: str) -> str:
    lowered = url.lower()
    if any(site in lowered for site in SafetyPolicy().unsupported_sites):
        return "high"
    if any(site in lowered for site in ("greenhouse", "lever")):
        return "low"
    if "ashby" in lowered:
        return "medium"
    return "medium"
