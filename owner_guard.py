"""
owner_guard.py  -  protects the server OWNER: whoever moves / disconnects / server-mutes / server-deafens (or times out)
the owner gets timed out automatically.                                                        [v1]

HOW IT WORKS
  - server mute / deafen / timeout of the owner : read from the audit log the moment it happens (exact: it names the victim)
  - moved / disconnected from voice by someone  : the owner's voice state changes, then the audit log is checked for a
                                                  fresh "member moved" / "member disconnected" entry (see the notes below)
  The person who did it is timed out for OWNER_GUARD_MINUTES and a card is posted in the log channel (or sent to the
  owner by DM). Nothing happens when the owner moves himself, or when THIS bot does it (for example the AI admin).

NOTES (Discord limits - not bugs)
  * Discord does not let anybody time out the server owner at all, so that attempt just fails on its own.
  * Discord cannot time out members with the Administrator permission, or members whose top role is not BELOW the bot's
    role. For them the bot cannot punish - it posts the alert and says why. Keep the bot role high.
  * "Moved" / "disconnected" audit entries do not name the victim and Discord merges repeated ones, so for those two the
    bot matches the entry by time (a few seconds) and, for moves, by the destination channel. It is very reliable, but
    use OWNER_GUARD_DRY_RUN=1 for your first test: it only reports, it does not time anybody out.
  * Other bots that move you are only reported (the bot cannot know who typed the command).

SETUP
  Bot permissions: View Audit Log, Moderate Members.   Intents: members + voice_states (default) - nothing else.
  Load it:  await bot.load_extension("owner_guard")

OPTIONAL RAILWAY VARIABLES
  OWNER_GUARD              1 (default) = on, 0 = off
  OWNER_GUARD_MINUTES      length of the timeout, default 30 (max 40320 = 28 days)
  OWNER_GUARD_LOG_ID       channel id for the alert cards (empty = the owner gets a DM)
  OWNER_GUARD_EXEMPT_IDS   user id(s) that may touch the owner without punishment, e.g. 111,222
  OWNER_GUARD_PROTECT_IDS  extra user id(s) protected like the owner
  OWNER_GUARD_DRY_RUN      1 = only report, never time out

COMMAND (owner only):  !guard status | on | off | minutes 60 | dry on | dry off     (changes last until the bot restarts;
                       set the Railway variables to make them permanent)
"""

import asyncio
import datetime as dt
import logging
import os
from collections import deque

import discord
from discord.ext import commands

log = logging.getLogger("owner_guard")
RED, ORANGE = 0xE74C3C, 0xE67E22
A = discord.AuditLogAction


def _ids(name):
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x.isdigit()}


def _num(name, default):
    v = os.getenv(name, "").strip()
    return int(v) if v.isdigit() else default


def _flag(name, default):
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off")


EXEMPT = _ids("OWNER_GUARD_EXEMPT_IDS")
PROTECT = _ids("OWNER_GUARD_PROTECT_IDS")
LOG_ID = _num("OWNER_GUARD_LOG_ID", 0)


