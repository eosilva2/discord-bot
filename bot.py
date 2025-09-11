# ======== LIVE LYRICS ADD-ON ========
# Requires: pip install lrclibapi
import math
import aiohttp
from lrclib import LrcLibAPI
from dataclasses import dataclass

LYRICS_USER_AGENT = os.getenv("LRCLIB_USER_AGENT", "discord-lyrics-bot/1.0")

# Keep one active session per guild so you don’t get duplicates
_active_lyrics: dict[int, "LyricsSession"] = {}

@dataclass
class LrcLine:
    t: float   # seconds from start
    text: str

class LyricsSession:
    def __init__(self, guild_id: int, channel: discord.TextChannel, track: str, artist: str,
                 lrc_lines: list[LrcLine], duration: int | None):
        self.guild_id = guild_id
        self.channel = channel
        self.track = track
        self.artist = artist
        self.lines = sorted(lrc_lines, key=lambda x: x.t)
        self.duration = duration
        self.offset = 0.0
        self._task: asyncio.Task | None = None
        self._start_monotonic = time.monotonic()
        self._msg: discord.Message | None = None
        self._last_index = -1
        self._stopped = asyncio.Event()

    def now(self) -> float:
        return time.monotonic() - self._start_monotonic + self.offset

    def stop(self):
        self._stopped.set()

    async def run(self):
        # create/update a single message that we keep editing
        header = f"🎤 **{self.artist} — {self.track}**"
        try:
            self._msg = await self.channel.send(header + "\nStarting…")
        except discord.Forbidden:
            return

        while not self._stopped.is_set():
            t = self.now()
            i = self._find_current_index(t)
            if i != self._last_index:
                self._last_index = i
                prev_line = self.lines[i-1].text if i > 0 else " "
                curr_line = self.lines[i].text if 0 <= i < len(self.lines) else "【End】"
                next_line = self.lines[i+1].text if i+1 < len(self.lines) else " "

                body = (
                    f"{header}\n"
                    f"```text\n"
                    f"{truncate(prev_line)}\n"
                    f"> {truncate(curr_line)}\n"
                    f"{truncate(next_line)}\n"
                    f"```\n"
                    f"_/lyrics offset ±seconds • /lyrics stop_"
                )
                try:
                    await self._msg.edit(content=body)
                except discord.HTTPException:
                    pass

            # graceful stop at end (+ a tiny buffer)
            if self.duration and t > (self.duration + 5):
                break

            await asyncio.sleep(0.25)

        # tidy
        _active_lyrics.pop(self.guild_id, None)
        try:
            if self._msg:
                await self._msg.edit(content=f"🎤 **{self.artist} — {self.track}**\n_lyrics session ended_")
        except discord.HTTPException:
            pass

    def _find_current_index(self, t: float) -> int:
        # binary search would be faster; linear is fine for a few hundred lines
        idx = 0
        for i, line in enumerate(self.lines):
            if line.t <= t:
                idx = i
            else:
                break
        return idx

def truncate(s: str, n: int = 140) -> str:
    return s if len(s) <= n else s[: n-1] + "…"

# ---------- LRCLIB helpers ----------
_api = LrcLibAPI(user_agent=LYRICS_USER_AGENT)

async def fetch_lrclib(track: str, artist: str, album: str | None = None, duration: int | None = None):
    """
    Returns (lrc_text, duration_seconds) or (None, None) if not found.
    Uses LRCLIB get_lyrics; will fall back to a search if needed.
    """
    loop = asyncio.get_running_loop()

    def _get():
        try:
            return _api.get_lyrics(
                track_name=track,
                artist_name=artist,
                album_name=album or "",
                duration=int(duration) if duration else None,
            )
        except Exception:
            return None

    lyrics = await loop.run_in_executor(None, _get)
    if not lyrics:
        # fallback: try search then get by id
        def _search_get():
            try:
                res = _api.search_lyrics(track_name=track, artist_name=artist)
                if not res:
                    return None
                # pick the closest by duration if available
                if duration:
                    best = min(res, key=lambda r: abs((r.duration or duration) - duration))
                else:
                    best = res[0]
                return _api.get_lyrics_by_id(best.id)
            except Exception:
                return None
        lyrics = await loop.run_in_executor(None, _search_get)

    if not lyrics:
        return None, None

    lrc_text = lyrics.synced_lyrics or None
    plain = lyrics.plain_lyrics or None
    dur = lyrics.duration or duration or None
    # Prefer synced; if only plain, we’ll pseudo-time it later
    return (lrc_text or plain), (dur if isinstance(dur, int) else None)

