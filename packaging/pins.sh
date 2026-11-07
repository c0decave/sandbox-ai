# shellcheck shell=bash
# The third-party downloads of the AppImage (scripts/build_appimage.sh) and of the macOS app
# (scripts/build_macos_app.sh), each pinned by release, exact file name and SHA-256. The PySide6 wheels are
# pinned in requirements-app.txt (pip --require-hashes).
#
# The pinning rule: a release is pinned only once it is at least 14 days old; its publish date (GitHub's
# published_at, PyPI's upload_time) stands beside it. Pinned on 2026-11-06. tests/test_dist_packaging.py checks
# that every pin has its SHA-256 and its date, and that each date lies 14 days or more before PINNED_ON.
PINNED_ON=2026-11-06

# Python: astral-sh/python-build-standalone, release 20261021, published 2026-10-21T19:23:37Z.
# The SHA-256 values are the release's own SHA256SUMS (and GitHub's asset digests); both agree.
PBS_RELEASE=20261021
PBS_PUBLISHED=2026-10-21
PBS_BASE=https://github.com/astral-sh/python-build-standalone/releases/download/20261021
PBS_PYTHON=3.13
PBS_LINUX_X86_64=cpython-3.13.17+20261021-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
PBS_LINUX_X86_64_SHA256=f829adc9d888e76c8801b4799ed444188d35239fcae3a9808a99ec5cb0d8d92c
PBS_LINUX_AARCH64=cpython-3.13.17+20261021-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz
PBS_LINUX_AARCH64_SHA256=c6857c65b996701a5da104cbb49b35249c0c026dc0664239b04f7156aa88326a
PBS_MACOS_ARM64=cpython-3.13.17+20261021-aarch64-apple-darwin-install_only_stripped.tar.gz
PBS_MACOS_ARM64_SHA256=8dc60610a82f1e73be7faf964383e02520f95f6e39592a5c37c3809f5a7b798e
PBS_MACOS_X86_64=cpython-3.13.17+20261021-x86_64-apple-darwin-install_only_stripped.tar.gz
PBS_MACOS_X86_64_SHA256=ce9c748c8f7525c0052247edb2435fdb1cac89c77e687e0e199eaf2b827668c2

# appimagetool: AppImage/appimagetool, release 1.9.1 (not "continuous"), published 2025-11-18T17:47:57Z.
APPIMAGETOOL_RELEASE=1.9.1
APPIMAGETOOL_PUBLISHED=2025-11-18
APPIMAGETOOL_BASE=https://github.com/AppImage/appimagetool/releases/download/1.9.1
APPIMAGETOOL_X86_64=appimagetool-x86_64.AppImage
APPIMAGETOOL_X86_64_SHA256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
APPIMAGETOOL_AARCH64=appimagetool-aarch64.AppImage
APPIMAGETOOL_AARCH64_SHA256=f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158

# The AppImage runtime (the start of the AppImage file): appimagetool would fetch the "continuous" one, so it
# gets this one with --runtime-file. AppImage/type2-runtime, release 20251108, published 2025-12-08T17:50:07Z.
RUNTIME_RELEASE=20251108
RUNTIME_PUBLISHED=2025-12-08
RUNTIME_BASE=https://github.com/AppImage/type2-runtime/releases/download/20251108
RUNTIME_X86_64=runtime-x86_64
RUNTIME_X86_64_SHA256=2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d
RUNTIME_AARCH64=runtime-aarch64
RUNTIME_AARCH64_SHA256=00cbdfcf917cc6c0ff6d3347d59e0ca1f7f45a6df1a428a0d6d8a78664d87444
