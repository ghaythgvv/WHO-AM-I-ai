import os
import pathlib
import subprocess
import sys

import discord
from discord.ext import commands

# ---- one-time upgrade: patches ai_admin_cog.py on every start (safe to repeat: already-patched files are skipped).
# Needs patch_ai_admin.py and patch_ai_admin_2.py in the repo. Once you upload an already patched ai_admin_cog.py
# you can delete this block and the two patch files.
HERE = pathlib.Path(__file__).resolve().parent
for _script in ("patch_ai_admin.py", "patch_ai_admin_2.py"):
    _p = HERE / _script
    if _p.exists():
        _r = subprocess.run([sys.executable, str(_p), str(HERE / "ai_admin_cog.py")], capture_output=True, text=True)
        print(f"[patch] {_script}: {(_r.stdout or _r.stderr).strip()[:400]}", flush=True)

intents = discord.Intents.default()
intents.members = True          # also ON in the Developer Portal
intents.message_content = True  # also ON in the Developer Portal

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def setup_hook():
    await bot.load_extension("ai_admin_cog")
    await bot.load_extension("auto_cog")   # bump alert v2, loops, rules, welcome
    await bot.load_extension("voice_cog")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


bot.run(os.environ["DISCORD_TOKEN"])
