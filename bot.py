import os
import json
import time
import asyncio
import re
import discord
from discord.ext import commands

CONFIG_FILE = "config.json"

# ---------- Basic Config Persistence ----------
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

config = load_config()  # {guild_id: {"words": ["chunky","cheater"], "window": 30}}

def get_guild_cfg(guild_id: int):
    g = str(guild_id)
    if g not in config:
        config[g] = {"words": ["chunky", "cheater"], "window": 30}
        save_config(config)
    return config[g]

# ---------- Cache for back-to-back messages ----------
# key: (guild_id, channel_id, user_id) -> value: {"content": str, "time": float, "message_id": int}
last_msg_cache = {}

# ---------- Bot Setup ----------
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Utility: only allow server managers to run config commands
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
    await ctx.reply(
        "**Commands (server managers only for config):**\n"
        "`!setwords word1, word2, ...` – set required word combo (at least 2 words)\n"
        "`!setwindow N` – set back-to-back time window in seconds (default 30)\n"
        "`!config` – show current settings\n\n"
        f"**Current:** words = [{words}] | window = {gcfg['window']}s"
    )

@bot.command(name="config")
async def config_cmd(ctx):
    gcfg = get_guild_cfg(ctx.guild.id)
    await ctx.reply(f"Words: {gcfg['words']}\nWindow: {gcfg['window']} seconds")

@bot.command(name="setwords")
@is_guild_manager()
async def setwords_cmd(ctx, *, args: str):
    # Split by comma, strip whitespace, lower
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

# ---------- Moderation Logic ----------
def contains_all_words(text: str, words: list[str]) -> bool:
    text = text.lower()
    return all(w in text for w in words)

def wordset_regex(words: list[str]) -> re.Pattern:
    # For same-message combos where order doesn't matter; require all words to appear in any order.
    # We'll just use contains_all_words for simplicity; regex could be added for word boundaries if desired.
    return None  # not used in this minimal version

@bot.event
async def on_message(message: discord.Message):
    # Let commands run
    await bot.process_commands(message)

    if message.author.bot or not message.guild:
        return

    gcfg = get_guild_cfg(message.guild.id)
    words = gcfg["words"]
    window = gcfg["window"]

    # Case 1: same-message contains all words
    content = message.content.lower()
    if contains_all_words(content, words):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        return

    # Case 2: two back-to-back messages by the same user (within window)
    now = time.time()
    key = (message.guild.id, message.channel.id, message.author.id)
    prev = last_msg_cache.get(key)

    # Update cache BEFORE return (so if someone sends only one of the words we can see it on next msg)
    last_msg_cache[key] = {"content": content, "time": now, "message_id": message.id}

    if prev:
        # Only consider if within window
        if (now - prev["time"]) <= window:
            prev_text = prev["content"]
            # If split across the two messages, e.g. (prev has word A, current has word B) in any order
            # We require that the union of both messages contains all words.
            combo_text = f"{prev_text} || {content}"
            if contains_all_words(combo_text, words):
                try:
                    # Delete the latest message (and optionally the previous one too)
                    await message.delete()
                    # Optional: also remove the previous message to keep things clean
                    # prev_msg = await message.channel.fetch_message(prev["message_id"])
                    # await prev_msg.delete()
                except discord.Forbidden:
                    pass

    # Optional cleanup: purge stale cache entries occasionally (lightweight)
    # Not strictly necessary for small servers.


import os
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    print("ERROR: Missing DISCORD_BOT_TOKEN")
    raise SystemExit(1)
bot.run(TOKEN)



