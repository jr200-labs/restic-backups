"""Transcribe + summarise iCloud Voice Memos into per-memo JSON records.

Run via `uv run restic-backups voice-memos ...` from the repository root.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os

# Force HF downloads to show progress bars.
os.environ.setdefault("HF_HUB_VERBOSITY", "info")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "0")

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

import click
import requests

# Mac absolute time epoch (2001-01-01 UTC) → unix epoch offset
MAC_EPOCH_OFFSET = 978307200

DEFAULT_RECORDINGS_DIR = Path(
    os.path.expanduser(
        "~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
    )
)
DEFAULT_DB = DEFAULT_RECORDINGS_DIR / "CloudRecordings.db"

SUMMARIES_DIR = Path(
    os.environ.get(
        "SUMMARIES_DIR", Path(__file__).resolve().parent.parent / "summaries"
    )
)
INDEX_FILE = SUMMARIES_DIR / "index.json"
PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "summary.md"

PROCESSOR_VERSION = 1


# ---------- data classes ----------


@dataclass
class Memo:
    uuid: str
    title: str
    datetime_iso: str
    duration_seconds: float | None
    location: dict | None
    audio_path: Path
    ios_transcript: str | None
    folder: str | None = None
    modified_iso: str | None = None
    file_size_bytes: int | None = None
    audio_info: dict | None = None  # {sample_rate, channels, codec}


@dataclass
class Record:
    uuid: str
    title: str
    datetime: str
    duration_seconds: float | None
    location: dict | None
    source_path: str
    transcript: dict
    summary: str
    summary_model: str
    tags: list[str]
    audio_sha256: str
    processed_at: str
    folder: str | None = None
    modified: str | None = None
    file_size_bytes: int | None = None
    audio_info: dict | None = None
    processor_version: int = PROCESSOR_VERSION


# ---------- DB ----------


def open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise click.ClickException(
            f"CloudRecordings.db not found at {db_path}. "
            "Open Voice Memos.app and wait for iCloud sync."
        )
    # read-only URI; avoid locking issues if Voice Memos.app is running
    uri = f"file:{db_path}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def column_set(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def pick(cols: set[str], *candidates: str) -> str | None:
    for c in candidates:
        if c in cols:
            return c
    return None


def list_memos(conn: sqlite3.Connection, recordings_dir: Path) -> list[Memo]:
    cols = column_set(conn, "ZCLOUDRECORDING")
    uuid_col = pick(cols, "ZUNIQUEID", "ZIDENTIFIER", "ZENCRYPTEDTITLE")
    title_col = pick(cols, "ZCUSTOMLABELFORSORTING", "ZENCRYPTEDTITLE", "ZCUSTOMLABEL")
    date_col = pick(cols, "ZDATE", "ZCREATIONDATE")
    dur_col = pick(cols, "ZDURATION", "ZAUDIODURATION")
    path_col = pick(cols, "ZPATH", "ZLOCALFILENAME", "ZENCRYPTEDPATH")
    lat_col = pick(cols, "ZLATITUDE")
    lon_col = pick(cols, "ZLONGITUDE")
    loc_name_col = pick(cols, "ZLOCATIONNAME", "ZCUSTOMLOCATION")
    trans_col = pick(cols, "ZTRANSCRIPTION", "ZTRANSCRIPT", "ZTRANSCRIPTIONTEXT")
    folder_col = pick(cols, "ZFOLDER", "ZCLOUDFOLDER")
    mod_col = pick(cols, "ZMODIFICATIONDATE", "ZLASTMODIFIEDDATE")

    if not (uuid_col and date_col and path_col):
        raise click.ClickException(
            f"CloudRecordings.db schema unfamiliar. Found cols: {sorted(cols)}"
        )

    folders = folder_lookup(conn) if folder_col else {}

    select_cols = [
        uuid_col,
        title_col or uuid_col,
        date_col,
        dur_col or "NULL",
        path_col,
        lat_col or "NULL",
        lon_col or "NULL",
        loc_name_col or "NULL",
        trans_col or "NULL",
        folder_col or "NULL",
        mod_col or "NULL",
    ]
    sql = f"SELECT {', '.join(select_cols)} FROM ZCLOUDRECORDING"
    rows = conn.execute(sql).fetchall()

    memos: list[Memo] = []
    for (
        uuid,
        title,
        mac_date,
        duration,
        path,
        lat,
        lon,
        loc_name,
        ios_trans,
        folder_fk,
        mod_date,
    ) in rows:
        if not uuid or not path:
            continue
        audio_path = (recordings_dir / path).resolve()
        if not audio_path.exists():
            # path may be relative bare filename
            alt = recordings_dir / Path(path).name
            if alt.exists():
                audio_path = alt
            else:
                continue
        unix_ts = (mac_date or 0) + MAC_EPOCH_OFFSET
        datetime_iso = dt.datetime.fromtimestamp(unix_ts, dt.UTC).isoformat()
        location = None
        if lat is not None and lon is not None and (lat or lon):
            location = {"lat": float(lat), "lon": float(lon)}
            if loc_name:
                location["name"] = str(loc_name)
        modified_iso = None
        if mod_date is not None:
            modified_iso = dt.datetime.fromtimestamp(
                mod_date + MAC_EPOCH_OFFSET, dt.UTC
            ).isoformat()
        try:
            size_bytes = audio_path.stat().st_size
        except OSError:
            size_bytes = None
        memos.append(
            Memo(
                uuid=str(uuid),
                title=str(title) if title else "(untitled)",
                datetime_iso=datetime_iso,
                duration_seconds=float(duration) if duration is not None else None,
                location=location,
                audio_path=audio_path,
                ios_transcript=str(ios_trans).strip() if ios_trans else None,
                folder=folders.get(folder_fk) if folder_fk else None,
                modified_iso=modified_iso,
                file_size_bytes=size_bytes,
                audio_info=None,  # populated lazily before record write (avoid 750x ffprobe upfront)
            )
        )
    return memos


# ---------- helpers ----------


def probe_audio(p: Path) -> dict | None:
    """Use ffprobe for sample_rate / channels / codec. None if ffprobe missing or fails."""
    import shutil
    import subprocess

    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate,channels,codec_name",
                "-of",
                "json",
                str(p),
            ],
            timeout=10,
        )
        data = json.loads(out)
        s = (data.get("streams") or [{}])[0]
        if not s:
            return None
        return {
            "sample_rate": int(s["sample_rate"]) if s.get("sample_rate") else None,
            "channels": s.get("channels"),
            "codec": s.get("codec_name"),
        }
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        KeyError,
        ValueError,
    ):
        return None


def folder_lookup(conn: sqlite3.Connection) -> dict[int, str]:
    """Return {Z_PK: folder_name} for any folder-like table present."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    tables = [r[0] for r in rows]
    folder_table = next(
        (
            t
            for t in tables
            if t.upper() in ("ZFOLDER", "ZCLOUDFOLDER", "ZCLOUDRECORDINGFOLDER")
        ),
        None,
    )
    if not folder_table:
        return {}
    cols = column_set(conn, folder_table)
    name_col = pick(cols, "ZENCRYPTEDNAME", "ZNAME", "ZTITLE", "ZCUSTOMLABEL")
    if not name_col:
        return {}
    out: dict[int, str] = {}
    for pk, name in conn.execute(f"SELECT Z_PK, {name_col} FROM {folder_table}"):
        if name:
            out[pk] = str(name)
    return out


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_index() -> dict:
    if not INDEX_FILE.exists():
        return {"version": 1, "last_run": None, "processed": {}}
    return json.loads(INDEX_FILE.read_text())