def parse_lrc_to_lines(lrc_text: str, duration_hint: int | None) -> list[LrcLine]:
    """
    Parse LRC into time-tagged lines.
    If text is not LRC (no [mm:ss] tags), fake timestamps across duration.
    """
    tag_re = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:\.(\d{1,2}))?\]")
    lines: list[LrcLine] = []

    has_tags = bool(tag_re.search(lrc_text or ""))

    if has_tags:
        for raw in lrc_text.splitlines():
            tags = list(tag_re.finditer(raw))
            if not tags:
                continue
            content = tag_re.sub("", raw).strip()
            if not content:
                continue
            for m in tags:
                mm = int(m.group(1))
                ss = int(m.group(2))
                cs = int(m.group(3) or 0)
                t = mm * 60 + ss + (cs / (10 ** len(m.group(3))) if m.group(3) else 0)
                lines.append(LrcLine(t=t, text=content))
    else:
        # plain lyrics — distribute evenly
        stripped = [ln.strip() for ln in (lrc_text or "").splitlines() if ln.strip()]
        if not stripped:
            return []
        dur = duration_hint or max(120, len(stripped) * 3)  # guess
        step = max(1.5, dur / max(1, len(stripped)))
        t = 0.0
        for ln in stripped:
            lines.append(LrcLine(t=t, text=ln))
            t += step

    # de-dup consecutive identical lines and sort
    cleaned = []
    last = None
    for ln in sorted(lines, key=lambda x: x.t):
        if ln.text != last:
            cleaned.append(ln)
            last = ln.text
    return cleaned

# ---------- “Now Playing” auto-detect (best effort) ----------
async def detect_np_in_channel(channel: discord.TextChannel) -> tuple[str | None, str | None, int | None]:
    """
    Try to find a recent "now playing" message (common patterns many music bots use).
    Return (track, artist, duration_seconds)
    """
    async for msg in channel.history(limit=50):
        # prefer bot messages with embeds
        if msg.author.bot:
            # 1) Check embed “Now Playing” style
            for emb in msg.embeds:
                text = " ".join(filter(None, [emb.title or "", emb.description or ""])).lower()
                if "now playing" in text or "playing" in text:
                    # Try to parse "Artist - Track" or "Track - Artist"
                    parts = split_title_artist(emb.title or emb.description or "")
                    if parts:
                        return parts[1], parts[0], parse_duration_from_text(emb.description or emb.title or "")
            # 2) Check plain content
            lower = (msg.content or "").lower()
            if "now playing" in lower or "playing:" in lower:
                parts = split_title_artist(msg.content)
                if parts:
                    return parts[1], parts[0], parse_duration_from_text(msg.content)
    return None, None, None

