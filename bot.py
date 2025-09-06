import os
import json
import time
import re
from typing import List
import discord
from discord.ext import commands

CONFIG_FILE = "config.json"

# ---------------- Normalization helpers ----------------
LEET_MAP = str.maketrans({
    "0":"o","1":"i","!":"i","3":"e","4":"a","@":"a","$":"s","5":"s","7":"t","8":"b","+":"t","|":"l"
})

def normalize(text: str) -> str:
    text = text.lower().translate(LEET_MAP)
    text = re.sub(r"[^a-z0-9@\s]", " ", text)   # keep @ so mentions still detectable upstream
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

config = load_config()  # { "<guild_id>": {words, window}, "_default": {words, window} }

def get_guild_cfg(guild_id: int):
    g = str(guild_id)
    if g not in config:
        config[g] = dict(config["_default"])
        save_config(config)
    return config[g]

# ---------------- Dynamic ban list via ENV ----------------
# Add comma-separated insults in Railway → Variables: BAN_WORDS="idiot, clown, ..."
BAN_WORDS = [normalize(w) for w in os.getenv("BAN_WORDS", "").split(",") if w.strip()]
BAN_WORDS = [w for w in BAN_WORDS if w]

# Always-ban baseline (now includes 'bitch' too)
ALWAYS_BAN = {"hoe", "hoes", "cunt", "bitch", "bitches"}

# ---------------- Moderation rules (patterns) ----------------
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
    # Covers "<name> a|is a cheater/cheating"
    if re.search(r"\b\w{2,}\s+(?:is|s|is a|s a|a)\s+cheat\w*\b", norm_text):
        return True
    # General accusations / imperatives
    if any_word(norm_text, list(CHEAT_STEMS)):
        if is_directed(norm_text, has_mention):
            return True
        if re.search(r"\bstop\s+cheat\w*\b", norm_text):
            return True
        if re.search(r"\b(is|are)\s+cheat\w*\b", norm_text):  # is/are cheating/cheater
            return True
    return False

def is_bitch_insult(norm_text: str, has_mention: bool) -> bool:
    # We still detect directed usage and "<name> a bitch", but single-word is handled by ALWAYS_BAN
    if re.search(r"\bbi?atch(es)?\b", norm_text) or has_word(norm_text, "bitch") or has_word(norm_text, "bitches"):
        if is_directed(norm_text, has_mention):
            return True
        if re.search(r"\b\w{2,}\s+(?:is|s|is a|s a|a)\s+bitch(?:es)?\b", norm_text):
            return True
    return False

def is_fuck_you_super_strict(norm_text: str) -> bool:
    # 'fuck/fck/fuc you'
    if re.search(r"\bfu?c?k+\s*you\b", norm_text):
        return True
    # 'f you' / 'f-you'
    if re.search(r"\bf\s*you\b", norm_text):
        return True
    # 'f u' (even without 'you')
    if re.search(r"\bf\s*u\b", norm_text):
        return True
    # 'fuh u' / 'fu u' styles
    if re.search(r"\bfu?h+\s*u\b", norm_text):
        return True
    # 'fu' + 'you' separated
    if re.search(r"\bfu\b", norm_text) and has_word(norm_text, "you"):
        return True
    return False

def contains_banned_word(norm_text: str) -> bool:
    for w in ALWAYS_BAN:
        if re.search(rf"\b{re.escape(w)}\b", norm_text):
            return True
    for w in BAN_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", norm_text):
            return True
    return False

# ---- Player implication / “talks to a lot of girls” ----
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
        if re.search(r"\b\w{2,}\s+(?:is|s|is a|s a|a)\s+player\b", norm_text):  # e.g., "bros a player"
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

# ---------------- Cache for back-to-back messages ----------------
last_msg_cache = {}  # key: (guild_id, channel_id, user_id) -> { "norm": str, "time": float, "id": int, "had_mention": bool }

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
        f"**Defaults (from ENV):** [{default_words}] | window = {config['_default']['window']}s"
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

# ---------------- Moderation core ----------------
def contains_all_words(text: str, words: List[str]) -> bool:
    text = text.lower()
    return all(w in text for w in words)

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.author.bot or not message.guild:
        return

    gcfg = get_guild_cfg(message.guild.id)
    combo_words = gcfg["words"]
    window = gcfg["window"]

    content_norm = normalize(message.content)
    had_mention = bool(message.mentions)

    # Single-message checks
    if is_fuck_you_super_strict(content_norm):
        try: await message.delete()
        except discord.Forbidden: pass
        return

    if is_bitch_insult(content_norm, had_mention):
        try: await message.delete()
        except discord.Forbidden: pass
        return

    if is_cheater_accusation(content_norm, had_mention):
        try: await message.delete()
        except discord.Forbidden: pass
        return

    if is_player_implication(content_norm, had_mention):
        try: await message.delete()
        except discord.Forbidden: pass
        return

    if contains_banned_word(content_norm):
        try: await message.delete()
        except discord.Forbidden: pass
        return

    # Back-to-back logic (same user)
    now = time.time()
    key = (message.guild.id, message.channel.id, message.author.id)
    prev = last_msg_cache.get(key)

    last_msg_cache[key] = {"norm": content_norm, "time": now, "id": message.id, "had_mention": had_mention}

    if prev and (now - prev["time"]) <= window:
        combo_norm = f"{prev['norm']} || {content_norm}"
        if contains_all_words(combo_norm, combo_words):
            try: await message.delete()
            except discord.Forbidden: pass
            return
        if is_cheater_accusation(combo_norm, had_mention or prev["had_mention"]):
            try: await message.delete()
            except discord.Forbidden: pass
            return
        if is_player_implication(combo_norm, had_mention or prev["had_mention"]):
            try: await message.delete()
            except discord.Forbidden: pass
            return

# ---------------- Run the bot ----------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    print("ERROR: Missing DISCORD_BOT_TOKEN")
    raise SystemExit(1)
bot.run(TOKEN)






