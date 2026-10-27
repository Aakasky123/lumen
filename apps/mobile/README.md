# Lumen for Android

A remote for Lumen on your PC. Start tasks, follow them live, answer Lumen's questions, and approve
final actions from your phone. Everything still runs on the PC; the phone talks to the backend's
paired-device listener over your Tailscale network (see `docs/architecture.md`, section 8, Remote
access).

## What you need

- Lumen running on the PC, with **Settings → Devices** showing Phone access ready
  (`tailscale serve --bg --https=443 http://127.0.0.1:8002`).
- Tailscale on the phone, signed in to the same tailnet.
- A fingerprint or face unlock on the phone. Pairing creates a key in the Android Keystore that only
  signs after a strong biometric check; Lumen asks for it before payments, sign-in saves, and setting
  changes.
- `google-services.json` for the Firebase app `ai.lumen.remote` in this folder (git-ignored) for alerts.

## Build and install

From `apps/mobile` (Windows, Android SDK at `%LOCALAPPDATA%\Android\Sdk`, JDK 17):

```bash
npm install
npx expo prebuild --platform android --clean
cd android && ./gradlew assembleRelease
```

The APK lands in `android/app/build/outputs/apk/release/app-release.apk`. Install it with
`adb install -r` or copy it to the phone. Release builds are signed with the debug keystore, which
is fine for sideloading on your own phone.

`android/` is generated (continuous native generation) and not checked in; app config lives in
`app.config.ts`, `plugins/`, and the local module `modules/lumen-keys`.

## Develop

```bash
npx expo run:android      # debug build on a device or emulator, with Metro
npm run typecheck
npm test                  # pure-logic tests (node --test)
```

The emulator reaches the PC at `10.0.2.2`, and the app accepts plain HTTP only to that address when
running on an emulator. To pair an emulator without Tailscale, open a pairing link that points at
the remote listener directly:

```bash
adb shell am start -a android.intent.action.VIEW -d "lumen://pair?v=1&url=http%3A%2F%2F10.0.2.2%3A8002&code=<code>"
```

where `<code>` comes from `POST /devices/pairing-codes` on the PC (the Devices panel shows the same
code inside its QR).

## Layout

- `app/` screens (expo-router): `pair`, `index` (chats), `chat/[id]`, `live/[runId]`, `run/[id]`
  (alert landing), `settings/*` (profile, documents, sign-ins).
- `src/api.ts` the backend client: device token, SSE chat stream, step-up signing.
- `src/hooks.ts` polling, the chat-thread state (mirrors the cockpit's `useChat`), approval mode.
- `src/components/chat/` message, question, run dock, secure-entry, and composer components.
- Shared with the cockpit, imported straight from `apps/renderer/lib`: `runPhase.ts`, `threads.ts`,
  `format.ts`, `pendingQuestionConfirmation.ts`, and the API types in `api.ts`.
