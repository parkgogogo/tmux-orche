from __future__ import annotations

import re
from pathlib import Path


def shorten(text: object, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", str(text)).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def longest_common_prefix(before: str, after: str) -> int:
    limit = min(len(before), len(after))
    index = 0
    while index < limit and before[index] == after[index]:
        index += 1
    return index


def turn_delta(before: str, after: str) -> str:
    if before and after and before in after:
        return after.split(before, 1)[1]
    return after[longest_common_prefix(before, after) :]


def extract_summary_candidate(text: str, *, prompt: str = "") -> str:
    lines: list[str] = []
    prompt_inline = compact_text(prompt)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```") or line.startswith(("╭", "╰", "│", "› ", "└ ")):
            continue
        if line.startswith("• "):
            line = line[2:].strip()
            if not line:
                continue
        if re.match(r"^[─━]{6,}$", line) or re.match(r"^[\W_─━]{20,}$", line):
            continue
        if line.startswith(("Tip:", "Command:", "Chunk ID:", "Wall time:", "Output:")):
            continue
        if line in {"Explored", "Ran", "Read", "List", "Updated Plan"}:
            continue
        if line.startswith(("Explored", "Ran ", "Read ", "List ", "Edited ")):
            continue
        if line.startswith(("OpenAI Codex", "dnq@", "^C")) or ("gpt-" in line and "% left" in line):
            continue
        if line.startswith(("session:", "cwd:")):
            continue
        if prompt_inline and (compact_text(line) == prompt_inline or compact_text(line).endswith(prompt_inline)):
            continue
        line = compact_text(line.replace("`", ""))
        if line:
            lines.append(line)
    return lines[-1] if lines else ""


def _is_prompt_fragment(candidate: str, prompt: str) -> bool:
    candidate_inline = compact_text(candidate)
    prompt_inline = compact_text(prompt)
    if not candidate_inline or not prompt_inline:
        return False
    return len(candidate_inline) >= 8 and candidate_inline in prompt_inline


def slugify(text: str) -> str:
    out: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in ("-", "_", "/", "."):
            out.append("-")
    value = "".join(out)
    while "--" in value:
        value = value.replace("--", "-")
    return value.strip("-") or "root"


def repo_name(cwd: Path) -> str:
    return slugify(cwd.resolve().name)


def default_session_name(cwd: Path, agent: str, purpose: str = "main") -> str:
    return f"{repo_name(cwd)}-{slugify(agent)}-{slugify(purpose)}"


def window_name(session: str) -> str:
    return f"orche-{slugify(session)}"


def session_key(session: str) -> str:
    return slugify(session)
