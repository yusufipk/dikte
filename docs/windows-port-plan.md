# Windows port: technical comparison and implementation order

This port targets the current upstream `master` while retaining its Linux and
macOS behavior. Two earlier implementations were reviewed as references:

- PR #3 proves the basic Win32 choices (RegisterHotKey, Unicode clipboard,
  SendInput, DirectShow and Windows subprocess flags), but places platform code
  inside the existing large modules and predates parts of the current design.
- `melih3774/dikte:windows-support` provides the stronger reference: WASAPI and
  loopback capture, DPAPI, per-user IPC, Job Objects, packaging, CI and Windows
  tests. Its branch diverged before upstream's current macOS backend, so it
  cannot be merged without removing or regressing that work.

Implementation will therefore be adapted in small, testable slices:

1. Add Windows AppData paths and per-user IPC naming without changing Linux or
   macOS behavior.
2. Introduce platform contracts for runtime, clipboard, hotkeys and audio;
   move existing Linux/macOS behavior behind them unchanged.
3. Add native Win32 clipboard/SendInput and RegisterHotKey adapters.
4. Add WASAPI microphone and loopback recording, preserving the current PCM
   and meeting contracts.
5. Adapt process lifecycle, DPAPI secrets, local model binaries and packaged
   executable startup.
6. Add Windows packaging/CI, documentation, and complete the manual Windows
   10/11 validation matrix.

Each slice must keep the existing suite green and add Windows-focused contract
tests. Packaging follows functional parity, so build tooling cannot conceal a
runtime regression.
