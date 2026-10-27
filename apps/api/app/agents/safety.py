from app.policies import SafetyPolicy, classify_site_risk


class SafetyGovernor:
    def evaluate_goal(self, job_url: str) -> dict:
        lowered = job_url.lower()
        watchlist = [site for site in SafetyPolicy().unsupported_sites if site in lowered]
        return {
            "risk_level": classify_site_risk(job_url),
            "unsupported_reasons": [],
            "platform_watchlist_reasons": watchlist,
            "allowed_to_automate": True,
            "requires_final_approval": SafetyPolicy().require_human_final_submit,
        }
