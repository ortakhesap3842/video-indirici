import asyncio
import json
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

NEURAL_VOICES: Dict[str, str] = {
    "tr": "tr-TR-AhmetNeural",
    "en": "en-US-AndrewNeural",
    "de": "de-DE-ConradNeural",
    "ru": "ru-RU-DmitryNeural",
    "zh": "zh-CN-YunxiNeural",
    "es": "es-ES-AlvaroNeural",
    "fr": "fr-FR-HenriNeural",
    "ar": "ar-SA-HamedNeural",
    "ja": "ja-JP-KeitaNeural",
    "ko": "ko-KR-InJoonNeural",
    "it": "it-IT-DiegoNeural",
    "pt": "pt-BR-AntonioNeural",
}


def _time_to_ms(ts: str) -> int:
    cleaned = ts.strip().replace(",", ".")
    parts = cleaned.split(":")
    if len(parts) == 2:
        h, m, s = 0, int(parts[0]), float(parts[1])
    elif len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    else:
        return 0
    return int((h * 3600 + m * 60 + s) * 1000)


def parse_subtitle_cues(sub_path: Path) -> List[Tuple[int, int, str]]:
    text = sub_path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\r?\n\r?\n+", text)
    cues: List[Tuple[int, int, str]] = []
    seen_last = ""

    for blk in blocks:
        lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
        if not lines:
            continue
        ts_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        if ts_idx == -1:
            continue
        ts_line = lines[ts_idx]
        m = re.search(r"([\d:.,]+)\s*-->\s*([\d:.,]+)", ts_line)
        if not m:
            continue
        start_ms = _time_to_ms(m.group(1))
        end_ms = _time_to_ms(m.group(2))
        body = " ".join(lines[ts_idx + 1 :])
        body = re.sub(r"<[^>]+>", "", body)
        body = re.sub(r"\s+", " ", body).strip()
        if not body or body == seen_last:
            continue
        seen_last = body
        cues.append((start_ms, max(end_ms, start_ms + 500), body))

    return merge_cues_into_sentences(cues)


def merge_cues_into_sentences(
    cues: List[Tuple[int, int, str]], max_chars: int = 240
) -> List[Tuple[int, int, str]]:
    """Merge fragmented auto-captions into full sentences for coherent AI translation."""
    merged: List[Tuple[int, int, str]] = []
    cur_start: Optional[int] = None
    cur_end = 0
    cur_parts: List[str] = []

    for start_ms, end_ms, txt in cues:
        if cur_start is None:
            cur_start = start_ms
        cur_parts.append(txt)
        cur_end = end_ms
        joined = " ".join(cur_parts)
        if joined.endswith((".", "!", "?")) or len(joined) >= max_chars:
            merged.append((cur_start, cur_end, joined))
            cur_start = None
            cur_parts = []

    if cur_parts and cur_start is not None:
        merged.append((cur_start, cur_end, " ".join(cur_parts)))
    return merged


def translate_text_contextual(text: str, target_lang: str) -> str:
    code = target_lang.strip().lower()
    tl = "zh-CN" if code == "zh" else code
    try:
        from deep_translator import GoogleTranslator

        return GoogleTranslator(source="auto", target=tl).translate(text) or text
    except Exception:
        pass

    try:
        q = urllib.parse.quote(text)
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={tl}&dt=t&q={q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        translated = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
        return translated.strip() or text
    except Exception:
        return text


async def _synth_edge_tts(segments: List[Tuple[int, str]], voice: str, out_dir: Path) -> List[Tuple[int, Path]]:
    import edge_tts

    audio_parts: List[Tuple[int, Path]] = []
    for idx, (start_ms, txt) in enumerate(segments):
        part_path = out_dir / f"seg_{idx:04d}.mp3"
        communicate = edge_tts.Communicate(txt, voice)
        await communicate.save(str(part_path))
        if part_path.exists() and part_path.stat().st_size > 0:
            audio_parts.append((start_ms, part_path))
    return audio_parts


def apply_ai_dubbing(
    video_path: Path, sub_path: Path, dub_lang: str, ffmpeg_exe: str
) -> Optional[Path]:
    """Generate contextual neural TTS dubbing track and mux into the video."""
    lang = dub_lang.strip().lower()
    voice = NEURAL_VOICES.get(lang)
    if not voice or not video_path.exists() or not sub_path.exists():
        return None

    cues = parse_subtitle_cues(sub_path)
    if not cues:
        return None

    translated_segments: List[Tuple[int, str]] = []
    for start_ms, _end_ms, txt in cues[:80]:
        tr_txt = translate_text_contextual(txt, lang)
        if tr_txt:
            translated_segments.append((start_ms, tr_txt))

    if not translated_segments:
        return None

    with tempfile.TemporaryDirectory(prefix="vi_dub_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        try:
            audio_parts = asyncio.run(_synth_edge_tts(translated_segments, voice, tmp_path))
        except Exception:
            return None

        if not audio_parts:
            return None

        dub_out = video_path.with_name(f"{video_path.stem} [Dub-{lang.upper()}]{video_path.suffix}")
        cmd = [ffmpeg_exe, "-y", "-i", str(video_path)]
        filter_parts = ["[0:a]volume=0.15[orig]"]
        mix_inputs = ["[orig]"]

        for i, (start_ms, seg_path) in enumerate(audio_parts, start=1):
            cmd.extend(["-i", str(seg_path)])
            delay = max(0, start_ms)
            filter_parts.append(f"[{i}:a]adelay={delay}|{delay}[d{i}]")
            mix_inputs.append(f"[d{i}]")

        mix_chain = "".join(mix_inputs) + f"amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=2[aout]"
        filter_parts.append(mix_chain)
        cmd.extend([
            "-filter_complex", ";".join(filter_parts),
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            str(dub_out),
        ])
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        if res.returncode == 0 and dub_out.exists() and dub_out.stat().st_size > 0:
            return dub_out
    return None
