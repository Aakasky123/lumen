from urllib.parse import urlparse


def detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()
    combined = f"{host}{path}"
    if "greenhouse" in combined or "boards.greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in combined or "jobs.lever.co" in host:
        return "lever"
    if "ashby" in combined:
        return "ashby"
    return "generic"
