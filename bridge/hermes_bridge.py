"""
Hermes bridge — async HTTP shim around `hermes chat`.

HA posts a question, gets 202 immediately, and receives the completed answer
later at an HA webhook. Keeps the Echo out of dead air while a run takes 30s+.

Invocation shape:
    hermes chat -Q --query-file - \
        -c voice-<device>-<bucket> --create-if-missing \
        -t <toolsets> --max-turns N --run-budget S \
        --source tool --reasoning <level>

Named threads (-c) replace manual session tracking: the thread name carries the
conversation, so nothing is lost across bridge restarts. The name includes a
time bucket, so context resets on its own without an idle timer.

Run on the host with the hermes CLI.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import shlex
import time
import uuid

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

AUTH_TOKEN = os.environ["HERMES_BRIDGE_TOKEN"]
HA_WEBHOOK_URL = os.environ["HA_WEBHOOK_URL"]
HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")

ALLOWED_DEVICES = {
    d.strip() for d in os.environ.get("ALLOWED_DEVICES", "zeds_office").split(",")
}

# Tool policy for voice-originated runs.
#
# WARNING: unknown toolset names WARN, they do not fail — and the run proceeds
# with default tools. So `-t` only restricts anything once you have confirmed
# the names are valid for your build. `web_search`/`web_extract` are TOOL names,
# not toolset names, and do not work here. Until you have verified real names
# and confirmed the restriction actually bites, treat this as unenforced.
TOOLSETS = os.environ.get("TOOLSETS", "")

MAX_TURNS = int(os.environ.get("MAX_TURNS", "12"))
RUN_BUDGET = int(os.environ.get("RUN_BUDGET", "120"))     # hermes-side ceiling
HARD_TIMEOUT = RUN_BUDGET + 30                            # backstop kill
# Leave empty. `--reasoning low` made this model emit literal tool-call syntax
# ("search(foo) search(bar)") instead of calling tools, and return no answer.
# If you set it at all, test on real questions first.
REASONING = os.environ.get("REASONING", "")

# daily | hourly | none — how often a device's thread starts fresh.
THREAD_BUCKET = os.environ.get("THREAD_BUCKET", "daily")

DEDUP_WINDOW = int(os.environ.get("DEDUP_WINDOW", "10"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
MAX_TEXT_LEN = 2000

VOICE_PREAMBLE = (
    "Answer in 2-3 sentences, plain spoken prose for text-to-speech. "
    "No preamble, no headers, no lists, no markdown. Output only the answer.\n\n"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hermes-bridge")

app = FastAPI(title="Hermes bridge")
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

_recent: dict[tuple[str, str], float] = {}
_inflight = 0
_last: dict[str, dict] = {}     # device -> last result, for /health and debugging


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _thread_name(device_id: str, override: str | None = None) -> str:
    """Name the hermes session. The time bucket is what resets context."""
    if override:
        return override
    now = dt.datetime.now()
    if THREAD_BUCKET == "hourly":
        suffix = now.strftime("%Y%m%d-%H")
    elif THREAD_BUCKET == "none":
        suffix = "persistent"
    else:
        suffix = now.strftime("%Y%m%d")
    return f"voice-{device_id}-{suffix}"


def _is_duplicate(device_id: str, text: str) -> bool:
    now = time.monotonic()
    for k, ts in list(_recent.items()):
        if now - ts > DEDUP_WINDOW:
            del _recent[k]
    key = (device_id, text)
    if key in _recent:
        return True
    _recent[key] = now
    return False


# Lines hermes may emit around the answer. Dropped wherever they appear, in
# case they land on stdout rather than stderr — otherwise TTS reads them aloud.
_NOISE_PREFIXES = (
    "Warning:",
    "Unknown toolsets",
    "Session ",
    "Initializing agent",
)

# Degraded output: the model printing tool-call syntax as text instead of
# calling tools. Seen with --reasoning low. Never speak this.
_TOOLCALL_RE = re.compile(r"^\s*\w+\([^)]*\)(\s+\w+\([^)]*\))*\s*$")


def _parse_output(stdout: str) -> tuple[str, str | None]:
    """Return the answer, plus session_id if it ever shows up on stdout.

    In testing, hermes puts warnings, the session notice, and `session_id:` on
    STDERR — stdout carries only the answer. The stripping below is a guard in
    case that changes, and only applies to leading lines so a real answer that
    happens to begin with "Warning:" is not eaten.
    """
    session_id = None
    kept: list[str] = []
    for line in stdout.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("session_id:"):
            session_id = stripped.split(":", 1)[1].strip()
            continue
        if not kept and stripped.startswith(_NOISE_PREFIXES):
            continue
        kept.append(line)
    return "\n".join(kept).strip(), session_id


def _is_usable(answer: str) -> bool:
    """Reject empty or degraded output rather than speaking it."""
    if len(answer) < 2:
        return False
    if _TOOLCALL_RE.match(answer):
        return False
    return True


def _build_cmd(thread: str) -> list[str]:
    cmd = [
        HERMES_BIN, "chat",
        "-Q",                       # programmatic output only
        "--query-file", "-",        # question on stdin, nothing shell-interpreted
        "-c", thread,
        "--create-if-missing",      # make the thread on first use
        "--max-turns", str(MAX_TURNS),
        "--run-budget", str(RUN_BUDGET),
        "--source", "tool",         # keep voice runs out of CLI session lists
    ]
    if TOOLSETS:
        cmd += ["-t", TOOLSETS]
    if REASONING:
        cmd += ["--reasoning", REASONING]
    return cmd


async def _run_hermes(question: str, thread: str) -> tuple[str, str | None]:
    cmd = _build_cmd(thread)
    log.info("running: %s", shlex.join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(question.encode()), timeout=HARD_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"hermes exceeded hard timeout {HARD_TIMEOUT}s")

    if proc.returncode != 0:
        raise RuntimeError(
            f"hermes exited {proc.returncode}: {err.decode(errors='replace')[:500]}"
        )

    # Warnings ("Unknown toolsets: ...") go to stderr, so stdout stays clean.
    stderr_text = err.decode(errors="replace").strip()
    if stderr_text:
        log.debug("hermes stderr: %s", stderr_text[:500])

    return _parse_output(out.decode(errors="replace"))


async def _post_to_ha(payload: dict) -> None:
    delay = 1.0
    async with httpx.AsyncClient(timeout=10) as client:
        for attempt in range(4):
            try:
                r = await client.post(HA_WEBHOOK_URL, json=payload)
                r.raise_for_status()
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("webhook attempt %d failed: %s", attempt + 1, exc)
                await asyncio.sleep(delay)
                delay *= 2
    log.error("giving up delivering %s to HA", payload.get("request_id"))


async def _handle(
    request_id: str, text: str, device_id: str, thread: str
) -> None:
    global _inflight
    async with _semaphore:
        _inflight += 1
        started = time.monotonic()
        try:
            answer, session_id = await _run_hermes(VOICE_PREAMBLE + text, thread)
            if not _is_usable(answer):
                raise RuntimeError(f"unusable output: {answer[:200]!r}")
            payload = {
                "type": "reply",
                "request_id": request_id,
                "device_id": device_id,
                "text": answer,
                "session_id": session_id,
                "duration": round(time.monotonic() - started, 1),
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("run failed")
            payload = {
                "type": "error",
                "request_id": request_id,
                "device_id": device_id,
                "text": "Sorry, I couldn't get an answer.",
                "detail": str(exc)[:500],
            }
        finally:
            _inflight -= 1
        _last[device_id] = {
            "request_id": request_id,
            "type": payload["type"],
            "duration": payload.get("duration"),
            "thread": thread,
        }
        await _post_to_ha(payload)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_LEN)
    device_id: str
    thread: str | None = None       # override the auto-named thread


def _check_auth(authorization: str | None) -> None:
    if authorization != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


@app.post("/ask", status_code=202)
async def ask(
    req: AskRequest,
    background: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    _check_auth(authorization)

    if req.device_id not in ALLOWED_DEVICES:
        raise HTTPException(status_code=403, detail="device not allowed")

    if _is_duplicate(req.device_id, req.text):
        log.info("duplicate suppressed from %s", req.device_id)
        return {"request_id": None, "duplicate": True}

    request_id = uuid.uuid4().hex[:12]
    thread = _thread_name(req.device_id, req.thread)
    background.add_task(_handle, request_id, req.text, req.device_id, thread)
    return {"request_id": request_id, "thread": thread}


@app.get("/health")
async def health():
    # No auth, and deliberately no conversation content.
    return {
        "ok": True,
        "inflight": _inflight,
        "toolsets": TOOLSETS or "(config default)",
        "max_turns": MAX_TURNS,
        "run_budget": RUN_BUDGET,
        "thread_bucket": THREAD_BUCKET,
        "last": _last,
    }
