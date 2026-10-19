"""
bump_cog.py - DISBOARD bump tracker with a real reminder ping.

What it does
  * Detects a successful DISBOARD /bump and thanks the bumper (bump #N).
  * Pings when the 2-hour cooldown is over: BUMP_ROLE_ID if set, otherwise the last bumper.
  * Reads DISBOARD's "please wait X minutes" reply and re-syncs the timer from it.
  * Survives Railway redeploys: state is kept in a private channel (AUTO_STORE_CHANNEL_ID),
    with a local-file fallback.
  * Deletes the old reminder when the next bump happens, so the channel stays clean.

Env vars
  BUMP_CHANNEL_ID        channel where people use /bump (optional but recommended)
  BUMP_ROLE_ID           role to ping when the server can be bumped again (optional)
  AUTO_STORE_CHANNEL_ID  private channel used to persist state (optional)
  BUMP_THANKS            "1" (default) = this cog sends the thank-you message.
                         Set "0" if another cog in your bot already sends it.

Load it in main.py:  await bot.load_extension("bump_cog")
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

import discord
from discord.ext import commands, tasks

log = logging.getLogger("bump_cog")

DISBOARD_ID = 302050872383242240
COOLDOWN = 2 * 60 * 60
STATE_TAG = "BUMP_STATE "
LOCAL_FILE = "bump_state.json"


def _env_int(name: str) -> int:
    try:
        return int(os.getenv(name, "0") or 0)
    except ValueError:
        return 0


class BumpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_id = _env_int("BUMP_CHANNEL_ID")
        self.role_id = _env_int("BUMP_ROLE_ID")
        self.store_id = _env_int("AUTO_STORE_CHANNEL_ID")
        self.thanks = os.getenv("BUMP_THANKS", "1") != "0"
        self.state = {
            "count": 0,
            "next_ts": 0.0,
            "user_id": 0,
            "channel_id": 0,
            "reminded": True,
            "reminder_msg_id": 0,
        }
        self._store_msg: Optional[discord.Message] = None

    # ------------------------------------------------------------------ lifecycle
    async def cog_load(self):
        self.reminder_loop.start()

    async def cog_unload(self):
        self.reminder_loop.cancel()

    # ------------------------------------------------------------------ persistence
    async def _load(self):
        data = None
        store = self.bot.get_channel(self.store_id) if self.store_id else None
        if store:
            try:
                async for m in store.history(limit=100):
                    if m.author.id == self.bot.user.id and m.content.startswith(STATE_TAG):
                        self._store_msg = m
                        data = json.loads(m.content[len(STATE_TAG):])
                        break
            except Exception:
                log.exception("could not read bump state from the store channel")
        if data is None and os.path.exists(LOCAL_FILE):
            try:
                with open(LOCAL_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                log.exception("could not read local bump state")
        if isinstance(data, dict):
            self.state.update({k: v for k, v in data.items() if k in self.state})

    async def _save(self):
        payload = json.dumps(self.state, separators=(",", ":"))
        try:
            with open(LOCAL_FILE, "w", encoding="utf-8") as f:
                f.write(payload)
        except OSError:
            pass  # read-only or wiped filesystem is fine, the store channel is the real copy

        store = self.bot.get_channel(self.store_id) if self.store_id else None
        if not store:
            return
        content = STATE_TAG + payload
        try:
            if self._store_msg:
                try:
                    await self._store_msg.edit(content=content)
                    return
                except discord.NotFound:
                    self._store_msg = None
            self._store_msg = await store.send(content)
        except discord.HTTPException:
            log.exception("could not save bump state to the store channel")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _bumper(message: discord.Message) -> Optional[discord.abc.User]:
        meta = getattr(message, "interaction_metadata", None)
        if meta is not None and getattr(meta, "user", None):
            return meta.user
        inter = getattr(message, "interaction", None)
        if inter is not None:
            return getattr(inter, "user", None)
        return None

    async def _delete_old_reminder(self, channel: discord.abc.Messageable):
        mid = self.state.get("reminder_msg_id")
        if not mid:
            return
        self.state["reminder_msg_id"] = 0
        try:
            await channel.get_partial_message(mid).delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            pass

    # ------------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != DISBOARD_ID or message.guild is None:
            return
        if self.channel_id and message.channel.id != self.channel_id:
            return

        text = " ".join((e.description or "") for e in message.embeds).lower()
        if not text:
            text = (message.content or "").lower()

        if "bump done" in text:
            await self._on_bump(message)
        elif "wait another" in text:
            await self._on_cooldown(message, text)

    async def _on_bump(self, message: discord.Message):
        now = time.time()
        last_bump_ts = self.state["next_ts"] - COOLDOWN
        if now - last_bump_ts < 30:  # duplicate event guard
            return

        user = self._bumper(message)
        await self._delete_old_reminder(message.channel)
        self.state.update(
            count=self.state["count"] + 1,
            next_ts=now + COOLDOWN,
            user_id=user.id if user else 0,
            channel_id=message.channel.id,
            reminded=False,
        )
        await self._save()

        if self.thanks:
            who = user.mention if user else "Someone"
            ts = int(self.state["next_ts"])
            try:
                await message.channel.send(
                    f"{who} just bumped the server! Thank you \U0001F49C (bump #{self.state['count']})\n"
                    f"\u23F0 Next bump <t:{ts}:R>",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.exception("could not send the bump thank-you")

    async def _on_cooldown(self, message: discord.Message, text: str):
        secs = 0
        units = {"hour": 3600, "minute": 60, "second": 1}
        for n, unit in re.findall(r"(\d+)\s*(hour|minute|second)", text):
            secs += int(n) * units[unit]
        if not secs:
            return
        self.state.update(
            next_ts=time.time() + secs,
            channel_id=message.channel.id,
            reminded=False,
        )
        await self._save()

    # ------------------------------------------------------------------ reminder
    @tasks.loop(seconds=15)
    async def reminder_loop(self):
        try:
            st = self.state
            if st["reminded"] or not st["next_ts"] or time.time() < st["next_ts"]:
                return

            channel = self.bot.get_channel(st["channel_id"] or self.channel_id)
            if channel is None:
                return  # try again next tick

            mention = ""
            allowed = discord.AllowedMentions.none()
            guild = getattr(channel, "guild", None)
            if self.role_id and guild and guild.get_role(self.role_id):
                mention = f"<@&{self.role_id}> "
                allowed = discord.AllowedMentions(roles=True)
            elif st["user_id"]:
                mention = f"<@{st['user_id']}> "
                allowed = discord.AllowedMentions(users=True)

            msg = await channel.send(
                f"{mention}\u23F0 **Time to bump!** The server can be bumped again. Use `/bump`",
                allowed_mentions=allowed,
            )
            st["reminded"] = True
            st["reminder_msg_id"] = msg.id
            await self._save()
        except Exception:
            log.exception("bump reminder loop error")

    @reminder_loop.before_loop
    async def _before_reminder_loop(self):
        await self.bot.wait_until_ready()
        await self._load()

    # ------------------------------------------------------------------ command
    @commands.command(name="bumpstatus")
    async def bumpstatus(self, ctx: commands.Context):
        ts = int(self.state["next_ts"])
        if not ts:
            await ctx.send("No bump recorded yet.")
        elif ts <= time.time():
            await ctx.send("\u2705 The server can be bumped now. Use `/bump`.")
        else:
            await ctx.send(f"\u23F0 Next bump <t:{ts}:R> (total bumps: {self.state['count']})")


async def setup(bot: commands.Bot):
    await bot.add_cog(BumpCog(bot))
