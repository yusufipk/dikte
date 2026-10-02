# A clean Arch root; pacman updates it to the current repository state.
FROM ghcr.io/archlinux/archlinux@sha256:e1f633ea418483bf7c322a4c7354aa8fa9f6ed48ea7b1239410fac2b4823b566
RUN pacman -Syu --noconfirm --needed base-devel python python-pyqt6 ffmpeg \
    libpulse hicolor-icon-theme desktop-file-utils namcap ttf-dejavu \
    && useradd -m builder
COPY . /recipe/
RUN mkdir /build && cp /recipe/PKGBUILD /recipe/.SRCINFO /recipe/dikte \
    /recipe/dikte.desktop /build/ && chown -R builder:builder /build
WORKDIR /build
