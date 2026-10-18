"""
auto_cog.py  -  automations for the ELITE SYSTEM bot   [v1]

You talk to the AI admin (ai_admin_cog) in plain words and it uses the tools in this file:

  BUMP ALERT  (rewritten: the roles are pinged ONCE per bump cycle, never twice)
      !ask tag Staff and UT Staff in #bump every time someone bumps, remind them after 120 minutes
      !ask bump alert status        /  !ask bump alert off        /  !bumps  (top bumpers, everybody can use it)
  LOOPS
      !ask send a random message in #chat every 30 seconds and delete it immediately
  RULES
      !ask when someone says "free nitro" in #chat delete it and reply "no scams here"
      !ask react with a fire emoji to every message in #showcase
      !ask welcome new members in #welcome with "Welcome {user} to {server}!" and give them the Member role
  MANAGE
      !ask list automations   /   !ask remove r3   /   !ask pause r2

SAVING
  Everything is saved to AUTO_DATA_FILE and (recommended on Railway) mirrored into a private channel, so a redeploy
  never wipes it. Saved settings WIN over the BUMP_* Railway variables - change them by talking to the bot.

RAILWAY VARIABLES (all optional)
    AUTO_STORE_CHANNEL_ID   id of a PRIVATE text channel (only the bot + you see it). The bot keeps one message with its
                            settings there. Without it the settings live in a file that Railway deletes on redeploy.
    AUTO_DATA_FILE          default auto_data.json
    AUTO_MIN_LOOP_SECONDS   shortest allowed loop interval (default 5, Discord allows ~5 messages / 5 s per channel)
    AUTO_MAX_RULES          max automations (default 25)
    BUMP_*                  first-start defaults only: BUMP_PING_ROLE_IDS, BUMP_CHANNEL_IDS, BUMP_REMIND_MINUTES,
                            BUMP_PING_NOW, BUMP_BOT_IDS, BUMP_PHRASES (same meaning as before)

LOAD IT (after or before ai_admin_cog, order does not matter):   await bot.load_extension("auto_cog")
"""

import asyncio
import io
import json
import logging
import os
import random
import re
import time
from collections import deque

import discord
from discord.ext import commands

log = logging.getLogger("auto_cog")
PURPLE = 0x7B4DFF
NO_MENTIONS = discord.AllowedMentions.none()


def _ids(name):
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x.isdigit()}


def _num(name, default):
    v = os.getenv(name, "").strip()
    return int(v) if v.isdigit() else default


BUMP_BOT_IDS = _ids("BUMP_BOT_IDS") or {302050872383242240}  # 302050872383242240 = DISBOARD
BUMP_PHRASES = [p.strip().lower() for p in os.getenv("BUMP_PHRASES", "bump done").split(",") if p.strip()] or ["bump done"]
DATA_FILE = os.getenv("AUTO_DATA_FILE", "auto_data.json")
STORE_CHANNEL_ID = _num("AUTO_STORE_CHANNEL_ID", 0)
MIN_LOOP_SECONDS = max(2, _num("AUTO_MIN_LOOP_SECONDS", 5))
MAX_RULES = _num("AUTO_MAX_RULES", 25)
REMIND_MARK = "time to bump again"  # lowercase marker found in the reminder message
STORE_NAME = "auto_data.json"

RANDOM_POOL = [
    "hey", "psst", "boop", "beep boop", "hello there", "ping!", "what's up", "hmm", "random message",
    "still here", "tick tock", "anyone around?", "just passing by", "yo", "echo", "hi",
]


# ----------------------------------------------------------------- tool schemas
def _s(desc=None, **kw):
    p = {"type": "string", **kw}
    if desc:
        p["description"] = desc
    return p


def _i(desc=None):
    p = {"type": "integer"}
    if desc:
        p["description"] = desc
    return p


def _b(desc=None):
    p = {"type": "boolean"}
    if desc:
        p["description"] = desc
    return p


def _arr(desc):
    return {"type": "array", "items": {"type": "string"}, "description": desc}


def _tool(name, desc, props=None, required=()):
    params = {"type": "object", "properties": props or {}}
    if required:
        params["required"] = list(required)
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}