def split_title_artist(s: str) -> tuple[str, str] | None:
    # Try common splits: "Artist - Track", "Track — Artist", "Track by Artist"
    if " - " in s:
        a, b = s.split(" - ", 1)
        # We don’t know order; guess by “by”
        if " by " in b.lower():
            # "Track - by Artist"
            b = b.split(" by ", 1)[1]
        return a.strip(), b.strip()
    if " — " in s:
        a, b = s.split(" — ", 1)
        return a.strip(), b.strip()
    m = re.search(r"(.+?)\s+by\s+(.+)", s, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None

def parse_duration_from_text(s: str) -> int | None:
    # find 3:45 or 03:45 in string
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if not m: return None
    return int(m.group(1))*60 + int(m.group(2))

# ---------- Slash commands ----------
lyrics_group = app_commands.Group(name="lyrics", description="Live lyrics controls")
bot.tree.add_command(lyrics_group)

@lyrics_group.command(name="start", description="Start live lyrics in this channel (or pick a channel).")
@app_commands.describe(query="Optional: 'Song Title - Artist' (leave empty to auto-detect Now Playing)",
                       channel="Where to post the lyrics")
@app_commands.guild_only()
async def lyrics_start(interaction: discord.Interaction,
                       query: str | None = None,
                       channel: Optional[discord.TextChannel] = None):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server to use this.", ephemeral=True)
        return

    ch = channel or interaction.channel
    await interaction.response.defer(ephemeral=True, thinking=True)

    track = artist = None
    duration = None

    if query:
        parts = split_title_artist(query)
        if parts:
            # parts returns (a,b); but we don’t know which is artist/title; assume "Title - Artist" first, swap as needed
            # Try both ways (we’ll search either way)
            title_guess, artist_guess = parts[0], parts[1]
            track, artist = title_guess.strip(), artist_guess.strip()
        else:
            # fallback: assume "Title" only
            track = query.strip()
    else:
        # auto-detect from channel history (music bot messages)
        t, a, d = await detect_np_in_channel(ch)
        if t and a:
            track, artist, duration = t, a, d

    if not track:
        await interaction.followup.send("Couldn’t detect the current track. Provide a `query` like `Song - Artist`.", ephemeral=True)
        return

    # fetch lyrics from LRCLIB
    lrc_text, found_dur = await fetch_lrclib(track=track, artist=artist or "", duration=duration)
    if not lrc_text:
        await interaction.followup.send(f"Couldn’t find lyrics for **{artist or '?'} — {track}**.", ephemeral=True)
        return

    lines = parse_lrc_to_lines(lrc_text, duration_hint=found_dur)
    if not lines:
        await interaction.followup.send("Lyrics exist but couldn’t be parsed.", ephemeral=True)
        return

    # stop old session if any
    old = _active_lyrics.get(interaction.guild_id)
    if old:
        old.stop()

    sess = LyricsSession(
        guild_id=interaction.guild_id,
        channel=ch,
        track=track,
        artist=artist or "Unknown",
        lrc_lines=lines,
        duration=found_dur,
    )
    _active_lyrics[interaction.guild_id] = sess
    sess._task = asyncio.create_task(sess.run())
    await interaction.followup.send(f"Starting live lyrics in <#{ch.id}> for **{sess.artist} — {sess.track}**.", ephemeral=True)

@lyrics_group.command(name="stop", description="Stop live lyrics in this server")
@app_commands.guild_only()
async def lyrics_stop(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server to use this.", ephemeral=True)
        return
    sess = _active_lyrics.get(interaction.guild_id)
    if not sess:
        await interaction.response.send_message("No active lyrics session.", ephemeral=True)
        return
    sess.stop()
    await interaction.response.send_message("Stopped the lyrics session. ✅", ephemeral=True)

@lyrics_group.command(name="offset", description="Adjust sync by +/- seconds (e.g., -1.2 or 0.5)")
@app_commands.describe(seconds="Negative to move earlier, positive to move later")
@app_commands.guild_only()
async def lyrics_offset(interaction: discord.Interaction, seconds: float):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server to use this.", ephemeral=True)
        return
    sess = _active_lyrics.get(interaction.guild_id)
    if not sess:
        await interaction.response.send_message("No active lyrics session.", ephemeral=True)
        return
    sess.offset += seconds
    await interaction.response.send_message(f"Offset set to {sess.offset:+.2f}s.", ephemeral=True)

# Ensure slash cmds sync (your on_ready already syncs; this keeps guild-fast path working)
try:
    if GUILD_ID:
        asyncio.get_event_loop().create_task(bot.tree.sync(guild=discord.Object(id=GUILD_ID)))
except Exception:
    pass

# ======== END LIVE LYRICS ADD-ON ========

















