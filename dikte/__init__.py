"""Dikte: press a key, talk, press again to transcribe, clean up and paste.

The package is the application. Nothing is imported here on purpose: `dikte
config get` runs through the same package as the tray icon does, and it has no
business loading Qt to answer one question.
"""

# The one place the number is written down. scripts/release.sh rewrites this
# line and tags the commit, the release workflow reads the tag back out, and
# both the .dmg's Info.plist and the AppImage's file name are built from it. A
# build off master rather than off a tag appends the commit to it, so that a
# bug report from someone running "latest" names a commit.
__version__ = "1.2.0"