def save_index(idx: dict) -> None:
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    idx["last_run"] = dt.datetime.now(dt.UTC).astimezone().isoformat()
    INDEX_FILE.write_text(json.dumps(idx, indent=2, sort_keys=True) + "\n")


def _ym(datetime_iso: str | None) -> str:
    """Extract 'YYYY-MM' from an ISO datetime; fallback 'unknown'."""
    if not datetime_iso or len(datetime_iso) < 7:
        return "unknown"
    return datetime_iso[:7]


def record_path_for(uuid: str, datetime_iso: str | None) -> Path:
    return SUMMARIES_DIR / _ym(datetime_iso) / f"{uuid}.json"


def record_path(uuid: str) -> Path:
    """Find existing record by UUID (index lookup → glob fallback)."""
    idx = load_index()
    entry = (idx.get("processed") or {}).get(uuid) or {}
    if entry.get("path"):
        p = SUMMARIES_DIR / entry["path"]
        if p.exists():
            return p
    for cand in SUMMARIES_DIR.rglob(f"{uuid}.json"):
        return cand
    # Default for new writes when caller doesn't know datetime — under 'unknown/'.
    return SUMMARIES_DIR / "unknown" / f"{uuid}.json"


# ---------- transcription engines ----------

_TS_RE = __import__("re").compile(
    r"^\[(?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{3})\s*-->\s*"
    r"(?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{3})\]"
)


def _ts_to_seconds(h: int, m: int, s: int, ms: int) -> float:
    return h * 3600 + m * 60 + s + ms / 1000.0


def _collapse_repeat_runs(
    segments: list[dict], min_run: int = 3
) -> tuple[list[dict], int]:
    """Collapse consecutive identical-text segments into one.

    Whisper occasionally enters a hallucination loop emitting the same line N
    times over a long silent / noisy stretch. Keep the first occurrence and
    extend its end-time to span the whole run; drop the rest. Returns (segments,
    num_dropped). Only triggers on runs >= min_run so legitimate short
    repetitions ("yeah yeah yeah") survive.
    """
    if not segments:
        return segments, 0
    out: list[dict] = []
    dropped = 0
    i = 0
    while i < len(segments):
        j = i + 1
        text_i = (segments[i].get("text") or "").strip()
        while (
            j < len(segments)
            and text_i
            and (segments[j].get("text") or "").strip() == text_i
        ):
            j += 1
        run = j - i
        if run >= min_run:
            merged = dict(segments[i])
            merged["end"] = segments[j - 1].get("end", merged.get("end"))
            out.append(merged)
            dropped += run - 1
        else:
            out.extend(segments[i:j])
        i = j
    return out, dropped


def _detect_hallucination(segments: list[dict]) -> str | None:
    """Return reason string if transcript looks broken, else None."""
    if not segments:
        return "no segments produced"
    texts = [(s.get("text") or "").strip() for s in segments]
    # Longest run of identical consecutive segments
    max_run, cur_run, prev = 1, 1, None
    for t in texts:
        if t and t == prev:
            cur_run += 1
            max_run = max(max_run, cur_run)
        else:
            cur_run = 1
        prev = t
    if max_run >= 10:
        return f"hallucination loop: same line repeated {max_run}x"
    # Avg log-prob across segments
    lps = [s.get("avg_logprob") for s in segments if s.get("avg_logprob") is not None]
    if lps and sum(lps) / len(lps) < -1.5:
        return f"low confidence: avg logprob {sum(lps) / len(lps):.2f}"
    return None


def transcribe_whisper_mlx(
    audio: Path,
    model: str,
    total_duration: float | None = None,
    language: str | None = "en",
    label: str = "",
) -> dict:
    import builtins

    import mlx_whisper  # lazy: only needed if engine used

    click.echo(f"  transcribing ({model})...", err=True)

    orig_print = builtins.print
    short_label = (label[:40] + "…") if len(label) > 41 else label
    state = {"last_text": None, "dup_count": 0, "recent": [], "cycle_strikes": 0}
    RECENT_N = 6
    CYCLE_ABORT = 5  # abort after N consecutive A/B/A/B alternations

    def flush_dup():
        if state["dup_count"] > 1:
            orig_print(f"  … (last line repeated {state['dup_count']}x)")
        state["dup_count"] = 0

    def stamped_print(*args, **kwargs):
        line = " ".join(str(a) for a in args)
        m = _TS_RE.match(line)
        if m and total_duration:
            text_part = line[m.end() :].strip()
            if text_part == state["last_text"]:
                state["dup_count"] += 1
                return
            flush_dup()
            state["last_text"] = text_part

            recent = state["recent"]
            recent.append(text_part)
            if len(recent) > RECENT_N:
                recent.pop(0)
            if (
                len(recent) >= 4
                and recent[-1] == recent[-3]
                and recent[-2] == recent[-4]
                and recent[-1] != recent[-2]
            ):
                state["cycle_strikes"] += 1
                if state["cycle_strikes"] >= CYCLE_ABORT:
                    orig_print(
                        f"  ABORT: hallucination cycle "
                        f"('{recent[-1][:40]}' <-> '{recent[-2][:40]}')"
                    )
                    raise RuntimeError("whisper_hallucination_cycle")
            else:
                state["cycle_strikes"] = 0

            end = _ts_to_seconds(
                int(m.group(5) or 0), int(m.group(6)), int(m.group(7)), int(m.group(8))
            )
            pct = min(100.0, 100.0 * end / total_duration)
            prefix = (
                f"  [{pct:5.1f}% {short_label}]" if short_label else f"  [{pct:5.1f}%]"
            )
            orig_print(f"{prefix} {line}", **kwargs)
        else:
            flush_dup()
            state["last_text"] = None
            orig_print(*args, **kwargs)

    import time

    t_start = time.monotonic()
    t_start_iso = dt.datetime.now(dt.UTC).astimezone().isoformat()
    builtins.print = stamped_print
    try:
        result = mlx_whisper.transcribe(
            str(audio),
            path_or_hf_repo=model,
            verbose=True,
            language=language,
            # Whisper hallucinates repeating phrases when fed its own output
            # as context across silent/noisy segments. Disable that feedback.
            condition_on_previous_text=False,
            compression_ratio_threshold=2.4,
            logprob_threshold=-1.0,
            no_speech_threshold=0.6,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            # Skip segments where Whisper appears to be hallucinating on silence.
            hallucination_silence_threshold=2.0,
        )
    finally:
        flush_dup()
        builtins.print = orig_print

    segments = result.get("segments") or []
    warning = _detect_hallucination(segments)
    if warning:
        click.echo(f"  WARNING transcript suspect: {warning}", err=True)
    # Keep compact segment data: timing + text + confidence. Drop tokens/seek.
    trimmed_segments = [
        {
            "start": round(float(s.get("start", 0.0)), 3),
            "end": round(float(s.get("end", 0.0)), 3),
            "text": (s.get("text") or "").strip(),
            "avg_logprob": s.get("avg_logprob"),
            "no_speech_prob": s.get("no_speech_prob"),
        }
        for s in segments
    ]
    trimmed_segments, collapsed = _collapse_repeat_runs(trimmed_segments)
    if collapsed:
        click.echo(
            f"  collapsed {collapsed} repeated segment(s) (hallucination loops)",
            err=True,
        )
    elapsed = time.monotonic() - t_start
    return {
        "text": result.get("text", "").strip(),
        "segments": trimmed_segments,
        "source": "whisper-mlx",
        "model": model,
        "language": result.get("language"),
        "warning": warning,
        "started_at": t_start_iso,
        "elapsed_seconds": round(elapsed, 2),
    }


