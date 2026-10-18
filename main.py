import os

import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.members = True          # also ON in the Developer Portal
intents.message_content = True  # also ON in the Developer Portal

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def setup_hook():
    await bot.load_extension("ai_admin_cog")
    await bot.load_extension("voice_cog")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


bot.run(os.environ["DISCORD_TOKEN"])
