from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import re
import struct
import time
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, HORIZONTAL, LEFT, RIGHT, X, Y, filedialog, messagebox
import tkinter as tk
from tkinter import ttk


APP_CONFIG_PATH = Path.home() / ".heartopia_midi_bin_maker.json"


def heartopia_record_root() -> Path:
    return Path.home() / "AppData" / "LocalLow" / "xd" / "Heartopia" / "record"


def load_saved_output_dir() -> Path | None:
    try:
        raw = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    path = Path(str(raw.get("output_dir", "")))
    return path if path.exists() else None


def save_output_dir(path: Path) -> None:
    try:
        APP_CONFIG_PATH.write_text(
            json.dumps({"output_dir": str(path)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # Persisting the convenience setting is not required for conversion.
        pass


def find_record_dirs() -> list[Path]:
    root = heartopia_record_root()
    if not root.exists():
        return []
    candidates = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("_"):
            continue
        bin_files = list(child.glob("*.bin"))
        remote_bin_files = list((child / "remote").glob("*.bin")) if (child / "remote").exists() else []
        score = len(bin_files) * 10 + len(remote_bin_files)
        try:
            latest = child.stat().st_mtime
        except OSError:
            latest = 0.0
        for file in bin_files[:500] + remote_bin_files[:500]:
            try:
                latest = max(latest, file.stat().st_mtime)
            except OSError:
                pass
        candidates.append((score, latest, child))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in candidates]


def auto_detect_record_dir() -> Path:
    saved = load_saved_output_dir()
    if saved:
        return saved
    dirs = find_record_dirs()
    if dirs:
        return dirs[0]
    root = heartopia_record_root()
    return root if root.exists() else Path.home()


def newest_bin_files(folder: Path, limit: int = 10) -> list[Path]:
    try:
        files = [path for path in folder.glob("*.bin") if path.is_file()]
    except OSError:
        return []

    def modified_at(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(files, key=modified_at, reverse=True)[:limit]


def count_bin_files(folder: Path) -> int:
    try:
        return sum(1 for path in folder.glob("*.bin") if path.is_file())
    except OSError:
        return 0


@dataclasses.dataclass(frozen=True)
class InstrumentProfile:
    name: str
    instrument_id: int
    instrument_hash: int
    on_state: int
    off_state: int
    f2: float
    keys: tuple[int, ...]
    drum: bool = False


# Profiles confirmed from files recorded in-game:
# PianoFullMap, khong/cajon Map, and SaxMap.
PROFILES: dict[str, InstrumentProfile] = {
    "Piano": InstrumentProfile(
        name="Piano",
        instrument_id=2,
        instrument_hash=747743753,
        on_state=257,
        off_state=1,
        f2=1.0008,
        # Low row, middle row, top row. This is the pitch order used for mapping.
        keys=(
            10016,
            10211,
            10017,
            10212,
            10018,
            10019,
            10213,
            10020,
            10214,
            10021,
            10215,
            10022,
            10001,
            10201,
            10002,
            10202,
            10003,
            10004,
            10203,
            10005,
            10204,
            10006,
            10205,
            10007,
            10008,
            10206,
            10009,
            10207,
            10010,
            10011,
            10208,
            10012,
            10209,
            10013,
            10210,
            10014,
            10015,
        ),
    ),
    "Cajon": InstrumentProfile(
        name="Cajon",
        instrument_id=2,
        instrument_hash=747743753,
        on_state=259,
        off_state=3,
        f2=1.0773,
        keys=(10111, 10112, 10113, 10114, 10115, 10116, 10117, 10118),
        drum=True,
    ),
    "Sax": InstrumentProfile(
        name="Sax",
        instrument_id=2,
        instrument_hash=747743753,
        on_state=277,
        off_state=21,
        f2=0.4640,
        keys=(
            11176,
            11177,
            11178,
            11179,
            11180,
            11181,
            11182,
            11183,
            11184,
            11185,
            11186,
            11187,
            11188,
            11189,
            11190,
        ),
    ),
}


@dataclasses.dataclass
class MidiNote:
    start_tick: int
    end_tick: int
    channel: int
    note: int
    velocity: int
    track_name: str


@dataclasses.dataclass
class ParsedMidi:
    path: Path
    division: int
    tempos: list[tuple[int, int]]
    tracks: list[dict]


@dataclasses.dataclass
class SequenceConfig:
    path: Path
    instrument: str
    offset: float = 0.0
    transpose: int = 0


@dataclasses.dataclass
class RenderNote:
    start: float
    end: float
    key: int
    midi_note: int
    instrument: str
    sequence_name: str


@dataclasses.dataclass
class GameEvent:
    time_sec: float
    instrument_hash: int
    instrument_id: int
    state: int
    key: int
    z: int
    f2: float


def read_var_len(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    while True:
        if pos >= len(buf):
            raise ValueError("Unexpected end while reading MIDI varlen")
        b = buf[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            return value, pos


def parse_midi(path: Path) -> ParsedMidi:
    data = path.read_bytes()
    if data[:4] != b"MThd":
        raise ValueError(f"Not a MIDI file: {path}")
    header_len = struct.unpack(">I", data[4:8])[0]
    _fmt, track_count, division = struct.unpack(">HHH", data[8:14])
    if division & 0x8000:
        raise ValueError("SMPTE MIDI timing is not supported")

    pos = 8 + header_len
    tempos: list[tuple[int, int]] = []
    tracks: list[dict] = []
    for track_index in range(track_count):
        if data[pos : pos + 4] != b"MTrk":
            raise ValueError(f"Missing MTrk in {path}")
        length = struct.unpack(">I", data[pos + 4 : pos + 8])[0]
        pos += 8
        buf = data[pos : pos + length]
        pos += length

        p = 0
        tick = 0
        running_status: int | None = None
        name = ""
        events: list[tuple[int, int, str, int, int]] = []
        while p < len(buf):
            delta, p = read_var_len(buf, p)
            tick += delta
            if p >= len(buf):
                break
            b = buf[p]
            p += 1

            if b == 0xFF:
                meta_type = buf[p]
                p += 1
                meta_len, p = read_var_len(buf, p)
                payload = buf[p : p + meta_len]
                p += meta_len
                if meta_type == 0x03:
                    name = payload.decode("latin1", errors="ignore")
                elif meta_type == 0x51 and meta_len == 3:
                    tempos.append((tick, int.from_bytes(payload, "big")))
                continue

            if b in (0xF0, 0xF7):
                sysex_len, p = read_var_len(buf, p)
                p += sysex_len
                continue

            if b & 0x80:
                status = b
                running_status = status
                first_data = None
            else:
                if running_status is None:
                    raise ValueError("MIDI running status without status byte")
                status = running_status
                first_data = b

            event_type = status >> 4
            channel = status & 0x0F
            data_len = 1 if event_type in (0xC, 0xD) else 2
            values = [] if first_data is None else [first_data]
            while len(values) < data_len:
                values.append(buf[p])
                p += 1

            if event_type in (0x8, 0x9):
                note, velocity = values[0], values[1]
                kind = "on" if event_type == 0x9 and velocity > 0 else "off"
                events.append((tick, channel, kind, note, velocity))

        tracks.append({"index": track_index, "name": name, "events": events})

    tempos = sorted(tempos) or [(0, 500000)]
    if tempos[0][0] != 0:
        tempos.insert(0, (0, 500000))
    return ParsedMidi(path=path, division=division, tempos=tempos, tracks=tracks)


def tick_to_seconds(tick: int, division: int, tempos: list[tuple[int, int]]) -> float:
    seconds = 0.0
    last_tick = 0
    tempo = tempos[0][1]
    for next_tick, next_tempo in tempos[1:]:
        if tick < next_tick:
            break
        seconds += (next_tick - last_tick) * tempo / division / 1_000_000
        last_tick = next_tick
        tempo = next_tempo
    seconds += (tick - last_tick) * tempo / division / 1_000_000
    return seconds


def pair_notes(parsed: ParsedMidi, include_drums: bool) -> list[MidiNote]:
    notes: list[MidiNote] = []
    for track in parsed.tracks:
        stacks: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for tick, channel, kind, note, velocity in track["events"]:
            if channel == 9 and not include_drums:
                continue
            if channel != 9 and include_drums:
                continue
            key = (channel, note)
            if kind == "on":
                stacks.setdefault(key, []).append((tick, velocity))
            elif stacks.get(key):
                start_tick, start_velocity = stacks[key].pop(0)
                if tick > start_tick:
                    notes.append(
                        MidiNote(
                            start_tick=start_tick,
                            end_tick=tick,
                            channel=channel,
                            note=note,
                            velocity=start_velocity,
                            track_name=track["name"],
                        )
                    )
    notes.sort(key=lambda n: (n.start_tick, n.note))
    return notes


def map_linear(value: int, low: int, high: int, keys: tuple[int, ...]) -> int:
    if not keys:
        raise ValueError("Instrument has no keys")
    if high <= low:
        return keys[len(keys) // 2]
    ratio = (value - low) / (high - low)
    index = round(ratio * (len(keys) - 1))
    index = max(0, min(len(keys) - 1, index))
    return keys[index]


def build_sequence_events(
    seq: SequenceConfig,
) -> tuple[list[GameEvent], list[RenderNote], dict]:
    profile = PROFILES[seq.instrument]
    parsed = parse_midi(seq.path)
    notes = pair_notes(parsed, include_drums=profile.drum)
    if not notes:
        return [], [], {"notes": 0, "duration": 0.0}

    first_tick = min(note.start_tick for note in notes)
    game_events: list[GameEvent] = []
    render_notes: list[RenderNote] = []

    if profile.drum:
        unique_notes = sorted({note.note for note in notes})
        note_to_key = {
            note: map_linear(index, 0, max(1, len(unique_notes) - 1), profile.keys)
            for index, note in enumerate(unique_notes)
        }
        for note in notes:
            start = (
                tick_to_seconds(
                    note.start_tick - first_tick, parsed.division, parsed.tempos
                )
                + seq.offset
            )
            raw_len = tick_to_seconds(
                note.end_tick - note.start_tick, parsed.division, parsed.tempos
            )
            length = max(0.06, min(0.16, raw_len))
            end = start + length
            key = note_to_key[note.note]
            game_events.append(make_event(start, profile, profile.on_state, key))
            game_events.append(make_event(end, profile, profile.off_state, key))
            render_notes.append(
                RenderNote(
                    start=start,
                    end=end,
                    key=key,
                    midi_note=note.note,
                    instrument=seq.instrument,
                    sequence_name=seq.path.stem,
                )
            )
        stats = {
            "notes": len(notes),
            "duration": max(n.end for n in render_notes),
            "range": f"{min(unique_notes)}-{max(unique_notes)}",
        }
    else:
        shifted_notes = [note.note + seq.transpose for note in notes]
        min_note = min(shifted_notes)
        max_note = max(shifted_notes)
        for note, shifted in zip(notes, shifted_notes):
            start = (
                tick_to_seconds(
                    note.start_tick - first_tick, parsed.division, parsed.tempos
                )
                + seq.offset
            )
            end = (
                tick_to_seconds(
                    note.end_tick - first_tick, parsed.division, parsed.tempos
                )
                + seq.offset
            )
            if end <= start:
                continue
            key = map_linear(shifted, min_note, max_note, profile.keys)
            game_events.append(make_event(start, profile, profile.on_state, key))
            game_events.append(make_event(end, profile, profile.off_state, key))
            render_notes.append(
                RenderNote(
                    start=start,
                    end=end,
                    key=key,
                    midi_note=shifted,
                    instrument=seq.instrument,
                    sequence_name=seq.path.stem,
                )
            )
        stats = {
            "notes": len(render_notes),
            "duration": max(n.end for n in render_notes) if render_notes else 0.0,
            "range": f"{min_note}-{max_note}",
        }

    return game_events, render_notes, stats


def make_event(
    time_sec: float, profile: InstrumentProfile, state: int, key: int
) -> GameEvent:
    return GameEvent(
        time_sec=time_sec,
        instrument_hash=profile.instrument_hash,
        instrument_id=profile.instrument_id,
        state=state,
        key=key,
        z=0,
        f2=profile.f2,
    )


def build_bin(sequences: list[SequenceConfig]) -> tuple[bytes, float, list[RenderNote], dict]:
    all_events: list[GameEvent] = []
    all_notes: list[RenderNote] = []
    stats: dict[str, dict] = {}
    for seq in sequences:
        events, notes, seq_stats = build_sequence_events(seq)
        all_events.extend(events)
        all_notes.extend(notes)
        stats[seq.path.name] = seq_stats

    if not all_events:
        raise ValueError("No playable MIDI notes found in the selected sequences")

    off_states = {profile.off_state for profile in PROFILES.values()}
    all_events.sort(
        key=lambda e: (
            round(e.time_sec, 6),
            0 if e.state in off_states else 1,
            e.state,
            e.key,
        )
    )
    duration = max(event.time_sec for event in all_events)
    output = bytearray(b"DCER")
    output += struct.pack("<HII", 1, len(all_events), 1)
    for index, event in enumerate(all_events, start=2):
        tail = (
            struct.pack("<f", float(duration))
            if index == len(all_events) + 1
            else struct.pack("<I", index)
        )
        output += (
            struct.pack(
                "<fIIHHHf",
                float(event.time_sec),
                int(event.instrument_hash),
                int(event.instrument_id),
                int(event.state),
                int(event.key),
                int(event.z),
                float(event.f2),
            )
            + tail
        )

    return bytes(output), duration, all_notes, stats


def sanitize_title(title: str) -> str:
    title = title.strip() or "HeartopiaSong"
    title = re.sub(r'[<>:"/\\|?*]+', "_", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title[:80] or "HeartopiaSong"


def make_output_path(output_dir: Path, title: str, duration: float) -> Path:
    duration_ms = int(round(duration * 1000))
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]
    return output_dir / f"{sanitize_title(title)}_{stamp}_{duration_ms}.bin"


class HeartopiaMidiBinMaker(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Heartopia MIDI Bin Maker")
        self.geometry("1160x760")
        self.minsize(980, 620)

        self.sequences: list[SequenceConfig] = []
        self.render_notes: list[RenderNote] = []
        self.duration = 0.0
        self.playing = False
        self.play_started_at = 0.0
        self.play_start_time = 0.0

        self.title_var = tk.StringVar(value="ConanMapBand")
        self.output_dir_var = tk.StringVar(value=str(auto_detect_record_dir()))
        self.status_var = tk.StringVar(value="Ready")
        self.folder_status_var = tk.StringVar(value="")
        self.time_var = tk.StringVar(value="00:00.000 / 00:00.000")
        self.selected_instrument_var = tk.StringVar(value="Piano")
        self.offset_var = tk.StringVar(value="0.0")
        self.transpose_var = tk.StringVar(value="0")

        self._build_ui()
        self.output_dir_var.trace_add("write", lambda *_args: self._refresh_folder_check())
        self._refresh_folder_check()
        self.after(100, self._refresh_preview)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=BOTH, expand=True)

        top = ttk.Frame(root)
        top.pack(fill=X)
        ttk.Label(top, text="Song title").pack(side=LEFT)
        ttk.Entry(top, textvariable=self.title_var, width=32).pack(side=LEFT, padx=6)
        ttk.Label(top, text="Output folder").pack(side=LEFT, padx=(16, 0))
        ttk.Entry(top, textvariable=self.output_dir_var).pack(side=LEFT, fill=X, expand=True, padx=6)
        ttk.Button(top, text="Auto detect", command=self._auto_detect_output).pack(side=LEFT)
        ttk.Button(top, text="Browse", command=self._browse_output).pack(side=LEFT)

        main = ttk.PanedWindow(root, orient=HORIZONTAL)
        main.pack(fill=BOTH, expand=True, pady=10)

        left = ttk.Frame(main)
        right = ttk.Frame(main)
        main.add(left, weight=3)
        main.add(right, weight=2)

        self.tree = ttk.Treeview(
            left,
            columns=("instrument", "offset", "transpose", "notes", "duration"),
            show="tree headings",
            height=11,
        )
        self.tree.heading("#0", text="MIDI sequence")
        self.tree.heading("instrument", text="Instrument")
        self.tree.heading("offset", text="Offset")
        self.tree.heading("transpose", text="Transpose")
        self.tree.heading("notes", text="Notes")
        self.tree.heading("duration", text="Duration")
        self.tree.column("#0", width=320)
        self.tree.column("instrument", width=90, anchor="center")
        self.tree.column("offset", width=70, anchor="center")
        self.tree.column("transpose", width=80, anchor="center")
        self.tree.column("notes", width=70, anchor="center")
        self.tree.column("duration", width=90, anchor="center")
        self.tree.pack(fill=BOTH, expand=False)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._load_selected_to_editor())

        buttons = ttk.Frame(left)
        buttons.pack(fill=X, pady=6)
        ttk.Button(buttons, text="Add MIDI", command=self._add_midi).pack(side=LEFT)
        ttk.Button(buttons, text="Remove", command=self._remove_selected).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="Up", command=lambda: self._move_selected(-1)).pack(side=LEFT)
        ttk.Button(buttons, text="Down", command=lambda: self._move_selected(1)).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="Refresh preview", command=self._refresh_preview).pack(side=RIGHT)

        edit = ttk.LabelFrame(left, text="Selected sequence settings", padding=8)
        edit.pack(fill=X, pady=(4, 8))
        ttk.Label(edit, text="Instrument").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            edit,
            textvariable=self.selected_instrument_var,
            values=list(PROFILES.keys()),
            state="readonly",
            width=12,
        ).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(edit, text="Start offset (sec)").grid(row=0, column=2, sticky="w", padx=(18, 0))
        ttk.Entry(edit, textvariable=self.offset_var, width=10).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Label(edit, text="Transpose").grid(row=0, column=4, sticky="w", padx=(18, 0))
        ttk.Entry(edit, textvariable=self.transpose_var, width=8).grid(row=0, column=5, sticky="w", padx=6)
        ttk.Button(edit, text="Apply to selected", command=self._apply_selected_settings).grid(
            row=0, column=6, padx=(18, 0)
        )

        timeline_frame = ttk.LabelFrame(left, text="Visual rhythm preview (no audio)", padding=8)
        timeline_frame.pack(fill=BOTH, expand=True)
        self.canvas = tk.Canvas(timeline_frame, background="#16191d", height=260, highlightthickness=0)
        self.canvas.pack(fill=BOTH, expand=True)

        playbar = ttk.Frame(left)
        playbar.pack(fill=X, pady=8)
        ttk.Button(playbar, text="Play visual", command=self._play).pack(side=LEFT)
        ttk.Button(playbar, text="Pause", command=self._pause).pack(side=LEFT, padx=4)
        ttk.Button(playbar, text="Stop", command=self._stop).pack(side=LEFT)
        self.time_scale = ttk.Scale(
            playbar,
            from_=0,
            to=1,
            orient=HORIZONTAL,
            command=self._scrub,
        )
        self.time_scale.pack(side=LEFT, fill=X, expand=True, padx=10)
        ttk.Label(playbar, textvariable=self.time_var, width=20).pack(side=RIGHT)

        profile_box = ttk.LabelFrame(right, text="Confirmed instrument profiles", padding=8)
        profile_box.pack(fill=X)
        profile_text = tk.Text(profile_box, height=12, wrap="word")
        profile_text.pack(fill=X)
        profile_text.insert(END, self._profile_summary())
        profile_text.configure(state="disabled")

        export_box = ttk.LabelFrame(right, text="Export", padding=8)
        export_box.pack(fill=X, pady=10)
        ttk.Button(export_box, text="Build .bin", command=self._build_file).pack(fill=X)
        ttk.Label(export_box, textvariable=self.status_var, wraplength=330).pack(fill=X, pady=(8, 0))

        folder_box = ttk.LabelFrame(right, text="Record folder check", padding=8)
        folder_box.pack(fill=X, pady=(0, 10))
        ttk.Label(folder_box, textvariable=self.folder_status_var, wraplength=330).pack(fill=X)
        folder_buttons = ttk.Frame(folder_box)
        folder_buttons.pack(fill=X, pady=(6, 4))
        ttk.Button(folder_buttons, text="Refresh folder", command=self._refresh_folder_check).pack(side=LEFT)
        ttk.Button(folder_buttons, text="Auto detect", command=self._auto_detect_output).pack(side=LEFT, padx=4)
        self.folder_text = tk.Text(folder_box, height=8, wrap="none")
        self.folder_text.pack(fill=X)
        self.folder_text.configure(state="disabled")

        log_box = ttk.LabelFrame(right, text="Process log", padding=8)
        log_box.pack(fill=BOTH, expand=True)
        self.log = tk.Text(log_box, height=12, wrap="word")
        self.log.pack(fill=BOTH, expand=True)

    def _profile_summary(self) -> str:
        lines = []
        for profile in PROFILES.values():
            lines.append(
                f"{profile.name}: on/off {profile.on_state}/{profile.off_state}, "
                f"f2 {profile.f2}, keys {profile.keys[0]}-{profile.keys[-1]} "
                f"({len(profile.keys)} keys)"
            )
        lines.append("")
        lines.append("Tip: one .bin can contain multiple MIDI sequences. Each sequence can use a different profile.")
        return "\n".join(lines)

    def _refresh_folder_check(self) -> None:
        folder = Path(self.output_dir_var.get())
        lines: list[str] = []
        if not folder.exists():
            self.folder_status_var.set("Folder does not exist yet. Open the game once or use Auto detect/Browse.")
        elif not folder.is_dir():
            self.folder_status_var.set("Selected output path is not a folder.")
        else:
            bin_count = count_bin_files(folder)
            if bin_count:
                self.folder_status_var.set(
                    f"Found {bin_count} .bin file(s). This is likely correct if these names match the in-game list."
                )
                for path in newest_bin_files(folder, limit=10):
                    lines.append(self._format_record_file(path))
            else:
                self.folder_status_var.set(
                    "No .bin files found here. If the game already shows saved songs, this is probably the wrong profile folder."
                )
                candidates = find_record_dirs()
                if candidates:
                    lines.append("Auto-detect candidates:")
                    for candidate in candidates[:5]:
                        lines.append(f"{count_bin_files(candidate):>3} bin  {candidate}")
        if not lines:
            lines.append("No record files to show.")
        self.folder_text.configure(state="normal")
        self.folder_text.delete("1.0", END)
        self.folder_text.insert(END, "\n".join(lines))
        self.folder_text.configure(state="disabled")

    def _format_record_file(self, path: Path) -> str:
        title = path.stem
        duration_text = ""
        parts = path.stem.rsplit("_", 2)
        if len(parts) == 3 and parts[2].isdigit():
            title = parts[0]
            duration_text = format_time(int(parts[2]) / 1000.0)
        try:
            modified_text = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            modified_text = "unknown time"
        if duration_text:
            return f"{title}  {duration_text}  {modified_text}\n  {path.name}"
        return f"{path.name}  {modified_text}"

    def _browse_output(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_dir_var.get())
        if folder:
            self.output_dir_var.set(folder)
            save_output_dir(Path(folder))

    def _auto_detect_output(self) -> None:
        dirs = find_record_dirs()
        if not dirs:
            self.output_dir_var.set(str(auto_detect_record_dir()))
            messagebox.showwarning(
                "Auto detect",
                "No Heartopia record profile folder was found. Use Browse after opening the game once.",
            )
            return
        chosen = dirs[0]
        self.output_dir_var.set(str(chosen))
        save_output_dir(chosen)
        detail = "\n".join(str(path) for path in dirs[:5])
        self.status_var.set(f"Detected output folder: {chosen}")
        messagebox.showinfo("Auto detect", f"Selected:\n{chosen}\n\nCandidates:\n{detail}")

    def _add_midi(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select MIDI files",
            filetypes=(("MIDI files", "*.mid *.midi"), ("All files", "*.*")),
        )
        for raw in paths:
            path = Path(raw)
            guessed = self._guess_instrument(path)
            self.sequences.append(SequenceConfig(path=path, instrument=guessed))
        self._refresh_preview()

    def _guess_instrument(self, path: Path) -> str:
        lower = path.name.lower()
        if "drum" in lower or "drun" in lower or "cajon" in lower:
            return "Cajon"
        if "sax" in lower:
            return "Sax"
        if "piano" in lower:
            return "Piano"
        return self.selected_instrument_var.get() or "Piano"

    def _selected_index(self) -> int | None:
        selected = self.tree.selection()
        if not selected:
            return None
        try:
            return int(selected[0])
        except ValueError:
            return None

    def _remove_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        del self.sequences[index]
        self._refresh_preview()

    def _move_selected(self, direction: int) -> None:
        index = self._selected_index()
        if index is None:
            return
        new_index = index + direction
        if not (0 <= new_index < len(self.sequences)):
            return
        self.sequences[index], self.sequences[new_index] = (
            self.sequences[new_index],
            self.sequences[index],
        )
        self._refresh_preview(select_index=new_index)

    def _load_selected_to_editor(self) -> None:
        index = self._selected_index()
        if index is None or index >= len(self.sequences):
            return
        seq = self.sequences[index]
        self.selected_instrument_var.set(seq.instrument)
        self.offset_var.set(str(seq.offset))
        self.transpose_var.set(str(seq.transpose))

    def _apply_selected_settings(self) -> None:
        index = self._selected_index()
        if index is None or index >= len(self.sequences):
            return
        try:
            offset = float(self.offset_var.get() or "0")
            transpose = int(float(self.transpose_var.get() or "0"))
        except ValueError:
            messagebox.showerror("Invalid settings", "Offset must be a number and transpose must be an integer.")
            return
        instrument = self.selected_instrument_var.get()
        if instrument not in PROFILES:
            messagebox.showerror("Invalid instrument", "Please select a known instrument profile.")
            return
        self.sequences[index] = dataclasses.replace(
            self.sequences[index],
            instrument=instrument,
            offset=max(0.0, offset),
            transpose=transpose,
        )
        self._refresh_preview(select_index=index)

    def _refresh_preview(self, select_index: int | None = None) -> None:
        self.tree.delete(*self.tree.get_children())
        self.render_notes = []
        self.duration = 0.0
        for index, seq in enumerate(self.sequences):
            notes_text = "-"
            duration_text = "-"
            try:
                _events, notes, stats = build_sequence_events(seq)
                self.render_notes.extend(notes)
                if notes:
                    self.duration = max(self.duration, max(note.end for note in notes))
                notes_text = str(stats.get("notes", "-"))
                duration_text = format_time(float(stats.get("duration", 0.0)))
            except Exception as exc:
                notes_text = "ERR"
                duration_text = str(exc)[:18]
            self.tree.insert(
                "",
                END,
                iid=str(index),
                text=seq.path.name,
                values=(seq.instrument, seq.offset, seq.transpose, notes_text, duration_text),
            )
        if select_index is not None and 0 <= select_index < len(self.sequences):
            self.tree.selection_set(str(select_index))
        self.time_scale.configure(to=max(0.001, self.duration))
        self._draw_timeline(float(self.time_scale.get()))
        self._update_time_label(float(self.time_scale.get()))

    def _draw_timeline(self, current_time: float) -> None:
        self.canvas.delete("all")
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        left_margin = 90
        right_margin = 18
        top_margin = 18
        lane_height = 58
        duration = max(0.001, self.duration)
        lanes = {name: idx for idx, name in enumerate(PROFILES.keys())}
        colors = {"Piano": "#4cc9f0", "Cajon": "#f59e0b", "Sax": "#ef476f"}

        for name, lane in lanes.items():
            y = top_margin + lane * lane_height
            self.canvas.create_text(10, y + 18, text=name, fill="#d7dde8", anchor="w")
            self.canvas.create_line(left_margin, y + 34, width - right_margin, y + 34, fill="#303640")

        if not self.render_notes:
            self.canvas.create_text(
                width / 2,
                height / 2,
                text="Add MIDI files to preview rhythm",
                fill="#9aa4b2",
            )
            return

        def x_for(t: float) -> float:
            return left_margin + (t / duration) * (width - left_margin - right_margin)

        for note in self.render_notes:
            lane = lanes.get(note.instrument, 0)
            y = top_margin + lane * lane_height + 22
            x1 = x_for(note.start)
            x2 = max(x1 + 2, x_for(note.end))
            self.canvas.create_rectangle(
                x1,
                y,
                x2,
                y + 20,
                fill=colors.get(note.instrument, "#8b5cf6"),
                outline="",
            )

        play_x = x_for(min(max(0.0, current_time), duration))
        self.canvas.create_line(play_x, 0, play_x, height, fill="#ffffff", width=2)

        for seconds in nice_ticks(duration):
            x = x_for(seconds)
            self.canvas.create_line(x, height - 22, x, height - 14, fill="#687082")
            self.canvas.create_text(x, height - 8, text=format_time(seconds, short=True), fill="#9aa4b2")

    def _play(self) -> None:
        if self.duration <= 0:
            return
        self.playing = True
        self.play_start_time = float(self.time_scale.get())
        self.play_started_at = time.perf_counter()
        self._play_tick()

    def _pause(self) -> None:
        self.playing = False

    def _stop(self) -> None:
        self.playing = False
        self.time_scale.set(0)
        self._draw_timeline(0)
        self._update_time_label(0)

    def _scrub(self, value: str) -> None:
        if not self.playing:
            current = float(value)
            self._draw_timeline(current)
            self._update_time_label(current)

    def _play_tick(self) -> None:
        if not self.playing:
            return
        elapsed = time.perf_counter() - self.play_started_at
        current = self.play_start_time + elapsed
        if current >= self.duration:
            current = self.duration
            self.playing = False
        self.time_scale.set(current)
        self._draw_timeline(current)
        self._update_time_label(current)
        if self.playing:
            self.after(33, self._play_tick)

    def _update_time_label(self, current: float) -> None:
        self.time_var.set(f"{format_time(current)} / {format_time(self.duration)}")

    def _build_file(self) -> None:
        if not self.sequences:
            messagebox.showwarning("No MIDI", "Please add at least one MIDI sequence.")
            return
        output_dir = Path(self.output_dir_var.get())
        output_dir.mkdir(parents=True, exist_ok=True)
        save_output_dir(output_dir)
        try:
            data, duration, notes, stats = build_bin(self.sequences)
            out_path = make_output_path(output_dir, self.title_var.get(), duration)
            out_path.write_bytes(data)
        except Exception as exc:
            messagebox.showerror("Build failed", str(exc))
            return

        self.status_var.set(f"Built: {out_path}")
        self._refresh_folder_check()
        self._log(f"Built {out_path}")
        self._log(f"Duration: {duration:.3f}s, events: {(len(data) - 14) // 26}, notes: {len(notes)}")
        for name, seq_stats in stats.items():
            self._log(f"{name}: {seq_stats}")
        messagebox.showinfo("Build complete", f"Created:\n{out_path}")

    def _log(self, text: str) -> None:
        self.log.insert(END, text + "\n")
        self.log.see(END)


def nice_ticks(duration: float) -> list[float]:
    if duration <= 0:
        return []
    step = 5.0
    if duration > 120:
        step = 30.0
    elif duration > 60:
        step = 15.0
    elif duration < 20:
        step = 2.0
    ticks = []
    value = 0.0
    while value <= duration + 0.001:
        ticks.append(value)
        value += step
    return ticks


def format_time(seconds: float, short: bool = False) -> str:
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    if short:
        return f"{minutes}:{int(secs):02d}"
    return f"{minutes:02d}:{secs:06.3f}"


def self_test(paths: list[str]) -> None:
    sequences = []
    for raw in paths:
        path = Path(raw)
        lower = path.name.lower()
        instrument = "Piano"
        if "drum" in lower or "drun" in lower:
            instrument = "Cajon"
        elif "sax" in lower:
            instrument = "Sax"
        sequences.append(SequenceConfig(path=path, instrument=instrument))
    data, duration, notes, stats = build_bin(sequences)
    print(f"duration={duration:.3f}s events={(len(data)-14)//26} notes={len(notes)}")
    for key, value in stats.items():
        print(key, value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", nargs="*", help="Parse and build from MIDI files without opening the GUI")
    args = parser.parse_args()
    if args.self_test is not None:
        self_test(args.self_test)
        return
    app = HeartopiaMidiBinMaker()
    app.mainloop()


if __name__ == "__main__":
    main()
