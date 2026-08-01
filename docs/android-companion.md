# Android companion integration

Dikte is a Linux desktop application, but its CLI and agent workflow can be the
processing side of a lightweight Android voice inbox. Keeping audio capture
native to Android preserves microphone, power-management and accessibility
integration while the existing computer runs Codex CLI and project-aware tools.

## Reference flow

1. An Android speech-recognition service produces a transcript on the phone.
2. The phone shows the raw transcript before taking any action.
3. A small SSH command sends the text to a signed-in Codex CLI on the user's
   Dikte computer over Tailscale.
4. Codex returns a cleaned title, text, suggested destination and explanation.
5. The phone asks the user to choose a destination before writing anything.

DAAK NODE implements this pattern with four destinations: daakREMEMBER,
Obsidian, both stores, or an interactive Codex CLI session. The Android side can
queue a confirmed daakREMEMBER capture while the computer is offline.

## Why use the desktop CLI

- The phone contains no OpenAI API key.
- The existing signed-in Codex CLI session, tools and project directories stay
  on the computer.
- Tailscale SSH avoids exposing a shell to the public internet.
- Android handles the microphone and confirmation UI, so the PyQt desktop app
  does not need to be ported or kept alive on the phone.

## Security checklist

- Restrict SSH to the tailnet and use key-based authentication.
- Allow only a small launcher script with explicit working-directory choices.
- Base64 is transport encoding, not encryption; rely on SSH for confidentiality.
- Validate decoded paths against an allowlist.
- Never write or send on the user's behalf before showing the final destination.
- Keep offline queues private to the Android app and retry with bounded backoff.

The companion is deliberately separate from Dikte's desktop runtime. It is an
integration example for contributors, not an Android build artifact.
