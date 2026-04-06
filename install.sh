#!/usr/bin/env sh
set -eu

REPO="${ORCHE_INSTALL_REPO:-parkgogogo/tmux-orche}"
PREFIX="${ORCHE_INSTALL_PREFIX:-$HOME/.local/bin}"
BIN_NAME="orche"

xdg_data_home() {
  printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}"
}

INSTALL_ROOT="${ORCHE_INSTALL_ROOT:-$(xdg_data_home)/orche/releases}"
METADATA_PATH="${ORCHE_INSTALL_METADATA_PATH:-$(xdg_data_home)/orche/install.json}"

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

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

resolve_payload() {
  payload_root="$1"
  if [ -x "${payload_root}/${BIN_NAME}/${BIN_NAME}" ]; then
    printf '%s\n' "${payload_root}/${BIN_NAME}"
    return
  fi
  if [ -f "${payload_root}/${BIN_NAME}" ]; then
    legacy_dir="${payload_root}/.orche-legacy"
    mkdir -p "${legacy_dir}"
    cp "${payload_root}/${BIN_NAME}" "${legacy_dir}/${BIN_NAME}"
    chmod 755 "${legacy_dir}/${BIN_NAME}"
    printf '%s\n' "${legacy_dir}"
    return
  fi
  fail "archive did not contain ${BIN_NAME} or ${BIN_NAME}/${BIN_NAME}"
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
  need_cmd cp
  need_cmd ln
  need_cmd sed

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
  source_dir="$(resolve_payload "${tmpdir}")"
  executable_path="${source_dir}/${BIN_NAME}"
  [ -x "${executable_path}" ] || fail "archive did not contain executable ${BIN_NAME}"

  release_dir="${INSTALL_ROOT}/${version}/${target}"
  rm -rf "${release_dir}"
  mkdir -p "$(dirname "${release_dir}")"
  cp -R "${source_dir}" "${release_dir}"

  link_path="${PREFIX}/${BIN_NAME}"
  if [ -d "${link_path}" ] && [ ! -L "${link_path}" ]; then
    fail "refusing to replace directory at ${link_path}"
  fi
  rm -f "${link_path}"
  ln -s "${release_dir}/${BIN_NAME}" "${link_path}"

  mkdir -p "$(dirname "${METADATA_PATH}")"
  metadata_tmp="${tmpdir}/install.json"
  cat > "${metadata_tmp}" <<EOF
{
  "channel": "prebuilt-binary",
  "repo": "$(json_escape "${REPO}")",
  "version": "$(json_escape "${version}")",
  "target": "$(json_escape "${target}")",
  "prefix": "$(json_escape "${PREFIX}")",
  "link_path": "$(json_escape "${link_path}")",
  "install_root": "$(json_escape "${INSTALL_ROOT}")",
  "executable_path": "$(json_escape "${release_dir}/${BIN_NAME}")"
}
EOF
  install -m 644 "${metadata_tmp}" "${METADATA_PATH}"

  say "Installed ${BIN_NAME} ${version} to ${link_path}"
  say "Runtime files: ${release_dir}"
  case ":${PATH}:" in
    *":${PREFIX}:"*) ;;
    *)
      say "Note: ${PREFIX} is not on PATH. Add it before running ${BIN_NAME}."
      ;;
  esac
}

main "$@"