def transcribe_whisper_mlx_turbo(
    audio: Path,
    total_duration: float | None = None,
    language: str | None = "en",
    label: str = "",
) -> dict:
    return {
        **transcribe_whisper_mlx(
            audio,
            "mlx-community/whisper-large-v3-turbo-mlx",
            total_duration,
            language,
            label,
        ),
        "source": "whisper-mlx-turbo",
    }


ENGINES = {
    "whisper-mlx": lambda a, d, lang, lbl: transcribe_whisper_mlx(
        a, "mlx-community/whisper-large-v3-mlx", d, lang, lbl
    ),
    "whisper-mlx-turbo": transcribe_whisper_mlx_turbo,
}


def transcribe_audio(
    audio: Path,
    engine: str,
    total_duration: float | None = None,
    language: str | None = "en",
    label: str = "",
) -> dict:
    if engine not in ENGINES:
        raise click.ClickException(
            f"Unknown engine '{engine}'. Available: {list(ENGINES)}"
        )
    return ENGINES[engine](audio, total_duration, language, label)


def release_mlx_memory() -> None:
    """Release MLX + torch MPS cached buffers + Python objects to keep RAM stable across memos."""
    import gc

    gc.collect()
    try:
        import mlx.core as mx

        mx.clear_cache()
    except (ImportError, AttributeError):
        pass
    try:
        import torch

        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except (ImportError, AttributeError, RuntimeError):
        pass


# ---------- LLM ----------


def check_llm(base_url: str, model: str) -> None:
    """Ping LM Studio, verify the requested model id exists."""
    try:
        r = requests.get(f"{base_url}/models", timeout=10)
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", [])]
    except requests.RequestException as e:
        raise click.ClickException(
            f"Cannot reach LLM server at {base_url}: {e}\n"
            f"  - Start LM Studio and click 'Start Server' (Developer tab)."
        ) from e
    if model not in ids:
        listed = "\n    ".join(ids) or "(none loaded)"
        raise click.ClickException(
            f"LLM model '{model}' not found on server.\n"
            f"  Available model ids:\n    {listed}\n"
            f"  Fix: load '{model}' in LM Studio, or re-run with "
            f"LLM_MODEL=<id-from-list>."
        )


def summarise(text: str, base_url: str, model: str, timeout: int = 600) -> dict:
    system_prompt = PROMPT_FILE.read_text()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text or "(empty)"},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "stream": True,
    }
    import time

    click.echo(f"  calling LLM ({len(text)} chars) — prefill...", err=True, nl=False)

    for attempt in (1, 2):
        chunks: list[str] = []
        t0 = time.monotonic()
        first_token_at: float | None = None
        last_heartbeat = t0
        try:
            with requests.post(
                f"{base_url}/chat/completions",
                json=payload,
                timeout=timeout,
                stream=True,
            ) as r:
                if not r.ok:
                    raise requests.HTTPError(
                        f"{r.status_code} {r.reason}: {r.text[:500]}"
                    )
                for line in r.iter_lines(decode_unicode=True):
                    now = time.monotonic()
                    # Heartbeat dots every 5s while waiting for prefill
                    if first_token_at is None and now - last_heartbeat >= 5:
                        click.echo(".", err=True, nl=False)
                        last_heartbeat = now
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"].get(
                            "content", ""
                        )
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta:
                        if first_token_at is None:
                            first_token_at = now
                            click.echo(
                                f" first-token after {now - t0:.1f}s, streaming:",
                                err=True,
                            )
                        chunks.append(delta)
                        click.echo(delta, nl=False, err=True)
            total = time.monotonic() - t0
            gen = total - (first_token_at - t0) if first_token_at else 0
            click.echo(
                f"\n  done in {total:.1f}s (prefill "
                f"{(first_token_at - t0) if first_token_at else total:.1f}s, "
                f"gen {gen:.1f}s)",
                err=True,
            )
            content = "".join(chunks)
            parsed = json.loads(content)
            if "summary" in parsed and "tags" in parsed:
                return {
                    "summary": str(parsed["summary"]),
                    "tags": [str(t) for t in parsed["tags"]],
                }
        except (requests.RequestException, ValueError, KeyError) as e:
            if attempt == 2:
                raise click.ClickException(f"LLM summarise failed: {e}") from e
            payload.pop("response_format", None)
    raise click.ClickException("LLM returned malformed JSON twice; aborting.")


# ---------- pipeline ----------


