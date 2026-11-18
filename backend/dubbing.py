import array
import asyncio
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
import wave
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

NEURAL_VOICES_BY_GENDER: Dict[str, Dict[str, str]] = {
    "tr": {"male": "tr-TR-AhmetNeural", "female": "tr-TR-EmelNeural"},
    "en": {"male": "en-US-AndrewNeural", "female": "en-US-AvaNeural"},
    "de": {"male": "de-DE-ConradNeural", "female": "de-DE-KatjaNeural"},
    "ru": {"male": "ru-RU-DmitryNeural", "female": "ru-RU-SvetlanaNeural"},
    "zh": {"male": "zh-CN-YunxiNeural", "female": "zh-CN-XiaoxiaoNeural"},
    "es": {"male": "es-ES-AlvaroNeural", "female": "es-ES-ElviraNeural"},
    "fr": {"male": "fr-FR-HenriNeural", "female": "fr-FR-DeniseNeural"},
    "ar": {"male": "ar-SA-HamedNeural", "female": "ar-SA-ZariyahNeural"},
    "ja": {"male": "ja-JP-KeitaNeural", "female": "ja-JP-NanamiNeural"},
    "ko": {"male": "ko-KR-InJoonNeural", "female": "ko-KR-SunHiNeural"},
    "it": {"male": "it-IT-DiegoNeural", "female": "it-IT-ElsaNeural"},
    "pt": {"male": "pt-BR-AntonioNeural", "female": "pt-BR-FranciscaNeural"},
}