AUTO_TOOLS = [
    _tool(
        "set_bump_alert",
        "Set up, change, show or turn off the AUTOMATIC bump alert for DISBOARD (/bump). When someone bumps in `channel` the "
        "bot thanks them (and counts bumps); the `roles` are tagged ONCE per bump cycle: when it is time to bump again "
        "(remind_minutes after the bump; DISBOARD's cooldown is 120), and - if nobody was reminded yet - right after the "
        "bump (ping_now). Never twice in a row. Use it for 'tag staff every time someone bumps'. Only the fields you pass "
        "are changed. enabled=false turns it off. With no arguments it just shows the current setting and the top bumpers.",
        {"channel": _s("name/id or 'here'; leave out to keep the current one"),
         "roles": _arr("role names or ids to tag"),
         "remind_minutes": _i("0 = no reminder, 120 = when DISBOARD allows the next bump"),
         "ping_now": _b("tag the roles right after a bump when no reminder was waiting (default true)"),
         "enabled": _b("false = turn the alert off")},
    ),
    _tool(
        "auto_add",
        "Create an AUTOMATION that keeps running (saved, survives restarts). kind: "
        "'loop' = every `every_seconds` the bot posts one of `texts` (random pick; if the staff member gave none, WRITE 8-10 "
        "short varied ones yourself) in `channel` and deletes it after `delete_after_seconds` (0 = immediately, leave out = "
        "keep it). "
        "'on_message' = whenever someone writes in `channel` (leave out = whole server, but action delete needs a channel) a "
        "message containing any of `contains` (leave out = every message) the bot does `action`: reply (one of texts), "
        "send (post one of texts in that channel), react (with `emoji`), delete (that message; after delete_after_seconds, "
        "0 = at once). For reply/send, delete_after_seconds deletes the bot's own message. "
        "'on_join' = when a member joins: post `texts` in `channel` (or DM them when dm=true) and/or give them `role`. "
        "In texts you can use {user} {name} {server} {count}. The bot never pings @everyone/@here/roles from these. "
        "Needs a button confirm for loops and delete rules.",
        {"kind": _s(enum=["loop", "on_message", "on_join"]),
         "channel": _s("name/id or 'here'"),
         "texts": _arr("messages to post (one is picked at random each time)"),
         "every_seconds": _i("loop interval in seconds"),
         "delete_after_seconds": _i("0 = delete at once; leave out = don't delete"),
         "contains": _arr("on_message: trigger words (any of them); leave out = every message"),
         "action": _s(enum=["reply", "send", "react", "delete"]),
         "emoji": _s("on_message react: one or more emojis separated by spaces"),
         "role": _s("on_join: role to give the new member"),
         "dm": _b("on_join: DM the text instead of posting it in a channel"),
         "include_bots": _b("on_message: also react to messages of other bots (default false)"),
         "name": _s("short label")},
        ["kind"],
    ),
    _tool("auto_list", "List all automations (loops, message rules, welcome rules) with ids and state."),
    _tool("auto_remove", "Delete one automation by id (like r3) or 'all' (button confirm).",
          {"rule_id": _s("rule id, its name, or 'all'")}, ["rule_id"]),
    _tool("auto_toggle", "Pause or resume one automation.",
          {"rule_id": _s("rule id or its name"), "enabled": _b("true = running, false = paused")}, ["rule_id", "enabled"]),
]
AUTO_TOOL_NAMES = {t["function"]["name"] for t in AUTO_TOOLS}
AUTO_READ = {"auto_list"}
AUTO_CONFIRM = {"auto_add", "auto_remove"}


# ----------------------------------------------------------------- helpers
def _embed_text(e: discord.Embed) -> str:
    parts = [e.title or "", e.description or ""]
    parts += [f"{f.name} {f.value}" for f in e.fields]
    if e.author and e.author.name:
        parts.append(e.author.name)
    if e.footer and e.footer.text:
        parts.append(e.footer.text)
    return " ".join(p for p in parts if p)


def _blob(m: discord.Message) -> str:
    return (m.content + " " + " ".join(_embed_text(e) for e in m.embeds)).strip()