class OwnerGuard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.on = _flag("OWNER_GUARD", True)
        self.dry = _flag("OWNER_GUARD_DRY_RUN", False)
        self.minutes = max(1, min(_num("OWNER_GUARD_MINUTES", 30), 40320))
        self.seen = deque(maxlen=200)  # audit entries already handled
        self._bg = set()

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def cog_unload(self):
        for t in list(self._bg):
            t.cancel()

    def _protected(self, guild):
        ids = set(PROTECT)
        if guild.owner_id:
            ids.add(guild.owner_id)
        return ids

    # ------------------------------------------------------------------ exact: mute / deafen / timeout of the owner
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        if not self.on or entry.action != A.member_update:
            return
        g = entry.guild
        tid = getattr(entry.target, "id", None)
        if tid is None or tid not in self._protected(g) or entry.id in self.seen:
            return
        what = []
        if getattr(entry.after, "mute", None) is True:
            what.append("server-muted")
        if getattr(entry.after, "deaf", None) is True:
            what.append("server-deafened")
        if getattr(entry.after, "timed_out_until", None):
            what.append("timed out")
        if not what:
            return
        self.seen.append(entry.id)
        victim = g.get_member(tid)
        await self._punish(g, victim, tid, entry.user_id, " + ".join(what))

    # ------------------------------------------------------------------ moved / disconnected
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if not self.on or member.bot or member.id not in self._protected(member.guild):
            return
        if before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
            self._spawn(self._check_voice(member, "moved", after.channel))
        elif before.channel is not None and after.channel is None:
            self._spawn(self._check_voice(member, "disconnected", None))

    async def _check_voice(self, member, kind, dest):
        await asyncio.sleep(1.5)  # the audit entry shows up a moment after the voice event
        g = member.guild
        if not g.me.guild_permissions.view_audit_log:
            log.warning("owner_guard: I need the View Audit Log permission")
            return
        action = A.member_move if kind == "moved" else A.member_disconnect
        now = dt.datetime.now(dt.timezone.utc)
        try:
            async for e in g.audit_logs(limit=15, action=action):  # newest first
                if (now - e.created_at).total_seconds() > 7:
                    break
                if e.id in self.seen:
                    continue
                if kind == "moved":
                    ch = getattr(e.extra, "channel", None)
                    if ch is None or dest is None or ch.id != dest.id:
                        continue
                self.seen.append(e.id)
                await self._punish(g, member, member.id, e.user_id, kind + " from voice")
                return
        except discord.HTTPException as ex:
            log.warning("owner_guard: couldn't read the audit log: %s", ex)

    # ------------------------------------------------------------------ the punishment
    async def _punish(self, guild, victim, victim_id, culprit_id, what):
        if culprit_id is None or culprit_id == victim_id or culprit_id == self.bot.user.id:
            return  # unknown / himself / this bot (for example the AI admin acting on the owner's order)
        if culprit_id in EXEMPT or culprit_id == guild.owner_id:
            return
        culprit = guild.get_member(culprit_id)
        if culprit is None:
            try:
                culprit = await guild.fetch_member(culprit_id)
            except discord.HTTPException:
                culprit = None
        vname = victim.mention if victim else f"<@{victim_id}>"
        emb = discord.Embed(title="Owner protection", color=RED, timestamp=dt.datetime.now(dt.timezone.utc))
        emb.add_field(name="Protected member", value=vname, inline=True)
        emb.add_field(name="Action on them", value=what, inline=True)
        if culprit is None:
            emb.color = ORANGE
            emb.add_field(name="Done by", value=f"<@{culprit_id}> (not in the server any more)", inline=False)
            return await self._report(guild, emb)
        emb.add_field(name="Done by", value=f"{culprit.mention} (`{culprit.id}`)", inline=False)
        if culprit.bot:
            emb.color = ORANGE
            emb.add_field(name="Result", value="That is a bot - I can't know who typed its command, so I only report it.", inline=False)
            return await self._report(guild, emb)

        reason = None
        if culprit.guild_permissions.administrator:
            reason = "they have the Administrator permission (Discord can't time out administrators)"
        elif guild.me.top_role <= culprit.top_role:
            reason = "their top role is not below mine - move my role higher"
        elif not guild.me.guild_permissions.moderate_members:
            reason = "I don't have the Moderate Members permission"
        if reason:
            emb.color = ORANGE
            emb.add_field(name="Result", value=f"NOT punished: {reason}.", inline=False)
        elif self.dry:
            emb.color = ORANGE
            emb.add_field(name="Result", value=f"Dry run - would have timed them out for {self.minutes} min.", inline=False)
        else:
            try:
                await culprit.timeout(dt.timedelta(minutes=self.minutes), reason=f"Owner protection: {what}")
                emb.add_field(name="Result", value=f"Timed out for **{self.minutes} min**.", inline=False)
            except discord.HTTPException as e:
                emb.color = ORANGE
                emb.add_field(name="Result", value=f"Timeout failed: {getattr(e, 'text', None) or e}", inline=False)
        await self._report(guild, emb)

    async def _report(self, guild, emb):
        ch = guild.get_channel(LOG_ID) if LOG_ID else None
        try:
            if ch is not None:
                await ch.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
                return
        except discord.HTTPException:
            pass
        try:  # no log channel (or no access): tell the owner
            owner = guild.owner or await self.bot.fetch_user(guild.owner_id)
            await owner.send(embed=emb)
        except (discord.HTTPException, AttributeError):
            log.warning("owner_guard: couldn't report (set OWNER_GUARD_LOG_ID)")

    # ------------------------------------------------------------------ command
    @commands.command(name="guard")
    @commands.guild_only()
    async def guard(self, ctx: commands.Context, action: str = "status", value: str = ""):
        """owner only: !guard status | on | off | minutes 60 | dry on | dry off"""
        if ctx.author.id != ctx.guild.owner_id:
            return
        a, v = action.lower(), value.lower()
        if a == "on":
            self.on = True
        elif a == "off":
            self.on = False
        elif a == "minutes" and v.isdigit():
            self.minutes = max(1, min(int(v), 40320))
        elif a == "dry" and v in ("on", "off"):
            self.dry = v == "on"
        elif a != "status":
            return await ctx.reply("Use `!guard status`, `on`, `off`, `minutes 60` or `dry on/off`.", mention_author=False)
        ch = ctx.guild.get_channel(LOG_ID) if LOG_ID else None
        await ctx.reply(
            f"Owner protection is **{'ON' if self.on else 'OFF'}**{' (dry run: report only)' if self.dry else ''} - "
            f"timeout **{self.minutes} min** - alerts go to {ch.mention if ch else 'your DMs (set OWNER_GUARD_LOG_ID)'}.",
            mention_author=False,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(OwnerGuard(bot))