def load_partial(uuid: str) -> dict | None:
    """Return prior <uuid>.json contents if present, else None."""
    p = record_path(uuid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def write_failure_marker(memo: Memo, sha: str, error: str) -> Path:
    """Write a stub record marking the memo as un-transcribable.

    Index will mark it processed so it doesn't waste time on every run.
    Re-runnable explicitly via `summarise-rerun UUIDS=...`.
    """
    p = record_path_for(memo.uuid, memo.datetime_iso)
    p.parent.mkdir(parents=True, exist_ok=True)
    stub = {
        "uuid": memo.uuid,
        "title": memo.title,
        "datetime": memo.datetime_iso,
        "duration_seconds": memo.duration_seconds,
        "location": memo.location,
        "folder": memo.folder,
        "modified": memo.modified_iso,
        "file_size_bytes": memo.file_size_bytes,
        "audio_info": memo.audio_info,
        "source_path": memo.audio_path.name,
        "transcript": None,
        "summary": None,
        "summary_model": None,
        "tags": [],
        "audio_sha256": sha,
        "error": error,
        "processed_at": dt.datetime.now(dt.UTC).astimezone().isoformat(),
        "processor_version": PROCESSOR_VERSION,
    }
    p.write_text(json.dumps(stub, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return p


def write_partial_transcript(memo: Memo, transcript: dict, sha: str) -> None:
    """Persist transcript-only record so a later LLM failure doesn't waste it."""
    p = record_path_for(memo.uuid, memo.datetime_iso)
    p.parent.mkdir(parents=True, exist_ok=True)
    if memo.audio_info is None:
        memo.audio_info = probe_audio(memo.audio_path)
    partial = {
        "uuid": memo.uuid,
        "title": memo.title,
        "datetime": memo.datetime_iso,
        "duration_seconds": memo.duration_seconds,
        "location": memo.location,
        "folder": memo.folder,
        "modified": memo.modified_iso,
        "file_size_bytes": memo.file_size_bytes,
        "audio_info": memo.audio_info,
        "source_path": memo.audio_path.name,
        "transcript": transcript,
        "summary": None,
        "summary_model": None,
        "tags": [],
        "audio_sha256": sha,
        "processed_at": None,
        "processor_version": PROCESSOR_VERSION,
    }
    p.write_text(
        json.dumps(partial, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


def process_memo_transcript_only(
    memo: Memo, engine: str, language: str | None, force: bool = False
) -> None:
    """Transcribe and write partial record. No LLM call. Returns None."""
    sha = sha256_file(memo.audio_path)
    prior = load_partial(memo.uuid)
    has_segments = bool((prior or {}).get("transcript", {}).get("segments"))
    if (
        not force
        and prior
        and prior.get("audio_sha256") == sha
        and prior.get("transcript", {}).get("text")
        and has_segments
    ):
        click.echo("  transcript already exists, skipping", err=True)
        return
    if not force and prior and not has_segments:
        click.echo("  prior transcript has no segments — re-transcribing", err=True)
    if memo.ios_transcript:
        transcript = {
            "text": memo.ios_transcript,
            "source": "ios",
            "model": None,
            "language": None,
        }
    else:
        ymd = memo.datetime_iso[:10]
        label = memo.title if memo.title.startswith(ymd) else f"{ymd} {memo.title}"
        transcript = transcribe_audio(
            memo.audio_path, engine, memo.duration_seconds, language, label
        )
        release_mlx_memory()
    write_partial_transcript(memo, transcript, sha)
    return


def process_memo(
    memo: Memo,
    engine: str,
    language: str | None,
    llm_base_url: str,
    llm_model: str,
    existing_transcript: dict | None,
    force: bool = False,
) -> Record:
    sha = sha256_file(memo.audio_path)

    # Reuse previously-saved transcript if present and audio unchanged.
    prior = load_partial(memo.uuid)
    if (
        not force
        and existing_transcript is None
        and prior
        and prior.get("audio_sha256") == sha
        and prior.get("transcript", {}).get("text")
    ):
        existing_transcript = prior["transcript"]
        click.echo("  reusing existing transcript (skipping Whisper)", err=True)

    if existing_transcript is not None:
        transcript = existing_transcript
    elif memo.ios_transcript:
        transcript = {
            "text": memo.ios_transcript,
            "source": "ios",
            "model": None,
            "language": None,
        }
    else:
        ymd = memo.datetime_iso[:10]
        # If user never renamed memo, title is already the ISO datetime — don't dup.
        label = memo.title if memo.title.startswith(ymd) else f"{ymd} {memo.title}"
        transcript = transcribe_audio(
            memo.audio_path, engine, memo.duration_seconds, language, label
        )
        # Persist transcript NOW so an LLM failure later doesn't waste it.
        write_partial_transcript(memo, transcript, sha)
        # Free Whisper model + activations before LLM call to relieve RAM pressure.
        release_mlx_memory()

    s = summarise(transcript["text"], llm_base_url, llm_model)
    if memo.audio_info is None:
        memo.audio_info = probe_audio(memo.audio_path)
    return Record(
        uuid=memo.uuid,
        title=memo.title,
        datetime=memo.datetime_iso,
        duration_seconds=memo.duration_seconds,
        location=memo.location,
        folder=memo.folder,
        modified=memo.modified_iso,
        file_size_bytes=memo.file_size_bytes,
        audio_info=memo.audio_info,
        source_path=memo.audio_path.name,
        transcript=transcript,
        summary=s["summary"],
        summary_model=llm_model,
        tags=s["tags"],
        audio_sha256=sha,
        processed_at=dt.datetime.now(dt.UTC).astimezone().isoformat(),
    )


def write_record(rec: Record) -> None:
    p = record_path_for(rec.uuid, rec.datetime)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(asdict(rec), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


def select_targets(
    memos: list[Memo], idx: dict, *, mode: str, uuids: list[str] | None
) -> list[Memo]:
    processed = idx.get("processed", {})
    if mode == "all":
        return memos
    if mode == "uuids":
        wanted = set(uuids or [])
        out = [m for m in memos if m.uuid in wanted]
        missing = wanted - {m.uuid for m in out}
        if missing:
            click.echo(f"warning: UUIDs not found in DB: {sorted(missing)}", err=True)
        return out
    # incremental
    out = []
    for m in memos:
        prev = processed.get(m.uuid)
        if prev is None:
            out.append(m)
            continue
        sha = sha256_file(m.audio_path)
        if prev.get("audio_sha256") != sha:
            out.append(m)
            continue
        # Sha matches a prior failure marker → leave it alone.
        if prev.get("error"):
            continue
        # Index says done — but if the per-file JSON is gone, or partial
        # (no summary), re-queue. Deleting summaries/<ym>/<uuid>.json is the
        # supported way to force a single memo to be reprocessed.
        partial = load_partial(m.uuid)
        if partial is None:
            out.append(m)
            continue
        if not partial.get("summary"):
            out.append(m)
    return out


# ---------- workflows ----------


def run(
    mode_all: bool,
    uuids: str | None,
    summary_only: bool,
    no_summary: bool,
    force: bool,
    engine: str,
    language: str,
    llm_base_url: str,
    llm_model: str,
    db: str,
    recordings_dir: str,
    limit: int,
) -> None:
    """Process memos. Default: incremental."""
    mode = "incremental"
    uuid_list = None
    if mode_all:
        mode = "all"
    if uuids:
        mode = "uuids"
        uuid_list = [u.strip() for u in uuids.split(",") if u.strip()]

    db_path = Path(db).expanduser()
    rec_dir = Path(recordings_dir).expanduser()
    with open_db(db_path) as conn:
        all_memos = list_memos(conn, rec_dir)

    idx = load_index()
    targets = select_targets(all_memos, idx, mode=mode, uuids=uuid_list)
    if limit:
        targets = targets[:limit]

    click.echo(
        f"mode={mode} engine={engine} llm={llm_model} "
        f"no_summary={no_summary} target_count={len(targets)} "
        f"(of {len(all_memos)} total)"
    )

    if not targets:
        click.echo("nothing to do.")
        return

    if not no_summary:
        check_llm(llm_base_url, llm_model)

    import time

    run_audio_s = 0.0
    run_elapsed_s = 0.0

    for i, memo in enumerate(targets, 1):
        dur = memo.duration_seconds or 0
        mm, ss = divmod(int(dur), 60)
        click.echo(f"[{i}/{len(targets)}] {memo.uuid} :: {memo.title} ({mm}m{ss:02d}s)")
        memo_t0 = time.monotonic()
        existing = None
        if summary_only:
            rp = record_path(memo.uuid)
            if rp.exists():
                prev = json.loads(rp.read_text())
                existing = prev.get("transcript")
        try:
            lang = language or None
            if no_summary:
                rec = process_memo_transcript_only(memo, engine, lang, force=force)
            else:
                rec = process_memo(
                    memo, engine, lang, llm_base_url, llm_model, existing, force=force
                )
        except click.ClickException as e:
            click.echo(f"  FAIL: {e.message}", err=True)
            continue
        except RuntimeError as e:
            click.echo(f"  FAIL: {e}", err=True)
            release_mlx_memory()
            try:
                sha = sha256_file(memo.audio_path)
                rel = write_failure_marker(memo, sha, str(e)).relative_to(SUMMARIES_DIR)
                idx.setdefault("processed", {})[memo.uuid] = {
                    "audio_sha256": sha,
                    "processed_at": dt.datetime.now(dt.UTC).astimezone().isoformat(),
                    "processor_version": PROCESSOR_VERSION,
                    "path": str(rel),
                    "error": str(e),
                }
                save_index(idx)
            except OSError as e2:
                click.echo(f"  (also failed to write failure marker: {e2})", err=True)
            continue
        memo_elapsed = time.monotonic() - memo_t0
        # Skip-path (reused prior transcript) finishes in <5s for any non-trivial
        # audio — exclude from rate stats so ETA isn't poisoned by short non-work.
        if memo_elapsed >= 5.0:
            run_audio_s += dur
            run_elapsed_s += memo_elapsed
        if run_audio_s > 0:
            rtf = run_elapsed_s / run_audio_s
            remaining_audio = sum((t.duration_seconds or 0) for t in targets[i:])
            eta_s = rtf * remaining_audio
            eta_h, rem = divmod(int(eta_s), 3600)
            eta_m = rem // 60
            click.echo(
                f"  rate: RTF={rtf:.2f}  ETA for remaining "
                f"{len(targets) - i} memos ≈ {eta_h}h{eta_m:02d}m",
                err=True,
            )
        else:
            click.echo(
                f"  rate: no transcription work yet "
                f"(skipped {i} memos with existing transcripts)",
                err=True,
            )

        if no_summary:
            release_mlx_memory()
            # Partial record already on disk; don't index as complete.
            continue
        write_record(rec)
        rel = record_path_for(rec.uuid, rec.datetime).relative_to(SUMMARIES_DIR)
        idx.setdefault("processed", {})[memo.uuid] = {
            "audio_sha256": rec.audio_sha256,
            "processed_at": rec.processed_at,
            "processor_version": PROCESSOR_VERSION,
            "path": str(rel),
        }
        save_index(idx)  # flush after each so partial runs are durable
        release_mlx_memory()

    click.echo("done.")


def status(db: str, recordings_dir: str) -> None:
    """Show counts: total / processed / pending / stale."""
    with open_db(Path(db).expanduser()) as conn:
        memos = list_memos(conn, Path(recordings_dir).expanduser())
    idx = load_index()
    processed = idx.get("processed", {})

    pending, stale, done, errored = [], [], [], []
    for m in memos:
        prev = processed.get(m.uuid)
        if prev is None:
            pending.append(m)
        else:
            sha = sha256_file(m.audio_path)
            if prev.get("audio_sha256") != sha:
                stale.append(m)
            elif prev.get("error"):
                errored.append((m, prev["error"]))
            else:
                done.append(m)

    # Compute RTF from done records (transcript.elapsed_seconds / duration)
    rtfs = []
    for m in done:
        rec = load_partial(m.uuid)
        if not rec:
            continue
        t = rec.get("transcript") or {}
        el, dur = t.get("elapsed_seconds"), rec.get("duration_seconds")
        if el and dur and dur > 0:
            rtfs.append(el / dur)
    pending_audio_s = sum((m.duration_seconds or 0) for m in (pending + stale))
    eta_str = ""
    if rtfs and pending_audio_s:
        avg_rtf = sum(rtfs) / len(rtfs)
        eta_s = avg_rtf * pending_audio_s
        eta_h, rem = divmod(int(eta_s), 3600)
        eta_m = rem // 60
        eta_str = (
            f"  avg RTF={avg_rtf:.2f}  pending audio="
            f"{pending_audio_s / 3600:.1f}h  ETA≈{eta_h}h{eta_m:02d}m"
        )

    click.echo(f"total in DB:  {len(memos)}")
    click.echo(f"processed:    {len(done)}")
    click.echo(f"pending:      {len(pending)}{eta_str}")
    click.echo(f"stale (sha):  {len(stale)}")
    click.echo(
        f"errored:      {len(errored)}  "
        "(re-run with: restic-backups voice-memos transcribe --uuids ...)"
    )
    click.echo(f"last run:     {idx.get('last_run')}")


_PYANNOTE_PIPELINE = None  # cache pipeline across memos


def _get_pyannote_pipeline(hf_token: str):
    global _PYANNOTE_PIPELINE
    if _PYANNOTE_PIPELINE is None:
        from pyannote.audio import Pipeline

        click.echo("  loading pyannote pipeline (one-time per run)...", err=True)
        try:
            _PYANNOTE_PIPELINE = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                token=hf_token,
            )
        except TypeError:
            _PYANNOTE_PIPELINE = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=hf_token,
            )

        # Tunables via env:
        #   DIARIZE_DEVICE = auto | mps | cpu  (default: auto)
        #   DIARIZE_BATCH  = int               (default: 32 — applied to seg + embed)
        #   DIARIZE_FP16   = 1 | 0             (default: 1 on MPS, 0 on CPU)
        device_pref = (os.environ.get("DIARIZE_DEVICE") or "auto").lower()
        try:
            batch = int(os.environ.get("DIARIZE_BATCH") or "32")
        except ValueError:
            batch = 32

        try:
            import torch

            if device_pref == "cpu":
                device = torch.device("cpu")
                label = "CPU (forced)"
            elif device_pref == "mps":
                device = (
                    torch.device("mps")
                    if torch.backends.mps.is_available()
                    else torch.device("cpu")
                )
                label = (
                    "MPS (Metal)" if device.type == "mps" else "CPU (mps unavailable)"
                )
            else:  # auto
                if torch.backends.mps.is_available():
                    device = torch.device("mps")
                    label = "MPS (Metal)"
                else:
                    device = torch.device("cpu")
                    label = "CPU (slow)"
            _PYANNOTE_PIPELINE.to(device)

            # fp16 off by default — pyannote 3.1 segmentation breaks on MPS when
            # only the model params are cast (input stays fp32 → mps_normalization
            # broadcast error). Opt-in only.
            fp16 = (os.environ.get("DIARIZE_FP16") or "0") == "1"
            if fp16:
                try:
                    for attr in ("_segmentation", "_embedding"):
                        m = getattr(_PYANNOTE_PIPELINE, attr, None)
                        if m is not None and hasattr(m, "model"):
                            m.model.to(dtype=torch.float16)
                except (RuntimeError, AttributeError) as e:
                    click.echo(f"  fp16 cast skipped: {e}", err=True)
                    fp16 = False

            # Larger batches → fewer kernel launches, better MPS / CPU utilisation.
            for attr in ("segmentation_batch_size", "embedding_batch_size"):
                if hasattr(_PYANNOTE_PIPELINE, attr):
                    setattr(_PYANNOTE_PIPELINE, attr, batch)

            click.echo(
                f"  pyannote on {label}  batch={batch}  fp16={'on' if fp16 else 'off'}",
                err=True,
            )
            # CPU multi-threading hint (no-op if user already set OMP_NUM_THREADS).
            if device.type == "cpu":
                try:
                    torch.set_num_threads(
                        int(
                            os.environ.get("OMP_NUM_THREADS") or torch.get_num_threads()
                        )
                    )
                except (ValueError, RuntimeError):
                    pass
        except (ImportError, RuntimeError, AttributeError) as e:
            click.echo(f"  device select fallback to CPU: {e}", err=True)
    return _PYANNOTE_PIPELINE


def diarize_audio(
    audio_path: Path,
    hf_token: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
) -> list[dict]:
    """Run pyannote pipeline with stage progress hook; return turns."""
    pipeline = _get_pyannote_pipeline(hf_token)

    pipe_kwargs: dict = {}
    if num_speakers is not None:
        pipe_kwargs["num_speakers"] = num_speakers
    else:
        if min_speakers is not None:
            pipe_kwargs["min_speakers"] = min_speakers
        if max_speakers is not None:
            pipe_kwargs["max_speakers"] = max_speakers

    # Pre-load whole waveform → 5-10x speedup. Passing a file path causes
    # pyannote's Audio.crop to re-decode the file on every sliding window.
    # Skip preload for very long files (default >90min) to avoid OOM on MPS
    # unified memory. Override threshold with DIARIZE_PRELOAD_MAX_MIN; disable
    # entirely with DIARIZE_PRELOAD=0.
    audio_arg: object = str(audio_path)
    if (os.environ.get("DIARIZE_PRELOAD") or "1") == "1":
        try:
            preload_max_s = (
                float(os.environ.get("DIARIZE_PRELOAD_MAX_MIN") or "90") * 60
            )
        except ValueError:
            preload_max_s = 90 * 60
        try:
            import torchaudio

            info = torchaudio.info(str(audio_path))
            est_dur = info.num_frames / info.sample_rate if info.sample_rate else 0
            if est_dur > preload_max_s:
                click.echo(
                    f"  preload skipped (dur {est_dur / 60:.0f}min > {preload_max_s / 60:.0f}min cap) → file-path mode",
                    err=True,
                )
            else:
                wav, sr = torchaudio.load(str(audio_path))
                if sr != 16000:
                    wav = torchaudio.functional.resample(wav, sr, 16000)
                    sr = 16000
                if wav.shape[0] > 1:
                    wav = wav.mean(dim=0, keepdim=True)
                audio_arg = {"waveform": wav, "sample_rate": sr}
        except (ImportError, RuntimeError, OSError) as e:
            click.echo(f"  preload skipped, fallback to file path: {e}", err=True)
            audio_arg = str(audio_path)

    try:
        from pyannote.audio.pipelines.utils.hook import ProgressHook

        with ProgressHook() as hook:
            result = pipeline(audio_arg, hook=hook, **pipe_kwargs)
    except ImportError:
        result = pipeline(audio_arg, **pipe_kwargs)

    # pyannote 3.4+ returns DiarizeOutput(speaker_diarization=Annotation, embeddings=...).
    # Older versions return Annotation directly.
    if hasattr(result, "itertracks"):
        annotation = result
    elif hasattr(result, "speaker_diarization"):
        annotation = result.speaker_diarization
    else:
        # last-ditch attribute hunt
        annotation = next(
            (v for v in vars(result).values() if hasattr(v, "itertracks")), result
        )

    turns = []
    for seg, _, label in annotation.itertracks(yield_label=True):
        turns.append(
            {
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "speaker": str(label),
            }
        )
    return turns


def assign_speakers(segments: list[dict], turns: list[dict]) -> list[dict]:
    """For each whisper segment, attach speaker = pyannote turn with max overlap."""
    out = []
    for s in segments:
        s_start, s_end = float(s["start"]), float(s["end"])
        best, best_overlap = None, 0.0
        for t in turns:
            overlap = max(0.0, min(s_end, t["end"]) - max(s_start, t["start"]))
            if overlap > best_overlap:
                best, best_overlap = t["speaker"], overlap
        out.append({**s, "speaker": best})
    return out


def labelled_text(labelled_segments: list[dict]) -> str:
    """Group consecutive same-speaker segments into 'SPK_NN: ...' lines."""
    lines: list[str] = []
    cur_spk: str | None = None
    cur: list[str] = []
    for s in labelled_segments:
        spk = s.get("speaker") or "UNK"
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if spk != cur_spk:
            if cur:
                lines.append(f"{cur_spk}: " + " ".join(cur))
            cur_spk, cur = spk, [text]
        else:
            cur.append(text)
    if cur:
        lines.append(f"{cur_spk}: " + " ".join(cur))
    return "\n".join(lines)


def diarize(
    uuids: str | None,
    force: bool,
    limit: int,
    recordings_dir: str,
    order: str,
    min_duration: float,
    min_speakers: int | None,
    max_speakers: int | None,
    num_speakers: int | None,
) -> None:
    """Add pyannote speaker diarization to existing JSON records."""
    hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if not hf_token:
        raise click.ClickException(
            "HF_TOKEN env var required.\n"
            "  1. Accept terms at https://hf.co/pyannote/speaker-diarization-3.1\n"
            "     and  https://hf.co/pyannote/segmentation-3.0\n"
            "  2. Token: https://hf.co/settings/tokens\n"
            "  3. export HF_TOKEN=hf_xxx"
        )
    rec_dir = Path(recordings_dir).expanduser()
    wanted = {u.strip() for u in (uuids or "").split(",") if u.strip()}

    targets: list[tuple[Path, dict]] = []
    for p in sorted(SUMMARIES_DIR.rglob("*.json")):
        if p.name == "index.json":
            continue
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        uuid = data.get("uuid")
        if not uuid:
            continue
        if wanted and uuid not in wanted:
            continue
        if not (data.get("transcript") or {}).get("segments"):
            continue
        if data.get("diarization") and not force:
            continue
        if (data.get("duration_seconds") or 0) < min_duration:
            continue
        targets.append((p, data))

    if order == "short-first":
        targets.sort(key=lambda t: t[1].get("duration_seconds") or 0)
    elif order == "long-first":
        targets.sort(key=lambda t: -(t[1].get("duration_seconds") or 0))

    if limit:
        targets = targets[:limit]

    total_audio = sum((t[1].get("duration_seconds") or 0) for t in targets)
    click.echo(f"diarize: {len(targets)} memos, {total_audio / 3600:.1f}h total audio")
    if not targets:
        return

    import time

    run_audio_s, run_elapsed_s = 0.0, 0.0
    for i, (p, data) in enumerate(targets, 1):
        audio = rec_dir / data["source_path"]
        dur = data.get("duration_seconds") or 0
        mm, ss = divmod(int(dur), 60)
        title = data.get("title") or ""
        ymd = (data.get("datetime") or "")[:10]
        start_wall = dt.datetime.now().astimezone()
        click.echo(
            f"[{i}/{len(targets)}] {data['uuid']} :: "
            f"{ymd} {title} ({mm}m{ss:02d}s)  start={start_wall.strftime('%H:%M:%S')}"
        )
        if not audio.exists():
            click.echo(f"  audio missing: {audio}", err=True)
            continue
        t0 = time.monotonic()
        try:
            turns = diarize_audio(
                audio,
                hf_token,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
                num_speakers=num_speakers,
            )
        except (RuntimeError, OSError, ValueError) as e:
            click.echo(f"  FAIL: {e}", err=True)
            continue
        elapsed = time.monotonic() - t0
        end_wall = dt.datetime.now().astimezone()
        segs = data["transcript"]["segments"]
        labelled_segs = assign_speakers(segs, turns)
        speakers = len({t["speaker"] for t in turns})
        data["diarization"] = {
            "turns": turns,
            "labelled_segments": labelled_segs,
            "labelled_text": labelled_text(labelled_segs),
            "model": "pyannote/speaker-diarization-3.1",
            "speakers_detected": speakers,
            "elapsed_seconds": round(elapsed, 2),
            "processed_at": dt.datetime.now(dt.UTC).astimezone().isoformat(),
        }
        p.write_text(
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        release_mlx_memory()

        run_audio_s += dur
        run_elapsed_s += elapsed
        click.echo(
            f"  diarized in {elapsed:.1f}s — {speakers} speaker(s)  "
            f"[{start_wall.strftime('%H:%M:%S')} → {end_wall.strftime('%H:%M:%S')}]",
            err=True,
        )
        if run_audio_s > 0:
            rtf = run_elapsed_s / run_audio_s
            remaining_audio = sum(
                (t[1].get("duration_seconds") or 0) for t in targets[i:]
            )
            eta_s = rtf * remaining_audio
            eta_h, rem = divmod(int(eta_s), 3600)
            eta_m = rem // 60
            pct = 100.0 * i / len(targets)
            click.echo(
                f"  [{pct:5.1f}%]  RTF={rtf:.2f}  "
                f"ETA for remaining {len(targets) - i} ≈ {eta_h}h{eta_m:02d}m",
                err=True,
            )
    click.echo("done.")


def eligible_diarization_uuids(order: str, min_duration: float) -> list[str]:
    """Return UUIDs eligible for diarization in the requested order."""
    rows: list[tuple[str, float]] = []
    for p in SUMMARIES_DIR.rglob("*.json"):
        if p.name == "index.json":
            continue
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if not (data.get("transcript") or {}).get("segments"):
            continue
        if data.get("diarization"):
            continue
        dur = float(data.get("duration_seconds") or 0)
        if dur < min_duration:
            continue
        uuid = data.get("uuid")
        if uuid:
            rows.append((uuid, dur))
    if order == "short-first":
        rows.sort(key=lambda r: r[1])
    elif order == "long-first":
        rows.sort(key=lambda r: -r[1])
    return [uuid for uuid, _ in rows]


def diarize_status() -> None:
    """Count: have-transcript, have-diarization, eligible-not-yet."""
    have_seg, have_diar, eligible = 0, 0, 0
    for p in SUMMARIES_DIR.rglob("*.json"):
        if p.name == "index.json":
            continue
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        has_seg = bool((data.get("transcript") or {}).get("segments"))
        has_diar = bool(data.get("diarization"))
        if has_seg:
            have_seg += 1
        if has_diar:
            have_diar += 1
        if has_seg and not has_diar:
            eligible += 1
    click.echo(f"with segments:       {have_seg}")
    click.echo(f"with diarization:    {have_diar}")
    click.echo(f"eligible (run me):   {eligible}")


def migrate_layout() -> None:
    """Move flat summaries/<uuid>.json into summaries/YYYY-MM/<uuid>.json."""
    moved, skipped = 0, 0
    idx = load_index()
    for src in sorted(SUMMARIES_DIR.glob("*.json")):
        if src.name == "index.json":
            continue
        try:
            data = json.loads(src.read_text())
        except json.JSONDecodeError:
            click.echo(f"  skip (bad json): {src.name}", err=True)
            skipped += 1
            continue
        uuid = data.get("uuid") or src.stem
        ymd = data.get("datetime")
        dest = record_path_for(uuid, ymd)
        if dest == src:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)
        rel = dest.relative_to(SUMMARIES_DIR)
        if uuid in idx.get("processed", {}):
            idx["processed"][uuid]["path"] = str(rel)
        moved += 1
        click.echo(f"  {src.name} -> {rel}")
    save_index(idx)
    click.echo(f"done. moved={moved} skipped={skipped}")


def peek(db: str, uuid: str | None) -> None:
    """Show DB schema (and optionally one row) for debugging field choices."""
    with open_db(Path(db).expanduser()) as conn:
        cols = conn.execute("PRAGMA table_info(ZCLOUDRECORDING)").fetchall()
        click.echo("ZCLOUDRECORDING columns:")
        for c in cols:
            click.echo(f"  {c[1]:35s} {c[2]}")
        if uuid:
            uuid_col = pick(
                column_set(conn, "ZCLOUDRECORDING"), "ZUNIQUEID", "ZIDENTIFIER"
            )
            row = conn.execute(
                f"SELECT * FROM ZCLOUDRECORDING WHERE {uuid_col} = ?", (uuid,)
            ).fetchone()
            if not row:
                click.echo(f"\nno row with {uuid_col}={uuid}")
                return
            click.echo(f"\nrow for {uuid}:")
            for c, v in zip([x[1] for x in cols], row, strict=False):
                click.echo(f"  {c:35s} {v!r}")


def prune_index(yes: bool, dry_run: bool, db: str) -> None:
    """Remove index entries whose summary JSON no longer exists on disk.

    For each missing entry, prints what we know (index metadata + DB lookup by
    UUID) and prompts before removing. Use --yes to skip prompts.
    """
    idx = load_index()
    processed = idx.get("processed") or {}
    if not processed:
        click.echo("index has no processed entries.")
        return

    # Best-effort DB enrichment.
    db_rows: dict[str, dict] = {}
    try:
        with open_db(Path(db).expanduser()) as conn:
            uuid_col = pick(
                column_set(conn, "ZCLOUDRECORDING"), "ZUNIQUEID", "ZIDENTIFIER"
            )
            for row in conn.execute(
                f"SELECT {uuid_col}, ZCUSTOMLABEL, ZDATE, ZDURATION FROM ZCLOUDRECORDING"
            ):
                db_rows[row[0]] = {
                    "title": row[1],
                    "zdate": row[2],
                    "duration": row[3],
                }
    except (sqlite3.Error, OSError) as e:
        click.echo(f"(db lookup unavailable: {e})", err=True)

    missing = []
    for uuid, entry in processed.items():
        rel = entry.get("path")
        if not rel:
            missing.append((uuid, entry, None))
            continue
        full = SUMMARIES_DIR / rel
        if not full.exists():
            missing.append((uuid, entry, full))

    if not missing:
        click.echo(
            f"all {len(processed)} index entries have their JSON on disk. nothing to prune."
        )
        return

    click.echo(f"missing JSON for {len(missing)} of {len(processed)} index entries.\n")
    removed = 0
    for uuid, entry, full in missing:
        click.echo(f"=== {uuid}")
        click.echo(f"  index.path     : {entry.get('path')}")
        click.echo(f"  expected at    : {full}")
        click.echo(f"  processed_at   : {entry.get('processed_at')}")
        click.echo(f"  audio_sha256   : {entry.get('audio_sha256')}")
        if entry.get("error"):
            click.echo(f"  error          : {entry['error']}")
        if uuid in db_rows:
            r = db_rows[uuid]
            click.echo(f"  db.title       : {r.get('title')}")
            click.echo(f"  db.duration_s  : {r.get('duration')}")
        else:
            click.echo("  db             : <not in CloudRecordings.db>")

        if dry_run:
            click.echo("  → would remove (dry-run)")
            continue
        if yes or click.confirm("  remove from index?", default=True):
            del processed[uuid]
            removed += 1
            click.echo("  removed.")
        else:
            click.echo("  kept.")

    if dry_run:
        click.echo(f"\ndry-run: {len(missing)} entries would be removed.")
        return
    if removed:
        idx["processed"] = processed
        save_index(idx)
        click.echo(f"\npruned {removed} entries. index saved.")
    else:
        click.echo("\nnothing removed.")


def rebuild_index(dry_run: bool) -> None:
    """Rebuild summaries/index.json from JSON files on disk.

    Walks summaries/**/<uuid>.json, derives processed[uuid] from each file.
    Anything not on disk drops out. Existing index is overwritten.
    """
    processed: dict[str, dict] = {}
    skipped = 0
    for p in sorted(SUMMARIES_DIR.rglob("*.json")):
        if p.name == "index.json":
            continue
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            click.echo(f"  skip (bad json): {p.relative_to(SUMMARIES_DIR)}", err=True)
            skipped += 1
            continue
        uuid = data.get("uuid") or p.stem
        if not uuid:
            skipped += 1
            continue
        rel = p.relative_to(SUMMARIES_DIR)
        entry: dict = {
            "audio_sha256": data.get("audio_sha256"),
            "processed_at": (
                data.get("processed_at")
                or dt.datetime.fromtimestamp(p.stat().st_mtime, dt.UTC)
                .astimezone()
                .isoformat()
            ),
            "processor_version": data.get("processor_version", PROCESSOR_VERSION),
            "path": str(rel),
        }
        if data.get("error"):
            entry["error"] = data["error"]
        processed[uuid] = entry

    if dry_run:
        click.echo(
            f"rebuild-index (dry-run): {len(processed)} entries derived from disk; {skipped} skipped."
        )
        return

    old = load_index()
    idx = {
        "version": old.get("version", 1),
        "last_run": old.get("last_run"),
        "processed": processed,
    }
    save_index(idx)
    click.echo(
        f"rebuilt index: {len(processed)} entries (was {len(old.get('processed') or {})}); "
        f"{skipped} files skipped."
    )