def _dur(s):
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60} min" + (f" {s % 60}s" if s % 60 else "")
    if s < 86400:
        return f"{s // 3600} h" + (f" {s % 3600 // 60} min" if s % 3600 // 60 else "")
    return f"{s // 86400} d"


def _defaults():
    return {
        "seq": 0,
        "rules": [],
        "bump": {
            "on": True,
            "channels": sorted(_ids("BUMP_CHANNEL_IDS")),
            "roles": list(dict.fromkeys(
                int(x) for x in os.getenv("BUMP_PING_ROLE_IDS", "").replace(" ", "").split(",") if x.isdigit()
            )),
            "now": os.getenv("BUMP_PING_NOW", "1").strip().lower() not in ("0", "false", "no", "off"),
            "remind": _num("BUMP_REMIND_MINUTES", 0),
            "due": {},     # channel id -> unix time of the next "time to bump" reminder
            "msg": {},     # channel id -> id of the reminder message that is waiting
            "counts": {},  # user id -> number of bumps
        },
    }


class Automation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = _defaults()
        self.loops = {}        # rule id -> running loop task
        self.bump_tasks = {}   # channel id -> pending reminder task
        self.bump_seen = deque(maxlen=200)
        self.bump_lock = asyncio.Lock()
        self._bg = set()
        self._save_task = None
        self._store_msg = None
        self._boot_task = None

    # ------------------------------------------------------------ lifecycle / saving
    async def cog_load(self):
        await self._load()
        self._boot_task = asyncio.create_task(self._boot())

    async def cog_unload(self):
        for t in [*self.loops.values(), *self.bump_tasks.values(), self._boot_task, self._save_task]:
            if t is not None and not t.done():
                t.cancel()
        self._write_file()  # last state to disk

    async def _boot(self):
        await self.bot.wait_until_ready()
        for r in self.data["rules"]:
            if r["kind"] == "loop" and r.get("on"):
                self._start_loop(r["id"])
        for cid, due in list(self.data["bump"]["due"].items()):
            self._schedule_reminder(int(cid), int(due))
        log.info("automations ready: %d rule(s), %d reminder(s)", len(self.data["rules"]), len(self.data["bump"]["due"]))

    async def _store_channel(self):
        if not STORE_CHANNEL_ID:
            return None
        try:
            return self.bot.get_channel(STORE_CHANNEL_ID) or await self.bot.fetch_channel(STORE_CHANNEL_ID)
        except discord.HTTPException as e:
            log.warning("AUTO_STORE_CHANNEL_ID %s not reachable: %s", STORE_CHANNEL_ID, e)
            return None

    async def _load(self):
        raw = None
        ch = await self._store_channel()
        if ch is not None:
            try:
                async for m in ch.history(limit=50):
                    if m.author.id == self.bot.user.id:
                        att = next((a for a in m.attachments if a.filename == STORE_NAME), None)
                        if att:
                            self._store_msg = m
                            raw = json.loads((await att.read()).decode("utf-8"))
                            break
            except (discord.HTTPException, ValueError) as e:
                log.warning("couldn't read the store channel: %s", e)
        if raw is None and os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, ValueError) as e:
                log.warning("couldn't read %s: %s", DATA_FILE, e)
        if isinstance(raw, dict):
            d = _defaults()
            d.update({k: v for k, v in raw.items() if k in ("seq", "rules")})
            if isinstance(raw.get("bump"), dict):
                d["bump"].update(raw["bump"])
            self.data = d

    def _dirty(self):
        """save soon (waits 2 s so a burst of changes is one write)"""
        if self._save_task is None or self._save_task.done():
            self._save_task = asyncio.create_task(self._save_soon())

    def _write_file(self):
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
        except OSError as e:
            log.warning("couldn't write %s: %s", DATA_FILE, e)

    async def _save_soon(self):
        try:
            await asyncio.sleep(2)
            self._write_file()
            ch = await self._store_channel()
            if ch is None:
                return
            raw = json.dumps(self.data, ensure_ascii=False, indent=1).encode("utf-8")
            note = "ELITE SYSTEM automation settings - don't delete this message"
            if self._store_msg is not None:
                try:
                    await self._store_msg.edit(content=note, attachments=[discord.File(io.BytesIO(raw), STORE_NAME)])
                    return
                except discord.NotFound:
                    self._store_msg = None
            self._store_msg = await ch.send(note, file=discord.File(io.BytesIO(raw), STORE_NAME), allowed_mentions=NO_MENTIONS)
        except asyncio.CancelledError:
            raise
        except discord.HTTPException as e:
            log.warning("couldn't save to the store channel: %s", e)
        except Exception:
            log.exception("saving automations failed")

    def _persist_note(self):
        if STORE_CHANNEL_ID:
            return ""
        return ("\nSaved to a file - Railway wipes files on redeploy. Set `AUTO_STORE_CHANNEL_ID` (a private channel) "
                "and it is kept forever.")

    def _ai(self):
        return self.bot.get_cog("AIAdmin")

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    async def _del(self, msg, delay=0):
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    def _later_delete(self, msg, after):
        if after is not None:
            self._spawn(self._del(msg, max(0, int(after))))

    @staticmethod
    def _fmt(text, guild, member=None):
        return (
            text.replace("{server}", guild.name)
            .replace("{count}", str(guild.member_count or ""))
            .replace("{user}", member.mention if member else "")
            .replace("{name}", member.display_name if member else "")
        )

    def channel_replaced(self, old_id, new_id):
        """called by ai_admin_cog when a wiped channel got a new id. Returns notes for the staff member."""
        cfg, notes = self.data["bump"], []
        if old_id in cfg["channels"]:
            cfg["channels"] = [new_id if c == old_id else c for c in cfg["channels"]]
            notes.append("bump alert channel updated")
        for key in ("due", "msg"):
            if str(old_id) in cfg[key]:
                cfg[key].pop(str(old_id))
        task = self.bump_tasks.pop(old_id, None)
        if task and not task.done():
            task.cancel()
        for r in self.data["rules"]:
            if r.get("channel") == old_id:
                r["channel"] = new_id
                notes.append(f"automation {r['id']} follows the new channel")
        self._dirty()
        return notes

    # ------------------------------------------------------------ BUMP ALERT
    @commands.Cog.listener("on_message")
    async def bump_watch(self, message: discord.Message):
        await self._bump_check(message)

    @commands.Cog.listener("on_message_edit")
    async def bump_watch_edit(self, before: discord.Message, after: discord.Message):
        await self._bump_check(after)  # bots that "think" first post their real text as an edit

    @staticmethod
    def _bumper(m):
        """who ran /bump (None if Discord doesn't say)"""
        user = getattr(getattr(m, "interaction_metadata", None), "user", None)
        if user is None:
            try:
                user = m.interaction.user if m.interaction else None
            except Exception:
                user = None
        return user

    def _bump_roles(self, guild):
        return [r for r in (guild.get_role(i) for i in self.data["bump"]["roles"]) if r is not None]

    async def _bump_check(self, m):
        cfg = self.data["bump"]
        if not (cfg["on"] and cfg["roles"]) or m.guild is None or m.author.id not in BUMP_BOT_IDS:
            return
        if cfg["channels"] and m.channel.id not in cfg["channels"]:
            return
        if m.id in self.bump_seen:
            return
        text = _blob(m).lower()
        if not any(p in text for p in BUMP_PHRASES):
            if text:
                log.info("bump alert: a message from the bump bot didn't match BUMP_PHRASES: %r", text[:150])
            return
        self.bump_seen.append(m.id)
        try:
            async with self.bump_lock:  # one bump at a time: the reminder task can't slip in between
                await self._bump_handle(m)
        except Exception:
            log.exception("bump alert failed")

    async def _find_reminder(self, ch, max_age=None):
        """the 'time to bump again' message of the bot that is still waiting in this channel (or None)"""
        me = self.bot.user.id
        mid = self.data["bump"]["msg"].get(str(ch.id))
        if mid and max_age is None:
            try:
                x = await ch.fetch_message(int(mid))
                if x.author.id == me and REMIND_MARK in x.content.lower():
                    return x
            except discord.HTTPException:
                pass
        try:
            async for x in ch.history(limit=25):
                if x.author.id == me and REMIND_MARK in x.content.lower():
                    if max_age is None or (discord.utils.utcnow() - x.created_at).total_seconds() <= max_age:
                        return x
        except discord.HTTPException:
            pass
        return None

    async def _bump_answered(self, m):
        """True if another copy of the bot already thanked for THIS bump (two instances running = double messages)"""
        me = self.bot.user.id
        try:
            async for x in m.channel.history(limit=15):
                low = x.content.lower()
                if x.author.id != me or "bump" not in low or REMIND_MARK in low:
                    continue
                if (x.edited_at or x.created_at) >= m.created_at:
                    return True
        except discord.HTTPException:
            pass
        return False

    def _thanks(self, who, count, due):
        s = f"{who.mention if who else 'Someone'} just bumped the server! Thank you \U0001F49C"
        if count > 1:
            s += f" (bump #{count})"
        if due:
            s += f"\n\u23F0 Next bump <t:{due}:R>"
        return s

    async def _bump_handle(self, m):
        cfg, ch, g = self.data["bump"], m.channel, m.guild
        await asyncio.sleep(random.uniform(0.2, 1.2))  # if a 2nd copy of the bot runs, the slower one sees the answer and stops
        if await self._bump_answered(m):
            log.info("bump alert: already answered by another copy of the bot, skipping")
            return
        roles = self._bump_roles(g)
        if not roles:
            log.warning("bump alert: none of the configured roles exist in this server")
            return
        who = self._bumper(m)
        count = 0
        if who is not None:
            k = str(who.id)
            cfg["counts"][k] = cfg["counts"].get(k, 0) + 1
            count = cfg["counts"][k]
        cid = str(ch.id)
        due = int(time.time()) + cfg["remind"] * 60 if cfg["remind"] > 0 else 0
        thanks = self._thanks(who, count, due)

        waiting = await self._find_reminder(ch)  # the reminder already pinged the roles: turn it into the thank-you
        done = False
        if waiting is not None:
            try:
                await waiting.edit(content=thanks, allowed_mentions=NO_MENTIONS)  # an edit pings nobody
                done = True
            except discord.HTTPException:
                done = False
        if not done:
            ping = cfg["now"] and waiting is None
            try:
                await ch.send(
                    (" ".join(r.mention for r in roles) + " " if ping else "") + thanks,
                    allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=roles if ping else False),
                )
            except discord.HTTPException as e:
                log.warning("bump alert: couldn't post in #%s: %s", getattr(ch, "name", "?"), e)

        cfg["msg"].pop(cid, None)
        if due:
            cfg["due"][cid] = due
            self._schedule_reminder(ch.id, due)
        else:
            cfg["due"].pop(cid, None)
            self._cancel_reminder(ch.id)
        self._dirty()

    def _cancel_reminder(self, cid):
        t = self.bump_tasks.pop(cid, None)
        if t is not None and not t.done():
            t.cancel()

    def _schedule_reminder(self, cid, due):
        self._cancel_reminder(cid)
        self.bump_tasks[cid] = asyncio.create_task(self._bump_remind(cid, due))

    async def _bump_remind(self, cid, due):
        """wait for the cooldown, then tag the roles ONCE (a newer bump replaces this reminder)"""
        try:
            await asyncio.sleep(max(0, due - time.time()))
            cfg = self.data["bump"]
            if not cfg["on"] or cfg["due"].get(str(cid)) != due:
                return
            ch = self.bot.get_channel(cid)
            if ch is None:
                return
            await asyncio.sleep(random.uniform(0.2, 1.2))  # two copies of the bot: don't both post
            async with self.bump_lock:
                if cfg["due"].get(str(cid)) != due:
                    return
                cfg["due"].pop(str(cid), None)
                self._dirty()
                if await self._find_reminder(ch, max_age=90) is not None:
                    return  # another copy of the bot just posted it
                roles = self._bump_roles(ch.guild)
                if not roles:
                    return
                old = await self._find_reminder(ch)  # nobody bumped last time: keep only ONE reminder standing
                if old is not None:
                    await self._del(old)
                msg = await ch.send(
                    f"{' '.join(r.mention for r in roles)} Time to bump again! Use `/bump` \u23F0",
                    allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=roles),
                )
                cfg["msg"][str(cid)] = msg.id
                self._dirty()
        except asyncio.CancelledError:
            raise
        except discord.HTTPException as e:
            log.warning("bump reminder: couldn't post: %s", e)
        except Exception:
            log.exception("bump reminder failed")
        finally:
            if self.bump_tasks.get(cid) is asyncio.current_task():
                self.bump_tasks.pop(cid, None)

    def _top(self, guild, n):
        out = []
        for uid, c in sorted(self.data["bump"]["counts"].items(), key=lambda kv: kv[1], reverse=True)[:n]:
            mem = guild.get_member(int(uid))
            out.append((mem.display_name if mem else f"user {uid}", c))
        return out

    @commands.command(name="bumps")
    @commands.guild_only()
    async def bumps_cmd(self, ctx: commands.Context):
        """top bumpers (everybody can use it)"""
        top = self._top(ctx.guild, 10)
        if not top:
            return await ctx.reply("No bumps counted yet.", mention_author=False)
        lines = [f"**{i}.** {name} - {c} bump(s)" for i, (name, c) in enumerate(top, 1)]
        await ctx.reply(embed=discord.Embed(title="Top bumpers", description="\n".join(lines), color=PURPLE),
                        mention_author=False)

    @staticmethod
    def _roles_from(ai, guild, roles):
        """role names / ids (a list, or one string like 'Staff and UT Staff') -> (roles found, things that matched no role)"""
        if not isinstance(roles, (list, tuple, set)):
            roles = re.split(r"\s*(?:,|;|\+|&|\n|\band\b)\s*", str(roles))
        found, bad = {}, []
        for part in roles:
            part = str(part).strip()
            if not part:
                continue
            for q in re.findall(r"\d{15,20}", part) or [part]:
                r = ai._find_role(guild, q)
                if r is None:
                    bad.append(q)
                elif not r.is_default():
                    found[r.id] = r
        return list(found.values()), bad

    async def t_set_bump_alert(self, ctx, channel=None, roles=None, remind_minutes=None, ping_now=None, enabled=None):
        ai, g, cfg = self._ai(), ctx["guild"], self.data["bump"]
        if ai is None:
            return {"error": "ai_admin_cog isn't loaded."}
        new = {}
        if channel is not None and str(channel).strip():
            ch = ai._chan(ctx, channel)
            if ch is None or not hasattr(ch, "send"):
                return {"error": f"Channel '{channel}' not found."}
            new["channels"] = [ch.id]
        if roles:
            found, bad = self._roles_from(ai, g, roles)
            if bad:
                return {"error": "Role not found: " + ", ".join(bad) + " - check the name or id (list_roles shows them all)."}
            if not found:
                return {"error": "I can't tag @everyone - name a real role."}
            new["roles"] = [r.id for r in found]
        if remind_minutes is not None:
            new["remind"] = max(0, min(int(remind_minutes), 1440))
        if ping_now is not None:
            new["now"] = bool(ping_now)
        if enabled is not None:
            new["on"] = bool(enabled)
        if new:  # everything was validated first, so a typo never leaves a half-changed setting
            cfg.update(new)
            if enabled is None:
                cfg["on"] = True  # setting something up means "on"
            if not cfg["on"] or cfg["remind"] == 0:
                for cid in list(self.bump_tasks):
                    self._cancel_reminder(cid)
                cfg["due"].clear()
            self._dirty()

        if not cfg["roles"]:
            return {"ok": True, "summary": "Bump alert isn't set up yet - tell me which roles to tag and in which channel."}
        roles_now = self._bump_roles(g)
        names = ", ".join(f"**{r.name}**" for r in roles_now) or "(those roles don't exist any more)"
        where = ", ".join(c.mention for c in (g.get_channel_or_thread(i) for i in cfg["channels"]) if c) or "any channel"
        if not cfg["on"]:
            out = f"Bump alert is **OFF** (it was set to tag {names} in {where})."
        else:
            out = f"Bump alert is **ON** - {where}, tagging {names}.\n- I thank the bumper and count their bumps."
            if cfg["remind"]:
                out += (f"\n- **{cfg['remind']} min** after a bump I tag the roles once (\"time to bump again\"); when somebody "
                        "bumps, that message turns into the thank-you - no second ping.")
            if cfg["now"]:
                out += "\n- If nobody was reminded yet (first bump), I tag the roles right after the bump."
            due = [int(v) for v in cfg["due"].values()]
            if due and cfg["remind"]:
                out += f"\n- Next reminder <t:{min(due)}:R>."
        top = self._top(g, 3)
        if top:
            out += "\n- Top bumpers: " + ", ".join(f"{n} ({c})" for n, c in top)
        if new:
            out += self._persist_note()
        weak = [r.name for r in roles_now if not r.mentionable]
        if cfg["on"] and weak and not g.me.guild_permissions.mention_everyone:
            out += (f"\n\u26A0 I can't notify **{', '.join(weak)}** yet: turn on 'Allow anyone to @mention this role' for it, "
                    "or give me the *Mention Everyone* permission.")
        return {"ok": True, "summary": out}

    # ------------------------------------------------------------ AUTOMATIONS: engine
    def _rule(self, key):
        key = str(key).strip().lower().lstrip("`").rstrip("`")
        for r in self.data["rules"]:
            if r["id"].lower() == key or (r.get("name") and r["name"].lower() == key):
                return r
        return None

    def _start_loop(self, rid):
        old = self.loops.pop(rid, None)
        if old is not None and not old.done():
            old.cancel()
        self.loops[rid] = asyncio.create_task(self._loop_run(rid))

    def _stop_loop(self, rid):
        t = self.loops.pop(rid, None)
        if t is not None and not t.done():
            t.cancel()

    async def _loop_run(self, rid):
        try:
            await asyncio.sleep(random.uniform(1, 3))
            fails = 0
            while True:
                r = self._rule(rid)
                if r is None or not r.get("on"):
                    return
                fails = 0 if await self._loop_once(r) else fails + 1
                if fails >= 5:  # channel gone / no permission: stop instead of hammering Discord
                    r["on"] = False
                    self._dirty()
                    log.warning("loop %s paused: 5 failures in a row (channel deleted or no permission?)", rid)
                    return
                await asyncio.sleep(max(MIN_LOOP_SECONDS, int(r.get("every", 60))))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("loop %s crashed", rid)
        finally:
            if self.loops.get(rid) is asyncio.current_task():
                self.loops.pop(rid, None)

    async def _loop_once(self, r):
        ch = self.bot.get_channel(r.get("channel") or 0)
        if ch is None or not hasattr(ch, "send"):
            return False
        text = self._fmt(random.choice(r.get("texts") or RANDOM_POOL), ch.guild)
        try:
            msg = await ch.send(text[:2000] or "\u200b", allowed_mentions=NO_MENTIONS)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("loop %s: couldn't send: %s", r["id"], e)
            return False
        self._later_delete(msg, r.get("delete_after"))
        return True

    @commands.Cog.listener("on_message")
    async def rules_watch(self, m: discord.Message):
        if m.guild is None or m.author.id == self.bot.user.id:
            return
        rules = [r for r in self.data["rules"] if r.get("on") and r["kind"] == "on_message"]
        if not rules:
            return
        low = _blob(m).lower()
        for r in rules:
            if r.get("channel") and r["channel"] != m.channel.id:
                continue
            if m.author.bot and not r.get("include_bots"):
                continue
            kws = r.get("contains") or []
            if kws and not any(k.lower() in low for k in kws):
                continue
            try:
                await self._apply(r, m)
            except Exception:
                log.exception("rule %s failed", r["id"])

    async def _apply(self, r, m):
        act = r.get("action") or "reply"
        after = r.get("delete_after")
        text = random.choice(r["texts"]) if r.get("texts") else ""
        try:
            if act == "delete":
                self._spawn(self._del(m, after or 0))
            elif act == "react":
                for e in (r.get("emoji") or "\U0001F44D").split():
                    await m.add_reaction(e)
            elif act in ("reply", "send") and text:
                text = self._fmt(text, m.guild, m.author)[:2000]
                if act == "reply":
                    out = await m.reply(text, mention_author=False, allowed_mentions=NO_MENTIONS)
                else:
                    out = await m.channel.send(text, allowed_mentions=NO_MENTIONS)
                self._later_delete(out, after)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
            log.warning("rule %s: %s", r["id"], e)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        for r in [x for x in self.data["rules"] if x.get("on") and x["kind"] == "on_join"]:
            g = member.guild
            if r.get("role"):
                role = g.get_role(r["role"])
                if role is not None and not role.managed and role < g.me.top_role:
                    try:
                        await member.add_roles(role, reason=f"automation {r['id']}")
                    except discord.HTTPException as e:
                        log.warning("rule %s: couldn't give the role: %s", r["id"], e)
            if not r.get("texts"):
                continue
            text = self._fmt(random.choice(r["texts"]), g, member)[:2000]
            try:
                if r.get("dm"):
                    await member.send(text)
                else:
                    ch = g.get_channel(r.get("channel") or 0)
                    if ch is not None and hasattr(ch, "send"):
                        out = await ch.send(text, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[member]))
                        self._later_delete(out, r.get("delete_after"))
            except discord.HTTPException as e:
                log.warning("rule %s: couldn't send the welcome: %s", r["id"], e)

    # ------------------------------------------------------------ AUTOMATIONS: tools
    @staticmethod
    def _clean_list(items):
        if items is None:
            return []
        if isinstance(items, str):
            items = [items]
        return [str(x).strip()[:1900] for x in items if str(x).strip()][:40]

    def _describe(self, r):
        st = "" if r.get("on") else " *(paused)*"
        ch = f"<#{r['channel']}>" if r.get("channel") else "the whole server"
        da = r.get("delete_after")
        n = len(r.get("texts") or [])
        dl = "" if da is None else (", deleted at once" if da == 0 else f", deleted after {_dur(da)}")
        label = f" \"{r['name']}\"" if r.get("name") else ""
        if r["kind"] == "loop":
            what = f"one of {n} text(s)" if n else "a random built-in text"
            return f"`{r['id']}`{label} loop{st}: every **{_dur(r['every'])}** in {ch} post {what}{dl}"
        if r["kind"] == "on_message":
            kw = " / ".join(f"'{k}'" for k in r.get("contains") or []) or "every message"
            do = {
                "reply": f"reply with one of {n} text(s){dl}",
                "send": f"post one of {n} text(s){dl}",
                "react": f"react with {r.get('emoji')}",
                "delete": "delete it" + ("" if not da else f" after {_dur(da)}"),
            }.get(r.get("action"), r.get("action"))
            return f"`{r['id']}`{label} rule{st}: in {ch}, on {kw} -> {do}"
        parts = []
        if r.get("role"):
            parts.append(f"give <@&{r['role']}>")
        if n:
            parts.append("DM one of " + f"{n} text(s)" if r.get("dm") else f"post one of {n} text(s) in {ch}")
        return f"`{r['id']}`{label} welcome{st}: when someone joins -> " + " and ".join(parts)

    async def t_auto_add(self, ctx, kind, channel=None, texts=None, every_seconds=None, delete_after_seconds=None,
                         contains=None, action=None, emoji=None, role=None, dm=None, include_bots=None, name=None):
        ai, g = self._ai(), ctx["guild"]
        if ai is None:
            return {"error": "ai_admin_cog isn't loaded."}
        kind = {"message": "on_message", "join": "on_join", "welcome": "on_join", "repeat": "loop"}.get(
            str(kind).strip().lower(), str(kind).strip().lower())
        if kind not in ("loop", "on_message", "on_join"):
            return {"error": "kind must be loop, on_message or on_join."}
        if len(self.data["rules"]) >= MAX_RULES:
            return {"error": f"Too many automations ({MAX_RULES}) - remove one first (auto_list shows them)."}
        ch = None
        if channel is not None and str(channel).strip():
            ch = ai._chan(ctx, channel)
            if ch is None or not hasattr(ch, "send"):
                return {"error": f"Channel '{channel}' not found."}
        tx = self._clean_list(texts)
        after = None if delete_after_seconds is None else max(0, min(int(delete_after_seconds), 86400))
        rule = {"id": "", "kind": kind, "on": True, "channel": ch.id if ch else None, "texts": tx,
                "name": (name or "").strip()[:60], "by": ctx["author"].id, "at": int(time.time()), "delete_after": after}

        if kind == "loop":
            if ch is None:
                return {"error": "I need a channel for the loop - which one?"}
            if every_seconds is None:
                return {"error": "How often should it run? I need the interval (for example every 30 seconds)."}
            if not ch.permissions_for(g.me).send_messages:
                return {"error": f"I can't write in #{ch.name} (give the bot View Channel + Send Messages there)."}
            rule["every"] = max(MIN_LOOP_SECONDS, min(int(every_seconds), 604800))
        elif kind == "on_message":
            act = str(action or ("reply" if tx else "delete")).lower()
            if act not in ("reply", "send", "react", "delete"):
                return {"error": "action must be reply, send, react or delete."}
            if act in ("reply", "send") and not tx:
                return {"error": "What should I write? I need the text."}
            if act == "react" and not (emoji or "").strip():
                return {"error": "Which emoji should I react with?"}
            if act == "delete" and ch is None:
                return {"error": "A delete rule needs a channel - which one?"}
            if act == "delete":
                rule["delete_after"] = after or 0
            rule.update(action=act, contains=self._clean_list(contains), emoji=(emoji or "").strip(),
                        include_bots=bool(include_bots))
        else:
            role_obj = None
            if role:
                role_obj = ai._find_role(g, role)
                if role_obj is None:
                    return {"error": f"Role '{role}' not found."}
                bad = ai._role_ok(ctx, role_obj)
                if bad:
                    return {"error": bad}
            if not tx and role_obj is None:
                return {"error": "Tell me what to post or which role to give new members."}
            if tx and not dm and ch is None:
                return {"error": "In which channel should I post the welcome (or should I DM it)?"}
            rule.update(role=role_obj.id if role_obj else None, dm=bool(dm))

        rule["id"] = f"r{self.data['seq'] + 1}"
        if kind == "loop" or rule.get("action") == "delete":
            if not await ai._confirm(ctx, "Create automation:\n" + self._describe(rule)
                                     + ("\nDiscord allows about 5 messages per 5 s per channel." if kind == "loop" else "")):
                return {"cancelled": True}
        self.data["seq"] += 1
        self.data["rules"].append(rule)
        if kind == "loop":
            self._start_loop(rule["id"])
        self._dirty()
        await ai._log_action(ctx, f"automation {rule['id']} created ({kind})")
        out = "Created " + self._describe(rule)
        if kind == "loop" and not tx:
            out += "\n(no texts given - using the built-in random texts)"
        return {"ok": True, "id": rule["id"], "summary": out + self._persist_note()}

    async def t_auto_list(self, ctx):
        rules = self.data["rules"]
        if not rules:
            return {"ok": True, "summary": "No automations yet. Example: `send a random message in #chat every 30 seconds and delete it`."}
        lines = [self._describe(r) for r in rules[:30]]
        more = len(rules) - len(lines)
        return {"ok": True, "summary": "**Automations:**\n" + "\n".join(f"- {x}" for x in lines)
                + (f"\n+{more} more" if more > 0 else "")}

    async def t_auto_remove(self, ctx, rule_id):
        ai = self._ai()
        if str(rule_id).strip().lower() == "all":
            if not self.data["rules"]:
                return {"ok": True, "summary": "There are no automations."}
            if not await ai._confirm(ctx, f"REMOVE ALL **{len(self.data['rules'])}** automations"):
                return {"cancelled": True}
            for r in self.data["rules"]:
                self._stop_loop(r["id"])
            n = len(self.data["rules"])
            self.data["rules"].clear()
            self._dirty()
            return {"ok": True, "summary": f"Removed **{n}** automation(s)."}
        r = self._rule(rule_id)
        if r is None:
            ids = ", ".join(x["id"] for x in self.data["rules"]) or "none"
            return {"error": f"No automation '{rule_id}'. Existing ids: {ids}."}
        if not await ai._confirm(ctx, "Remove automation:\n" + self._describe(r)):
            return {"cancelled": True}
        self._stop_loop(r["id"])
        self.data["rules"].remove(r)
        self._dirty()
        await ai._log_action(ctx, f"automation {r['id']} removed")
        return {"ok": True, "summary": f"Removed automation `{r['id']}`."}

    async def t_auto_toggle(self, ctx, rule_id, enabled):
        r = self._rule(rule_id)
        if r is None:
            ids = ", ".join(x["id"] for x in self.data["rules"]) or "none"
            return {"error": f"No automation '{rule_id}'. Existing ids: {ids}."}
        r["on"] = bool(enabled)
        if r["kind"] == "loop":
            self._start_loop(r["id"]) if r["on"] else self._stop_loop(r["id"])
        self._dirty()
        return {"ok": True, "summary": f"Automation `{r['id']}` is now **{'running' if r['on'] else 'paused'}**."}


async def setup(bot: commands.Bot):
    await bot.add_cog(Automation(bot))
