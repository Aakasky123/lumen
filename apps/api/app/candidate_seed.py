from __future__ import annotations




CANDIDATE_SEED_PROVENANCE_KEY = "_candidate_seed_provenance"
ONBOARDING_PROVENANCE = "onboarding_default"
ONBOARDING_SOURCE_REF = "runtime.bootstrap_user"


def neutral_onboarding_profile_json() -> dict:
    return {
        CANDIDATE_SEED_PROVENANCE_KEY: {
            "label": ONBOARDING_PROVENANCE,
            "source_ref": ONBOARDING_SOURCE_REF,
        }
    }
