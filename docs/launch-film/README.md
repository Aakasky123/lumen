# Lumen launch film

A 4:31 narrated explainer of Lumen as the system stood on October 1, 2026. The rendered video is not
committed (see `artifacts/launch-film/`, which is gitignored); this folder holds everything needed to
rebuild it.

## How it is made

The film is one HTML page (`src/film.html` plus `src/*.js`). Every visual is a pure function of time,
so the same page plays live against the soundtrack and renders frame-exact for video.

| Step | Tool | Output |
|---|---|---|
| Narration text | `tools/script.py` (edit lines here) | `build/script.json` |
| Voice | `tools/make_vo.py` (edge-tts, `en-US-AndrewMultilingualNeural`; needs network) | `build/vo/*.wav` + word timings |
| Timing | `tools/build_timeline.py` (scene cuts snap to a 96 BPM beat grid) | `build/timeline.json`, `src/timeline.js` |
| Sound cues | `tools/export_cues.py` (reads `FILM.collectCues()` from the page) | `build/cues.json` |
| Score, effects, mix | `tools/make_audio.py` (numpy/scipy synthesis; mix at -16 LUFS, -1.5 dBFS peak) | `src/soundtrack.mp3`, `out/soundtrack.m4a` |
| Picture | `tools/render.py video --fps 60 --workers 8` (Playwright frames into x264) | `out/video_noaudio.mp4` |
| Finish | `tools/finalize.py <folder>` | 60 fps master MP4, 30 fps share and captioned MP4s, 720p phone cut (under 30 MiB), SRT captions, soundtrack |

Review helpers: `tools/render.py stills 12.5 40` writes PNGs; `tools/contact.py 0 60 1 sheet` writes a
contact sheet. Scenes anchor their motion to spoken words (`WD('l03', 'quote')`), so editing a line and
re-running the pipeline keeps picture, effects, and narration in sync.

Requirements: Python with `playwright` (Chromium installed), `imageio-ffmpeg`, `numpy`, `scipy`,
`Pillow`, `edge-tts`.

## Where the facts come from

Every claim was checked on 2026-10-01/02 against `main` at 5c0744a, the run database, or eval artifacts.

| Claim in the film | Source |
|---|---|
| 22-action vocabulary; `then` holds at most 2 follow-ups; no approval field for the model | `apps/api/app/vision_agent.py` (`ACTIONS`, `action_schema`) |
| Click-consequence judge before every click; below 0.85 confidence the click is treated as final | `apps/api/app/extension_run.py` (`_judge_click_consequence`), `lumen_model.py` |
| Grounding citations checked against the cited source; required unknowns asked, optional unknowns left alone | `extension_run.py` (`_vision_grounding_citation_check`), `vision_agent.py` prompt |
| Receipt axes and computed outcomes | `apps/api/app/operation_receipt.py`; receipt shape from `cognitive_events` (`OPERATION_RECEIPT`) |
| "Submitted" claims need a verified receipt | `apps/api/app/claim_validator.py` (strips unsupported run-lifecycle claims) |
| Password / one-time-code masking; credential replay the model never sees | `apps/extension/src/background.js` (`maskSensitiveScreenshotRegionsInPage`), `credential_vault.py` |
| Safe approvals (default) vs Full autonomy; approval card and mode copy | `agent_settings.py`, `apps/renderer/components/lumen/chat/ActionApprovalSelector.tsx`, `extension_run.py` |
| No shells; blue frame and live pill; Pause computer | `apps/api/app/desktop_body.py`, `apps/desktop/control_indicator.mjs`, `DesktopHostBar.tsx` |
| One task can switch between Chrome and Windows | `apps/api/app/surface_body.py`, `switch_surface` |
| Memory file format and reply wording | `apps/api/app/user_memory.py`, `cognition.py` |
| 17 brains have driven recorded runs | `VISION_AGENT_DECISION` model metadata: GLM 5.3 Flash (11,284 decisions), GPT-6 Luna (5,756), MiMo V2.5, Gemini 2.5 Flash (runs 483, 493), MiniMax M3, GPT-5.6 Luna, Qwen 3.8 Flash, Gemini 3.8 Flash, Qwen 3.7 Flash, DeepSeek V4.1 Flash, MiMo V2.6 Flash, Jev router, GPT-5.5, Qwen 3.5 9B (run 499), Kimi K2.6, Mistral Small, and the external brain seat (Opus 5.5) |
| Gauntlet: 28 pages and missions, 133 challenges | `apps/api/app/agent_eval/interaction_bench/README.md` |
| 21/21 in-browser pages and 6/7 cross-surface missions passed, with passes/attempts per page | `storage/artifacts/eval_runs/g6-*/scorecard.json` (GPT-6 Luna, Sept 26-28, 2026) |
| GLM 5.3 Flash re-run on 4 pages: 52/60 passes | gauntlet runs 853-876 |
| Cross-surface scene thoughts | `LUMEN_PLANNER_STEP` events of run 724 (`v3_handoff`), quoted as logged |
| Frontier brain seat passed the 8 hardest pages, v3_paperwork after 0/31 | runs 1516-1524 (Oct 1, 2026), commit f4e6af8 |
| Real applications: Greenhouse run 483, Ashby run 493, Hirebridge runs 498/499 | `apps/api/tests/fixtures/capability_runs/*.json` |
| OSWorld 30.1% (110.99/369), per-domain rates, miss reasons, climb 21.0 to 33.0 on 79 tasks | `storage/artifacts/eval_runs/osworld-all-base/summary.json`, `docs/osworld-hill-climb-log.md` (branch `osworld-brain`) |
| Leading agents about 86% | OSWorld-Verified leaderboard, September 2026 |
| 1,330 backend and 222 extension tests passing | run on `main` (5c0744a), October 2, 2026 |

## What is dramatized

- The Acme Robotics application, applicant "Jordan Avery", phone numbers, and the memory-file run ids are
  sample data. The flow (observe, decide with a citation, check, act once, read back, ask for the
  ungrounded required answer, approval card, one activation) follows the real loop.
- Product UI is redrawn from the code, not screen-captured.
- The step-thought pill in the loop scene paraphrases the style of real thoughts; the cross-surface scene
  quotes real ones.
