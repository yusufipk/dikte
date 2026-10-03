#!/usr/bin/env bash
# Only inside the disposable container described in README.md.
set -euo pipefail
cd /build
build() { runuser -u builder -- env QT_QPA_PLATFORM=offscreen "$@"; }
case "${1:-}" in
  build)
    build makepkg --printsrcinfo > /tmp/generated.SRCINFO
    diff -u .SRCINFO /tmp/generated.SRCINFO
    build makepkg --cleanbuild --force --noconfirm
    namcap PKGBUILD dikte-*.pkg.tar.zst | tee /tmp/namcap.log
    # namcap's exit code does not report diagnostics.
    ! grep -q ' E: ' /tmp/namcap.log
    ;;
  lifecycle)
    # Run this stage with the container's network disconnected.
    pacman -U --noconfirm dikte-2.0.0-1-any.pkg.tar.zst
    pacman -Qkk dikte
    ! pacman -Qlq dikte | grep -E '^/(home|root|etc)/'
    ! bsdtar -tf dikte-2.0.0-1-any.pkg.tar.zst | grep -x '.INSTALL'
    runuser -u builder -- bash /recipe/smoke.sh
    # A packaging revision exercises a real pacman upgrade without inventing
    # an upstream release or modifying the checked-in recipe.
    sed -i 's/^pkgrel=1$/pkgrel=2/' PKGBUILD
    build makepkg --force --nocheck --noconfirm
    pacman -U --noconfirm dikte-2.0.0-2-any.pkg.tar.zst
    test "$(pacman -Q dikte)" = 'dikte 2.0.0-2'
    pacman -Qkk dikte
    runuser -u builder -- bash /recipe/smoke.sh
    pacman -R --noconfirm dikte
    test ! -e /usr/bin/dikte
    test ! -e /usr/share/applications/dikte.desktop
    test ! -e /usr/share/icons/hicolor/256x256/apps/dikte.png
    test ! -d "$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/dikte"
    test -f /home/builder/smoke/config/dikte/config.json
    echo 'Arch install / revision upgrade / uninstall passed; user config retained.'
    ;;
  *) echo 'usage: validate.sh build|lifecycle' >&2; exit 2 ;;
esac