def estimate_speaker_profile(video_path: Path, ffmpeg_exe: str) -> Tuple[str, str, str]:
    """Analyze original speaker vocal gender, pitch, and speaking cadence from video audio."""
    try:
        with tempfile.TemporaryDirectory(prefix="vi_pitch_") as td:
            wav_path = Path(td) / "sample.wav"
            subprocess.run(
                [ffmpeg_exe, "-y", "-ss", "2", "-t", "12", "-i", str(video_path),
                 "-ac", "1", "-ar", "16000", "-vn", str(wav_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
            )
            if not wav_path.exists():
                return "male", "+0Hz", "+0%"
            with wave.open(str(wav_path), "rb") as wf:
                raw = wf.readframes(wf.getnframes())
            samples = array.array("h")
            samples.frombytes(raw)
            if len(samples) < 3200:
                return "male", "+0Hz", "+0%"

            step = 16000
            chunk = samples[: min(len(samples), step * 4)]
            best_lag = 120
            best_corr = -1.0
            for lag in range(57, 200):
                corr = sum(chunk[i] * chunk[i + lag] for i in range(0, len(chunk) - lag, 4))
                if corr > best_corr:
                    best_corr = corr
                    best_lag = lag
            est_hz = step / best_lag

            # Estimate speaking cadence from short-window energy bursts
            win = 1600  # 100ms
            active_transitions = 0
            was_active = False
            for idx in range(0, len(samples) - win, win):
                rms = sum(abs(samples[j]) for j in range(idx, idx + win, 8)) / (win / 8)
                is_act = rms > 600
                if is_act and not was_active:
                    active_transitions += 1
                was_active = is_act
            rate_str = "+6%" if active_transitions > 18 else ("-4%" if active_transitions < 8 else "+0%")

            if est_hz >= 165.0:
                pitch_str = "+6Hz" if est_hz > 205.0 else "+0Hz"
                return "female", pitch_str, rate_str
            pitch_str = "-6Hz" if est_hz < 110.0 else "+0Hz"
            return "male", pitch_str, rate_str
    except Exception:
        return "male", "+0Hz", "+0%"


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
    raw_cues: List[Tuple[int, int, List[str]]] = []

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
        if end_ms - start_ms < 120:
            continue
        cleaned_lines: List[str] = []
        for raw_ln in lines[ts_idx + 1 :]:
            ln = re.sub(r"<[^>]+>", "", raw_ln)
            ln = re.sub(r"\s+", " ", ln).strip()
            if ln:
                cleaned_lines.append(ln)
        if cleaned_lines:
            raw_cues.append((start_ms, end_ms, cleaned_lines))

    # Remove YouTube rolling-caption overlap (where line[0] repeats previous cue's last line)
    deduped: List[Tuple[int, int, str]] = []
    prev_lines: List[str] = []
    for start_ms, end_ms, cur_lines in raw_cues:
        if prev_lines and cur_lines[0] == prev_lines[-1]:
            new_lines = cur_lines[1:]
        elif prev_lines and cur_lines == prev_lines:
            new_lines = []
        else:
            new_lines = cur_lines
        prev_lines = cur_lines
        joined = " ".join(new_lines).strip()
        if not joined:
            continue
        if deduped and joined == deduped[-1][2]:
            continue
        deduped.append((start_ms, max(end_ms, start_ms + 700), joined))

    # Clamp end_ms so blocks never overlap and disappear/appear cleanly together
    for i in range(len(deduped) - 1):
        s_ms, e_ms, txt = deduped[i]
        next_s = deduped[i + 1][0]
        if e_ms >= next_s:
            deduped[i] = (s_ms, max(s_ms + 300, next_s - 40), txt)

    return merge_cues_into_sentences(deduped)


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


def translate_batch_with_gemini(
    texts: List[str], target_lang: str, api_key: str
) -> Optional[List[str]]:
    """Translate dialogue lines in batch using Gemini 2.5 Flash for natural spoken dubbing."""
    if not api_key or not texts:
        return None
    lang_names = {
        "tr": "Turkish", "en": "English", "de": "German", "ru": "Russian",
        "zh": "Chinese (Simplified)", "es": "Spanish", "fr": "French",
        "ar": "Arabic", "ja": "Japanese", "ko": "Korean", "it": "Italian", "pt": "Portuguese",
    }
    target_name = lang_names.get(target_lang.lower(), target_lang)
    prompt = (
        f"You are a professional film dubbing translator. Translate the following {len(texts)} "
        f"video dialogue lines into natural, idiomatic, spoken {target_name}. "
        f"Keep the exact same count ({len(texts)}) and order, preserve tone and meaning, "
        f"and make them concise enough to be spoken naturally in dubbing. "
        f"Return ONLY a valid JSON array of {len(texts)} translated strings.\n\n"
        + json.dumps(texts, ensure_ascii=False)
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={urllib.parse.quote(api_key.strip())}"
    )
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw_txt = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        parsed = json.loads(raw_txt)
        if isinstance(parsed, list) and len(parsed) == len(texts):
            return [str(x).strip() or orig for x, orig in zip(parsed, texts)]
    except Exception:
        return None
    return None


def translate_cues_contextual(
    texts: List[str], target_lang: str, gemini_api_key: str = ""
) -> List[str]:
    key = (gemini_api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
    if key:
        results: List[str] = []
        for i in range(0, len(texts), 20):
            chunk = texts[i : i + 20]
            gem = translate_batch_with_gemini(chunk, target_lang, key)
            if gem:
                results.extend(gem)
            else:
                results.extend(translate_text_contextual(t, target_lang) for t in chunk)
        return results
    return [translate_text_contextual(t, target_lang) for t in texts]


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


async def _synth_edge_tts(
    segments: List[Tuple[int, str]], voice: str, out_dir: Path, pitch: str = "+0Hz", rate: str = "+0%"
) -> List[Tuple[int, Path]]:
    import edge_tts

    audio_parts: List[Tuple[int, Path]] = []
    for idx, (start_ms, txt) in enumerate(segments):
        part_path = out_dir / f"seg_{idx:04d}.mp3"
        communicate = edge_tts.Communicate(txt, voice, pitch=pitch, rate=rate)
        await communicate.save(str(part_path))
        if part_path.exists() and part_path.stat().st_size > 0:
            audio_parts.append((start_ms, part_path))
    return audio_parts


def apply_ai_dubbing(
    video_path: Path, sub_path: Path, dub_lang: str, ffmpeg_exe: str, gemini_api_key: str = ""
) -> Optional[Path]:
    """Generate speaker-matched contextual neural TTS dubbing track and mux into the video."""
    lang = dub_lang.strip().lower()
    gender, pitch, rate = estimate_speaker_profile(video_path, ffmpeg_exe)
    voice = NEURAL_VOICES_BY_GENDER.get(lang, {}).get(gender) or NEURAL_VOICES.get(lang)
    if not voice or not video_path.exists() or not sub_path.exists():
        return None

    cues = parse_subtitle_cues(sub_path)
    if not cues:
        return None

    limited = cues[:80]
    translated_texts = translate_cues_contextual([c[2] for c in limited], lang, gemini_api_key)
    translated_segments: List[Tuple[int, str]] = [
        (c[0], tr_txt) for c, tr_txt in zip(limited, translated_texts) if tr_txt
    ]

    if not translated_segments:
        return None

    with tempfile.TemporaryDirectory(prefix="vi_dub_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        try:
            audio_parts = asyncio.run(_synth_edge_tts(translated_segments, voice, tmp_path, pitch=pitch, rate=rate))
        except Exception:
            return None

        if not audio_parts:
            return None

        dub_out = video_path.with_name(f"{video_path.stem} [Dub-{lang.upper()}]{video_path.suffix}")
        cmd = [ffmpeg_exe, "-y", "-i", str(video_path)]
        filter_parts = ["[0:a]volume=0[orig]"]
        mix_inputs = ["[orig]"]

        for i, (start_ms, seg_path) in enumerate(audio_parts, start=1):
            cmd.extend(["-i", str(seg_path)])
            delay = max(0, start_ms)
            filter_parts.append(f"[{i}:a]adelay={delay}|{delay}[d{i}]")
            mix_inputs.append(f"[d{i}]")

        mix_chain = (
            "".join(mix_inputs)
            + f"amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=2:normalize=0[aout]"
        )
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


def _fmt_srt_ts(seconds: float) -> str:
    ms = max(0, int(seconds * 1000))
    h, r = divmod(ms, 3600000)
    m, r = divmod(r, 60000)
    s, ml = divmod(r, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ml:03d}"


def transcribe_video_to_srt(
    video_path: Path, ffmpeg_exe: str, out_srt: Path, gemini_api_key: str = ""
) -> Optional[Path]:
    """Generate SRT subtitles from video audio using faster-whisper (or Gemini audio fallback)."""
    if not video_path.exists() or not ffmpeg_exe:
        return None
    with tempfile.TemporaryDirectory(prefix="vi_stt_") as td:
        wav_path = Path(td) / "audio16k.wav"
        mp3_path = Path(td) / "audio.mp3"
        subprocess.run(
            [ffmpeg_exe, "-y", "-i", str(video_path), "-ac", "1", "-ar", "16000", "-vn", str(wav_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180,
        )
        if not wav_path.exists() or wav_path.stat().st_size == 0:
            return None

        # 1. Offline faster-whisper
        try:
            from faster_whisper import WhisperModel

            model = WhisperModel("base", device="cpu", compute_type="int8")
            segments, _info = model.transcribe(str(wav_path), beam_size=3, vad_filter=True)
            blocks = []
            for idx, seg in enumerate(segments, start=1):
                txt = (seg.text or "").strip()
                if txt:
                    blocks.append(f"{idx}\n{_fmt_srt_ts(seg.start)} --> {_fmt_srt_ts(seg.end)}\n{txt}")
            if blocks:
                out_srt.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
                return out_srt
        except Exception:
            pass

        # 2. Gemini multimodal audio fallback if API key is set
        key = (gemini_api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
        if key:
            try:
                import base64

                subprocess.run(
                    [ffmpeg_exe, "-y", "-i", str(wav_path), "-b:a", "48k", str(mp3_path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
                )
                if mp3_path.exists() and mp3_path.stat().st_size <= 18_000_000:
                    b64 = base64.b64encode(mp3_path.read_bytes()).decode("ascii")
                    payload = {
                        "contents": [{
                            "parts": [
                                {"inlineData": {"mimeType": "audio/mp3", "data": b64}},
                                {"text": "Transcribe all spoken dialogue in this audio into valid SRT subtitle format with accurate timestamps. Return ONLY raw SRT text."},
                            ]
                        }]
                    }
                    url = (
                        "https://generativelanguage.googleapis.com/v1beta/models/"
                        f"gemini-2.5-flash:generateContent?key={urllib.parse.quote(key)}"
                    )
                    req = urllib.request.Request(
                        url, data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"}, method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    srt_txt = (
                        data.get("candidates", [{}])[0]
                        .get("content", {})
                        .get("parts", [{}])[0]
                        .get("text", "")
                    ).strip()
                    if "-->" in srt_txt:
                        out_srt.write_text(srt_txt + "\n", encoding="utf-8")
                        return out_srt
            except Exception:
                pass
    return None
