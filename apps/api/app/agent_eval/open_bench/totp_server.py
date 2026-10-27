"""A two-step-verification page with ONE code field, in three seed variants (stdlib only).

    GET  /totp/<variant>          the second step of a login: a single "Verification code" field and Verify
    POST /totp/<variant>/verify   {"code": "123456"} -> {"ok": true, "password": ...} only for a current TOTP code

Variants (which only differ in where the authenticator key is):
    vault   the page shows no key; the runner stores it in the credential vault for this origin
    goal    the page shows no key; the task text carries it
    page    the page prints it ("Can't scan? Secret key: ...") and the task tells the agent to use it

The seed and the password are random per server (the runner starts a fresh server per task), the code is checked
server-side (SHA1, 30 s, 6 digits, one step either side), and the password appears only in the response to a valid
code, so grading by "the password is in the answer" cannot be satisfied without a computed code. There is no
other control to confuse: the field is a plain text input, not split boxes.
"""

from __future__ import annotations

import base64
import os
import secrets
import time
import urllib.parse

from app import totp
from app.agent_eval.open_bench import bench_common as bc

VARIANTS = ("vault", "goal", "page")

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Two-step verification</title>
<style>body{{font:16px system-ui,sans-serif;max-width:30rem;margin:3rem auto;padding:0 1rem}}
label{{display:block;margin:1rem 0 .3rem}}input{{font-size:1.2rem;padding:.4rem;width:10rem;letter-spacing:.2rem}}
button{{font-size:1rem;padding:.5rem 1rem;margin-left:.5rem}}.key{{font-family:monospace;word-break:break-all}}
.err{{color:#b00020}}.ok{{color:#0a6b2b;font-weight:600}}</style></head>
<body>
<h1>Two-step verification</h1>
<p>Signed in as <b>a.rivera@{host}</b>. One more step to finish signing in.</p>
{key_block}
<form id="f" onsubmit="return false">
<label for="otp">Verification code</label>
<input id="otp" name="otp" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="6-digit code">
<button id="verify" type="submit">Verify</button>
</form>
<p id="result" role="status"></p>
<script>
const form = document.getElementById('f'), out = document.getElementById('result');
async function submit() {{
  const code = document.getElementById('otp').value.trim();
  const r = await fetch('/totp/{variant}/verify', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{code}})}});
  const d = await r.json();
  if (d.ok) {{ out.className = 'ok'; out.textContent = 'Verified. Your password is ' + d.password; }}
  else {{ out.className = 'err'; out.textContent = 'That code is not valid. Try again.'; }}
}}
document.getElementById('verify').addEventListener('click', submit);
form.addEventListener('submit', submit);
</script>
</body></html>
"""

_KEY_BLOCK = (
    '<p>Add this account to your authenticator app. Can\'t scan a QR code? Secret key: '
    '<span class="key" id="seed">{seed}</span></p>'
)


def new_seed() -> str:
    return base64.b32encode(os.urandom(20)).decode()


class Handler(bc.QuietHandler):
    state: dict  # variant -> {"seed", "password"}

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        variant = path.removeprefix("/totp/")
        if not path.startswith("/totp/") or variant not in VARIANTS:
            self.send_bytes(404, b"not found", "text/plain")
            return
        seed = self.state[variant]["seed"]
        key_block = _KEY_BLOCK.format(seed=" ".join(seed[i : i + 4] for i in range(0, len(seed), 4))) if variant == "page" else ""
        host = self.headers.get("Host", "localhost")
        self.send_bytes(200, _PAGE.format(variant=variant, key_block=key_block, host=host).encode("utf-8"), "text/html; charset=utf-8")

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        parts = path.split("/")
        if len(parts) != 4 or parts[1] != "totp" or parts[2] not in VARIANTS or parts[3] != "verify":
            self.send_bytes(404, b"not found", "text/plain")
            return
        entry = self.state[parts[2]]
        code = str(self.read_json().get("code") or "").strip()
        params = totp.parse_seed(entry["seed"])
        now = time.time()
        valid = bool(
            params
            and len(code) == 6
            and code.isdigit()
            and any(
                secrets.compare_digest(code, totp.totp_code(params, at=now + step * params.period))
                for step in (-1, 0, 1)
            )
        )
        if valid:
            entry["passed"] = True
        else:
            entry["wrong_attempts"] = int(entry.get("wrong_attempts") or 0) + 1
        self.send_json({"ok": True, "password": entry["password"]} if valid else {"ok": False})


def start_server() -> tuple[bc.BenchServer, dict]:
    """A fresh server with fresh seeds and passwords; the returned dict is its live state (graders read it)."""
    state = {variant: {"seed": new_seed(), "password": f"TOTP-{secrets.token_hex(4).upper()}"} for variant in VARIANTS}
    server = bc.BenchServer(Handler, state=state).start()
    return server, state
