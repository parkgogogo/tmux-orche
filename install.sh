#!/usr/bin/env sh
set -eu

REPO="${ORCHE_INSTALL_REPO:-parkgogogo/tmux-orche}"
PREFIX="${ORCHE_INSTALL_PREFIX:-$HOME/.local/bin}"
BIN_NAME="orche"

say() {
  printf '%s\n' "$*"
}

fail() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

resolve_version() {
  if [ -n "${ORCHE_INSTALL_VERSION:-}" ]; then
    printf '%s\n' "${ORCHE_INSTALL_VERSION}"
    return
  fi
  need_cmd curl
  version="$(
    curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" |
      sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' |
      head -n 1
  )"
  [ -n "${version}" ] || fail "failed to resolve latest release version"
  printf '%s\n' "${version}"
}

detect_target() {
  os="$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m 2>/dev/null)"

  case "${os}" in
    darwin) os="darwin" ;;
    linux) os="linux" ;;
    *) fail "unsupported operating system: ${os}" ;;
  esac

  case "${arch}" in
    arm64|aarch64)
      arch="arm64"
      ;;
    x86_64|amd64)
      arch="x64"
      ;;
    *)
      fail "unsupported architecture: ${arch}"
      ;;
  esac

  target="${os}-${arch}"
  case "${target}" in
    darwin-arm64|darwin-x64|linux-x64)
      printf '%s\n' "${target}"
      ;;
    *)
      fail "no prebuilt binary is published for ${target}"
      ;;
  esac
}

main() {
  need_cmd curl
  need_cmd tar
  need_cmd mktemp
  need_cmd install

  version="$(resolve_version)"
  target="$(detect_target)"
  archive="orche-${version}-${target}.tar.gz"
  url="https://github.com/${REPO}/releases/download/${version}/${archive}"

  tmpdir="$(mktemp -d)"
  trap 'rm -rf "${tmpdir}"' EXIT INT TERM

  say "Downloading ${archive}..."
  curl -fsSL "${url}" -o "${tmpdir}/${archive}" || fail "download failed: ${url}"

  mkdir -p "${PREFIX}"
  tar -xzf "${tmpdir}/${archive}" -C "${tmpdir}"
  install "${tmpdir}/${BIN_NAME}" "${PREFIX}/${BIN_NAME}"

  say "Installed ${BIN_NAME} ${version} to ${PREFIX}/${BIN_NAME}"
  case ":${PATH}:" in
    *":${PREFIX}:"*) ;;
    *)
      say "Note: ${PREFIX} is not on PATH. Add it before running ${BIN_NAME}."
      ;;
  esac
}

main "$@"
