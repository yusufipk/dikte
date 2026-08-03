"""The four global shortcuts, named once.

There are four of them and the command line, the settings window, the installer
and both platform adapters each used to want their own copy of the list. The
id is what every platform files a shortcut under: a .desktop file name on
Linux, and on Windows just the key the registration is remembered by.
"""

import collections

DESKTOP_ID = "dikte-toggle.desktop"
CANCEL_DESKTOP_ID = "dikte-cancel.desktop"
MEETING_DESKTOP_ID = "dikte-meeting.desktop"
ASK_DESKTOP_ID = "dikte-ask.desktop"

Shortcut = collections.namedtuple("Shortcut", "verb desktop_id name setting fallback")

# `fallback` is what to register when the setting is empty: only the toggle has
# one, since it is the key the application is unusable without.
SHORTCUTS = {
    "toggle": Shortcut("toggle", DESKTOP_ID, "Dikte: start/stop recording",
                       "shortcut", "Ctrl+Space"),
    "cancel": Shortcut("cancel", CANCEL_DESKTOP_ID, "Dikte: discard the recording",
                       "cancel_shortcut", ""),
    "ask": Shortcut("ask", ASK_DESKTOP_ID, "Dikte: ask Claude Code",
                    "assistant_shortcut", ""),
    "meeting": Shortcut("meeting", MEETING_DESKTOP_ID,
                        "Dikte: start/end a meeting recording",
                        "meeting_shortcut", ""),
}
