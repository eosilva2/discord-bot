import os
import json
import time
import re
from typing import List
from collections import deque
import discord
from discord.ext import commands

CONFIG_FILE = "config.json"

# ---------------- Normalization helpers ----------------
LEET_MAP = str.maketrans({
    "0":"o","1":"i","!":"i","3":"e","4":"a","@":"a","$":"s","5":"s","7":"t","8":"b","+":"t","|":"l"
})

def normalize(text: str) -> str:
    """Lowercase, convert common leetspeak, strip punctuation/newlines, collapse spaces."""
    text = text.lower().translate(LEET_MAP)
    text = re.sub(r"[^a-z0-9@\s]", " ", text)  # keep @ so mentions are detectable before normalize if needed
    text = re.sub(r"\s+", " ", text).strip()
    return text

def has_word(norm_text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", norm_text) is not None

def any_word(norm_text: str, words: List[str]) -> bool:
    return any(has_word(norm_text, w) for w in words)

# ---------------- Config: prefer ENV, fallback to file ----------------
def load_config():
    words_env = os.getenv("WORDS")
    window_env = os.getenv("WINDOW")
    default_words = [w.strip().lower() for w in (words_env.split(",") if words_env else ["chunky","cheater"]) if w.strip()]
    try:
        default_window = int(window_env) if window_env else 30
    except ValueError:
        default_window = 30

    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

    cfg.setdefault("_default", {"words": default_words, "window": default_window})
    return cfg

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

config = load_config()  # {"<guild_id>": {words, window}, "_default": {words, window}}

def get_guild_cfg(guild_id: int):
    g = str(guild_id)
    if g not in config:
        config[g] = dict(config["_default"])
        save_config(config)
    return config[g]

# ---------------- Dynamic ban list with patterns ----------------
def spaced_pat(s: str) -> str:
    # "test" -> "t\s*e\s*s\s*t\s*" (matches t e s t, t---e__s t, etc.)
    return r"".join(re.escape(c) + r"\s*" for c in s)

def compile_ban_patterns(raw: str):
    patterns = []
    for item in [x.strip() for x in raw.split(",") if x.strip()]:
        is_stem = item.endswith("*")
        base = item[:-1] if is_stem else item
        base_norm = normalize(base)
        if not base_norm:
            continue
        if is_stem:
            pat = rf"\b{spaced_pat(base_norm)}[a-z0-9]*\b"   # stems: gay* -> gay/gays/g a y etc.
        else:
            pat = rf"\b{spaced_pat(base_norm)}\b"            # exact word (spacing tolerated)
        patterns.append(re.compile(pat))
    return patterns

BAN_PATTERNS = compile_ban_patterns(os.getenv("BAN_WORDS", ""))

# Always-ban baseline (compiled too)
ALWAYS_BAN = {"hoe", "hoes", "cunt", "bitch", "bitches"}
ALWAYS_PATTERNS = [re.compile(rf"\b{spaced_pat(normalize(w))}\b") for w in ALWAYS_BAN]

def contains_banned_word(norm_text: str) -> bool:
    for p in ALWAYS_PATTERNS:
        if p.search(norm_text):
            return True
    for p in BAN_PATTERNS:
        if p.search(norm_text):
            return True
    return False

# ---------------- Moderation rules (semantic) ----------------
PRONOUN_TARGETS = {
    "you","u","ur","youre","he","she","they","him","her","them",
    "this","that","it","these","those",
    "bro","bros","dude","man","guy","guys","girl","girls","boy","boys",
    "buddy","pal","homie","sis","brother","sister","dawg"
}
CHEAT_STEMS = {"cheat","cheater","cheating","cheated","cheats"}

def is_directed(norm_text: str, has_mention: bool) -> bool:
    return has_mention or any_word(norm_text, list(PRONOUN_TARGETS))

def is_cheater_accusation(norm_text: str, has_mention: bool) -> bool:
    # "<name> a|is a cheater/cheating"
    if re.search(r"\b\w{2,}\s+(?:is|s|is a|s a|a)\s+cheat\w*\b", norm_text):
        return True
    # directed accusations / imperatives
    if any_word(norm_text, list(CHEAT_STEMS)):
        if is_directed(norm_text, has_mention):
            return True
        if re.search(r"\bstop\s+cheat\w*\b", norm_text):
            return True
        if re.search(r"\b(is|are)\s+cheat\w*\b", norm_text):
            return True
    return False

def is_bitch_insult(norm_text: str, has_mention: bool) -> bool:
    # Single-word handled by ALWAYS_BAN; keep directed & "<name> a bitch"
    if re.search(r"\bbi?atch(es)?\b", norm_text) or has_word(norm_text, "bitch") or has_word(norm_text, "bitches"):
        if is_directed(norm_text, has_mention):
            return True
        if re.search(r"\b\w{2,}\s+(?:is|s|is a|s a|a)\s+bitch(?:es)?\b", norm_text):
            return True
    return False

def is_fuck_you_super_strict(norm_text: str) -> bool:
    if re.search(r"\bfu?c?k+\s*you\b", norm_text):  # fuck/fck/fuc you
        return True
    if re.search(r"\bf\s*you\b", norm_text):        # f you / f-you
        return True
    if re.search(r"\bf\s*u\b", norm_text):          # f u (even without "you")
        return True
    if re.search(r"\bfu?h+\s*u\b", norm_text):      # fuh u
        return True
    if re.search(r"\bfu\b", norm_text) and has_word(norm_text, "you"):
        return True
    return False

# Player implication
PLAYER_TERMS = {
    "player","playboy","womanizer","womaniser","womanizers","womanisers",
    "fboy","fboi","fuckboy","fuckboi","manwhore"
}
GIRL_WORDS = {"girl","girls","woman","women","female","females","hoe","hoes"}
QTY_WORDS = {"hella","many","every","all","lots","lot","alot","a lot"}

def is_player_implication(norm_text: str, has_mention: bool) -> bool:
    if any_word(norm_text, list(PLAYER_TERMS)) or "player" in norm_text:
        if is_directed(norm_text, has_mention):
            return True
        if re.search(r"\b\w{2,}\s+(?:is|s|is a|s a|a)\s+player\b", norm_text):
            return True
    if (any_word(norm_text, list(QTY_WORDS)) and any_word(norm_text, list(GIRL_WORDS))):
        if re.search(r"\b(got|has|have)\b", norm_text) or is_directed(norm_text, has_mention):
            return True
    qty = r"(a\s+lot\s+of|many|every|all|lots\s+of|hella)"
    girls = r"(girls?|women|females?|hoes?)"
    if re.search(rf"\b(talk|text|dm|message|chat)(s|ed|ing)?\s+(to|with)\s+{qty}\s+{girls}\b", norm_text):
        return True
    if re.search(rf"\b(flirt|rizz)(s|ed|ing)?\s+(with\s+)?{qty}\s+{girls}\b", norm_text):
        return True
    if re.search(r"\b(slide|sliding|slid|slides)\s+(in|into)\s+(\w+\s+)?dm(s)?\b", norm_text) and (
        any_word(norm_text, ["everyone","every","all","many","hella"]) or any_word(norm_text, list(GIRL_WORDS))
    ):
        return True
    return False

# Loyalty / serial-dating implication
def is_loyalty_implication(norm_text: str, has_mention: bool) -> bool:
    if re.search(r"\b\w{2,}\s+(?:lacks|lack|has\s+no|got\s+no|no)\s+loyalty\b", norm_text):
        return True
    if re.search(r"\b\w{2,}\s+(?:is|s|isn t|isnt|ain t|aint|not)\s+loyal\b", norm_text):
        return True
    if is_directed(norm_text, has_mention) and re.search(r"\b(disloyal|unloyal|unfaithful|not\s+faithful)\b", norm_text):
        return True
    if re.search(r"\b(go|goes|going|went|move|moves|moving|bounce|bounces|bouncing|hop|hops|hopping|switch|switches|switching|jump|jumps|jumping)\s+from\s+(girl|girls|woman|women|female|females)\s+to\s+(girl|girls|woman|women|female|females)\b", norm_text):
        return True
    if re.search(r"\b(new|another|different)\s+girl\s+(each|every|per)\s+(day|night|week|month)\b", norm_text):
        return True
    if re.search(r"\b(every|each)\s+(day|night|week|month)\s+(a\s+)?(new|different)\s+girl\b", norm_text):
        return True
    if re.search(r"\b(has|got|have)\s+(a\s+)?(roster|rotation)\b", norm_text) and any_word(norm_text, list(GIRL_WORDS)):
        return True
    return False

# ---------------- Recent message buffer (multi-message) ----------------
MAX_RECENT = 10  # keep last 10 messages per user per channel
recent_msgs = {}  # key: (guild_id, channel_id, user_id) -> deque[{"norm": str, "time": float, "had_mention": bool, "id": int}]

def get_recent_key(message: discord.Message):
    return (message.guild.id, message.channel.id, message.author.id)

def get_aggregate_text(dq: deque, now: float, window: int) -> tuple[str, bool]:
    # remove expired items
    while dq and (now - dq[0]["time"]) > window:
        dq.popleft()
    agg_text = " ".join(item["norm"] for item in dq)
    agg_mentions = any(item["had_mention"] for item in dq)
    return agg_text, agg_mentions

async def delete_recent_user_msgs(channel: discord.TextChannel, dq: deque, now: float, window: int):
    """Delete all recent messages from this user in the window (cleans up B I T C H sequences)."""
    for item in list(dq):
        if (now - item["time"]) <= window:
            try:
                msg = await channel.fetch_message(item["id"])
                await msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

# ---------------- Bot setup ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

def is_guild_manager():
    async def predicate(ctx):
        return ctx.author.guild_permissions.manage_guild
    return commands.check(predicate)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Ready.")

@bot.command(name="help")
async def help_cmd(ctx):
    gcfg = get_guild_cfg(ctx.guild.id)
    words = ", ".join(gcfg["words"])
    default_words = ", ".join(config["_default"]["words"])
    await ctx.reply(
        "**Commands (server managers only for config):**\n"
        "`!setwords word1, word2, ...` – set required word combo (at least 2 words)\n"
        "`!setwindow N` – set back-to-back time window in seconds (default 30)\n"
        "`!config` – show current settings\n\n"
        f"**Current (this server):** words = [{words}] | window = {gcfg['window']}s\n"
        f"**Defaults (from ENV):** [{default_words}] | window = {config['_default']['window']}s\n"
        "Built-ins: cheater accusations, bitch, super-strict 'f you', player, loyalty, BAN_WORDS, always-ban list, and spelled-out letters across messages."
    )

@bot.command(name="config")
async def config_cmd(ctx):
    gcfg = get_guild_cfg(ctx.guild.id)
    await ctx.reply(f"Words: {gcfg['words']}\nWindow: {gcfg['window']} seconds")

@bot.command(name="setwords")
@is_guild_manager()
async def setwords_cmd(ctx, *, args: str):
    words = [w.strip().lower() for w in args.split(",") if w.strip()]
    if len(words) < 2:
        await ctx.reply("Please provide at least **two** words, e.g. `!setwords chunky, cheater`")
        return
    gcfg = get_guild_cfg(ctx.guild.id)
    gcfg["words"] = words
    save_config(config)
    await ctx.reply(f"Updated word combo to: {words}")

@bot.command(name="setwindow")
@is_guild_manager()
async def setwindow_cmd(ctx, seconds: int):
    if seconds < 1 or seconds > 600:
        await ctx.reply("Please choose a window between 1 and 600 seconds.")
        return
    gcfg = get_guild_cfg(ctx.guild.id)
    gcfg["window"] = seconds
    save_config(config)
    await ctx.reply(f"Back-to-back window set to {seconds} seconds.")

def contains_all_words(text: str, words: List[str]) -> bool:
    text = text.lower()
    return all(w in text for w in words)

@bot.event
async def on_message(message: discord.Message):
    # Let commands run
    await bot.process_commands(message)
    if message.author.bot or not message.guild:
        return

    gcfg = get_guild_cfg(message.guild.id)
    combo_words = gcfg["words"]
    window = gcfg["window"]

    content_norm = normalize(message.content)
    had_mention = bool(message.mentions)
    now = time.time()

    # ---------- Single-message checks ----------
    if is_fuck_you_super_strict(content_norm):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        return

    if is_bitch_insult(content_norm, had_mention):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        return

    if is_cheater_accusation(content_norm, had_mention):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        return

    if is_player_implication(content_norm, had_mention):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        return

    if is_loyalty_implication(content_norm, had_mention):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        return

    if contains_banned_word(content_norm):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        return

    # ---------- Add to recent buffer ----------
    key = (message.guild.id, message.channel.id, message.author.id)
    dq = recent_msgs.get(key)
    if dq is None:
        dq = deque(maxlen=MAX_RECENT)
        recent_msgs[key] = dq
    dq.append({"norm": content_norm, "time": now, "had_mention": had_mention, "id": message.id})

    # ---------- Aggregate (multi-message) checks within window ----------
    agg_text, agg_mentions = get_aggregate_text(dq, now, window)

    if (
        contains_all_words(agg_text, combo_words) or
        contains_banned_word(agg_text) or
        is_fuck_you_super_strict(agg_text) or
        is_cheater_accusation(agg_text, agg_mentions) or
        is_player_implication(agg_text, agg_mentions) or
        is_loyalty_implication(agg_text, agg_mentions)
    ):
        await delete_recent_user_msgs(message.channel, dq, now, window)
        return

# ---------------- Run the bot ----------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    print("ERROR: Missing DISCORD_BOT_TOKEN")
    raise SystemExit(1)
bot.run(TOKEN)








