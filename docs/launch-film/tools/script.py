"""The film's narration, scene by scene.

Every spoken claim here was checked against the code on main (5c0744a), the run database, or the
benchmark artifacts on 2026-10-01. `gap` is the silence after a line; `lead`/`tail` pad a scene.
"""

import json
import pathlib

SCENES = [
    {
        "id": "cold", "chapter": "", "lead": 2.4, "tail": 1.2,
        "lines": [
            ("c01", "Most of what we do on a computer isn't hard. It's just endless.", 0.9),
            ("c02", "Teaching an AI to click through it is the easy part.", 0.55),
            ("c03", "The hard part is knowing what actually happened.", 0.2),
        ],
    },
    {
        "id": "meet", "chapter": "", "lead": 2.6, "tail": 1.0,
        "lines": [
            ("m01", "This is Lumen.", 0.9),
            ("m02", "A computer-use agent that works in your own Chrome, signed in as you, and across your Windows desktop.", 0.6),
            ("m03", "You describe the outcome. Lumen works toward it one verified step at a time.", 0.2),
        ],
    },
    {
        "id": "brain", "chapter": "01 · Brain and body", "lead": 1.0, "tail": 0.9,
        "lines": [
            ("b01", "It's built in two halves.", 0.45),
            ("b02", "The brain is a vision model. Each turn, it looks at the screen and chooses one next action. Any capable model can take that seat.", 0.55),
            ("b03", "The body is everything that touches the real world: a Chrome extension inside your browser, and a Windows body with a real mouse and keyboard. One task can move between them.", 0.6),
            ("b04", "The body enumerates. The brain interprets. Deterministic code binds, executes, and verifies.", 0.5),
            ("b05", "No website-specific scripts. One general loop.", 0.2),
        ],
    },
    {
        "id": "loop", "chapter": "02 · The loop", "lead": 1.0, "tail": 0.6,
        "lines": [
            ("l01", "Here's one step of a job application.", 0.4),
            ("l02", "Observe: the screen, the page text, and a numbered inventory of every control.", 0.45),
            ("l03", "Decide: type the phone number into control six, and quote where that number came from.", 0.45),
            ("l04", "Check: the runtime finds that exact quote in your profile, confirms the target, and screens the value for secrets.", 0.45),
            ("l05", "Act: one dispatch, on that exact field.", 0.45),
            ("l06", "Verify: read it back from the page. Only then does the step count.", 0.2),
        ],
    },
    {
        "id": "receipts", "chapter": "03 · Receipts", "lead": 0.9, "tail": 0.7,
        "lines": [
            ("r01", "Every action leaves a receipt: attempted, executed, verified. The runtime computes the outcome. The model never grades its own work.", 0.55),
            ("r02", "Opening a menu is verified, but it isn't progress. And when an outcome is uncertain, Lumen looks again instead of clicking twice.", 0.2),
        ],
    },
    {
        "id": "ground", "chapter": "04 · Grounded, or asked", "lead": 0.9, "tail": 0.8,
        "lines": [
            ("g01", "Lumen doesn't guess facts about you.", 0.45),
            ("g02", "If a required question has no grounded answer, it comes to you. Optional ones are left alone.", 0.5),
            ("g03", "Tell it to remember, and the answer goes into a memory file you can read and edit.", 0.2),
        ],
    },
    {
        "id": "floors", "chapter": "05 · The floors", "lead": 0.9, "tail": 0.8,
        "lines": [
            ("f01", "Some lines aren't settings. They're floors.", 0.6),
            ("f02", "The model can never authorize a final action. Before every click, a separate judge asks whether it's consequential, and if it isn't confident, the click is treated as final.", 0.55),
            ("f03", "In Safe approvals, the default, Lumen stops and asks you, bound to that exact button.", 0.5),
            ("f04", "Full autonomy is your call. Even then, it fires once, and verifies.", 0.7),
            ("f05", "Password and one-time-code fields are blacked out before the model sees the screen.", 0.45),
            ("f06", "And on the desktop: no shells, a blue frame whenever Lumen has control, and one switch to pause it.", 0.2),
        ],
    },
    {
        "id": "cross", "chapter": "06 · Beyond the browser", "lead": 0.9, "tail": 0.8,
        "lines": [
            ("x01", "The loop doesn't know it's filling a form.", 0.5),
            ("x02", "Here, it relays between a web dispatch page and a native Windows app. It reads a work order from one, enters it in the other, refuses a dialog that would overwrite the server's copy, and carries a seal across by clipboard.", 0.2),
        ],
    },
    {
        "id": "proof", "chapter": "07 · Proof", "lead": 0.9, "tail": 0.8,
        "lines": [
            ("p01", "To find what breaks it, we built the Interaction Gauntlet: twenty-eight hostile pages and missions.", 0.4),
            ("p02", "Labels that lie. Instructions planted in the page. Keyboards that drop keystrokes. Dialogs you have to refuse.", 0.55),
            ("p03", "With a production model as its brain, Lumen has passed each of the twenty-one in-browser pages, and six of the seven cross-surface missions, though not yet on every attempt.", 0.55),
            ("p04", "On real job sites, it has submitted applications end to end: one final click, confirmed by the site itself. And real failures become permanent regression tests.", 0.2),
        ],
    },
    {
        "id": "honest", "chapter": "08 · Where it stands", "lead": 1.0, "tail": 0.9,
        "lines": [
            ("h01", "It isn't finished.", 0.6),
            ("h02", "On OS World, the standard desktop benchmark, Lumen scores thirty percent, on a model that costs cents per task. The leaders score above eighty.", 0.6),
            ("h03", "But seat a frontier model in the same body, and Lumen passed all eight of the pages it had struggled with most, including the one it had failed thirty-one times.", 0.55),
            ("h04", "On those pages, what was missing was the brain. And brains keep getting better.", 0.2),
        ],
    },
    {
        "id": "close", "chapter": "", "lead": 1.2, "tail": 6.5,
        "lines": [
            ("z01", "Lumen is built so that every gain lands in an agent that acts carefully, proves what it did, and asks when it should.", 1.3),
            ("z02", "Lumen.", 0.2),
        ],
    },
]


def main() -> None:
    out = pathlib.Path(__file__).resolve().parent.parent / "build" / "script.json"
    out.write_text(json.dumps({"voice": "en-US-AndrewMultilingualNeural", "scenes": SCENES}, indent=1), encoding="utf-8")
    words = sum(len(text.split()) for scene in SCENES for _, text, _ in scene["lines"])
    print(f"wrote {out} - {words} words in {sum(len(s['lines']) for s in SCENES)} lines")


if __name__ == "__main__":
    main()
