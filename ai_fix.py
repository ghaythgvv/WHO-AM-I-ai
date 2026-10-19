"""
ai_fix.py  -  fixes the AI admin at RUNTIME. It does NOT edit ai_admin_cog.py, so there are no patch scripts that can
silently fail any more (your screenshots show they did not apply).

What it fixes
  1. DOUBLE BUMP MESSAGES  - the old bump alert inside ai_admin_cog (pings the roles at once) ran together with the new one
     in auto_cog, and its message made the new one think "already answered". The old one is switched off, the new one
     (thank-you + count + ONE reminder that turns into the thank-you) is the only one left.
  2. "u join" / "join voice 3" / "join vc <id>" MOVED YOU or asked "which member?"  -> the BOT joins now (no AI call,
     works even when the free limit is used up). "move Ali to voice 3" still moves Ali.
  3. voice tools for the AI: follow me, unfollow, leave, say out loud, music (play/skip/pause/resume/stop/volume/loop/queue)
  4. automation tools (loops, rules, welcome, bump alert v2) are registered even if the old patch did not apply
  5. "I'm not able to ..." counts as a refusal (the AI is pushed to really try the tool)
  6. the AI may only create / remove an automation when your CURRENT message asks for it
  7. Groq error "tool call validation failed ... /reason: expected string, but got null" (the model passes null for an
     optional field): the bad call is repaired (nulls dropped) and run, instead of "I didn't understand that one"
  8. a reply that is ONLY a mention of the bot (an answer to "which member?") no longer gets "Yo! How can I help?" - it is
     understood as "you, the bot"; "came to my vc" / "ta3al" work like "come"; the bot remembers more of the conversation
 10. BUMP PING: STAFF and UT STAFF are tagged when it is time to bump again (DISBOARD's cooldown = 120 min). The bump itself
     gets a quiet thank-you + the next bump time (the waiting reminder turns into it). One-time default set by this file:
     remind 120 min, no ping at the bump. Say "bump alert: ping right after every bump" if you want that too.
 11. "take it off" / "remove r1" / "pause r2" / "list automations" work instantly (no AI call, no free-limit problem), and
     the AI always sees the automation tools while automations exist - it no longer says "I have no tool for that"
 12. Railway volume: if a volume is mounted, automations are saved on it (RAILWAY_VOLUME_MOUNT_PATH) - they survive
     redeploys and the "Railway wipes files" warning disappears
  9. loads owner_guard.py by itself if it is in the same folder (one load line is enough)

!aicheck (administrator) prints a short health report: what is loaded, ffmpeg/opus, bump settings.

LOAD IT LAST (after ai_admin_cog, auto_cog, voice_cog):   await bot.load_extension("ai_fix")
You can delete the "[patch]" block in your main file and patch_ai_admin*.py - this file replaces them.
"""

import asyncio
import importlib.util
import json
import logging
import os
import random
import re
import shutil
import time
import uuid
from collections import defaultdict, deque
from types import SimpleNamespace

import discord
import openai
from discord.ext import commands

log = logging.getLogger("ai_fix")

VOICE_MISSING = "voice_cog isn't loaded - add  await bot.load_extension('voice_cog')  in your main file."

EXTRA_PROMPT = (
    " If the staff member answers a question of yours with only a mention of you (the bot), or with 'u' / 'you', they mean YOU, "
    "the bot - for 'come / join / came to my vc' use bot_join_voice, never voice_action. "
    " TOOL ARGUMENTS: leave optional arguments OUT - never pass null for them. "
    " NEVER create, change or remove an automation (auto_add / auto_remove / auto_toggle / set_bump_alert) unless the "
    "staff member's CURRENT message clearly asks for it - earlier messages in the history are finished, never repeat "
    "them. A question or a request to look something up is never an automation. "
    "VOICE: you have your own voice (bot_join_voice, bot_follow, bot_unfollow, bot_leave_voice, bot_speak, bot_music). "
    "'join me / come / join my vc' = the BOT joins the staff member's voice channel (bot_join_voice) - never move the "
    "staff member for that and never ask which channel; 'join voice 3' / 'u join Lounge' = bot_join_voice with "
    "channel=that name (the BOT goes there); 'follow me' = bot_follow; 'say X out loud / speak X' = bot_speak with the "
    "exact text; play / skip / pause / resume / stop / volume / loop / queue = bot_music. voice_action move is ONLY for "
    "moving ANOTHER member, e.g. 'move Ali to voice 3' - never use it when the staff member tells YOU (the bot) to join "
    "or come, and never treat 'u' / 'you' as a member name. Never say you can't join voice without trying the tool. "
)

# ------------------------------------------------------------------ instant (no-AI) patterns
_V_PRE = r"(?:(?:please|pls|plz|u|you|bot|hey|yo)\s+)*"
_V_POST = r"(?:\s+(?:please|pls|plz|now))?\s*[!.?]*\s*$"
_VQ_FOLLOW = re.compile(rf"^\s*{_V_PRE}follow\s+me{_V_POST}", re.I)
_VQ_UNFOLLOW = re.compile(
    rf"^\s*{_V_PRE}(?:unfollow(?:\s+me)?|stop\s+follow(?:ing)?(?:\s+me)?|don['’]?t\s+follow(?:\s+me)?){_V_POST}", re.I
)
_VQ_JOIN = re.compile(
    rf"^\s*{_V_PRE}(?:come|came|coem|com|join|jion|ta3al|ta3a|تعال)(?:\s+(?:to|in|into))?"
    r"(?:\s+(?:me|us|here|(?:my|the)\s*(?:vc|voice(?:\s*chat|\s*channel)?|channel|room)|vc|voice(?:\s*chat|\s*channel)?))?"
    rf"{_V_POST}",
    re.I,
)
_VQ_JOINCH = re.compile(rf"^\s*{_V_PRE}(?:come|came|coem|com|join|jion|ta3al|ta3a|تعال)(?:\s+(?:to|in|into))?\s+(?P<c>.+?){_V_POST}", re.I)
_VQ_LEAVE = re.compile(
    rf"^\s*{_V_PRE}(?:leave|disconnect|get\s+out|go\s+out)"
    r"(?:\s+(?:the\s+|my\s+|from\s+(?:the\s+)?)?(?:vc|voice(?:\s*chat|\s*channel)?|channel))?"
    rf"{_V_POST}",
    re.I,
)
_VQ_CTRL = re.compile(
    r"^\s*(?P<c>skip|next|pause|resume|unpause|stop\s+(?:the\s+)?music|now\s+playing|what['’]?s\s+playing|queue|loop)\s*[!.?]*\s*$",
    re.I,
)
_VQ_VOL = re.compile(r"^\s*(?:set\s+)?volume\s+(?:to\s+)?(?P<v>\d{1,3})\s*%?\s*$", re.I)
_VQ_PLAY = re.compile(r"^\s*(?:play|put\s+music)\s+(?P<q>.+?)\s*$", re.I)
_VQ_SPEAK = re.compile(
    r"^\s*(?:speak|say)\s+(?P<t>.+?)\s+(?:out\s*loud|aloud|in\s+(?:the\s+|my\s+)?(?:vc|voice(?:\s*chat|\s*channel)?))\s*$", re.I
)
_VQ_SPEAK2 = re.compile(
    r"^\s*speak\s+(?!(?:in\s+)?(?:english|arabic|french|tunis\w*|darija|derja|tounsi)\b)(?P<t>.+)$", re.I
)
_GREET = re.compile(
    r"\s*(?:yo+|hey+|hi+|hello+|hiya|sup|wassup|what'?s up|salam|salut|hola|bonjour|slm|ahla|aslema|3aslema)\s*[!.?]*\s*", re.I
)

_FILLER = r"(?:(?:ok(?:ay)?|nice|good|great|thanks?|thx|cool|now|alright|and|then|so|yo|hey|bro|please|pls|plz|u|you)[\s,!.]+)*"
_AQ_LIST = re.compile(
    rf"^\s*{_FILLER}(?:list|show)(?:\s+me)?\s+(?:all\s+|my\s+|the\s+)?(?:automations?|loops?|rules?)\s*[!.?]*\s*$", re.I
)
_AQ_VERB = re.compile(
    rf"^\s*{_FILLER}(?P<v>take\s+it\s+off|take\s+off|turn\s+(?:it\s+)?off|remove|delete|cancel|kill|end|stop|pause|resume|unpause|start)"
    r"(?P<rest>\s+.*)?$",
    re.I | re.S,
)

VOICE_TOOL_METHODS = (
    "t_bot_join_voice", "t_bot_follow", "t_bot_unfollow", "t_bot_leave_voice", "t_bot_voice_status", "t_bot_speak",
    "t_bot_music",
)
AUTO_TOOL_METHODS = ("t_auto_add", "t_auto_list", "t_auto_remove", "t_auto_toggle")


def _voice_tools(m):
    _tool, _s, _i, _b, _MEM = m._tool, m._s, m._i, m._b, m._MEM
    return [
        _tool("bot_join_voice",
              "Make the BOT join a voice channel (default: the voice channel the staff member is in right now). Use it for "
              "'join me / come / join my vc / join voice 3'. This does NOT move the staff member.",
              {"channel": _s("voice channel name/id; leave out = the staff member's own channel"),
               "member": _s(_MEM + " - join THIS member's channel instead")}),
        _tool("bot_follow",
              "The bot joins a member and then follows them from voice channel to voice channel. 'follow me'. "
              "Default member = the staff member.",
              {"member": _s(_MEM)}),
        _tool("bot_unfollow", "Stop following."),
        _tool("bot_leave_voice", "The bot disconnects from voice and clears its music queue."),
        _tool("bot_voice_status", "Where the bot is in voice, who it follows, what it plays."),
        _tool("bot_speak",
              "Text-to-speech: the bot says `text` OUT LOUD in its voice channel (it joins the staff member's channel if "
              "needed). Use the text exactly as the staff member wrote it.",
              {"text": _s(), "lang": _s(enum=["en", "ar", "fr"]), "voice": _s(enum=["female", "male"])}, ["text"]),
        _tool("bot_music",
              "Music in the voice channel. action: play (query = song name or link), skip, pause, resume, stop (clears the "
              "queue), volume (volume 1-150), loop (enabled true/false, leave out = toggle), queue, nowplaying.",
              {"action": _s(enum=["play", "skip", "pause", "resume", "stop", "volume", "loop", "queue", "nowplaying"]),
               "query": _s("song name or link"), "volume": _i("1-150"), "enabled": _b()}, ["action"]),
    ]


class AIFix(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai = None

    # ------------------------------------------------------------------ install
    async def cog_load(self):
        ai = self.bot.get_cog("AIAdmin")
        if ai is None:
            log.warning("ai_fix: the AIAdmin cog isn't loaded - load ai_admin_cog BEFORE ai_fix")
            return
        self.ai = ai
        try:
            import ai_admin_cog as m
        except ImportError:
            log.warning("ai_fix: can't import ai_admin_cog")
            return
        if getattr(ai, "_ai_fix_done", False):
            return
        ai._ai_fix_done = True
        self._tools(m)
        self._bump(ai)
        auto = self.bot.get_cog("Automation")
        if auto is not None:
            self._fix_bump(auto, ai)
            try:
                await self._volume(auto)
            except Exception:
                log.exception("ai_fix: volume setup failed")
            self._bump_defaults(auto)
        self._bind(ai)
        self._wrap(ai)
        log.info("ai_fix: installed (voice tools, automation tools, bump ping, old bump alert off)")

    def _tools(self, m):
        names, new = set(), []
        try:
            from auto_cog import AUTO_CONFIRM, AUTO_READ, AUTO_TOOLS

            new += list(AUTO_TOOLS)
            m.READ_TOOLS |= set(AUTO_READ)
            m.CONFIRM_TOOLS |= set(AUTO_CONFIRM)
        except ImportError:
            log.warning("ai_fix: auto_cog.py not found - automations are not available")
        new += _voice_tools(m)
        names = {t["function"]["name"] for t in new}
        m.TOOLS[:] = [t for t in m.TOOLS if t["function"]["name"] not in names] + new  # in place: other code holds this list
        m.TOOL_NAMES = set(m.TOOL_NAMES) | names
        for t in m.TOOLS:  # ping_now now means: tag the roles right after EVERY bump
            if t["function"]["name"] == "set_bump_alert":
                pr = t["function"]["parameters"].get("properties", {})
                if "ping_now" in pr:
                    pr["ping_now"]["description"] = "tag the roles right after EVERY bump (default true); false = quiet thank-you"
        m.INT_ARGS = tuple(dict.fromkeys(tuple(m.INT_ARGS) + ("every_seconds", "delete_after_seconds", "volume")))
        m.BOOL_ARGS = tuple(dict.fromkeys(tuple(m.BOOL_ARGS) + ("dm", "include_bots")))
        m.TOOL_GROUPS.append((
            re.compile(r"\bjoin\b|\bcome\b|follow|\bleave\b|speak|out ?loud|aloud|\btts\b|music|\bplay|\bsong|\bskip|pause|"
                       r"resume|volume|queue|\bsing|\bvc\b|voice|تعال|اتبع|شغل|غني|تكلم"),
            {"bot_join_voice", "bot_follow", "bot_unfollow", "bot_leave_voice", "bot_voice_status", "bot_speak", "bot_music"},
        ))
        m.TOOL_GROUPS.append((
            re.compile(r"every|loop|repeat|automat|schedul|interval|welcome|keyword|rule|random|forever|nonstop|whenever|"
                       r"when someone|\bauto"),
            {"auto_add", "auto_list", "auto_remove", "auto_toggle", "list_channels", "list_roles"},
        ))
        # "I'm not able to ..." is a refusal too -> the AI gets pushed to really try a tool
        old = m.REFUSAL_RE
        m.REFUSAL_RE = re.compile(f"(?:{old.pattern})|(?:^\\s*(?:i['’]?m not able|i am not able))", old.flags)

    def _bump(self, ai):
        """switch the OLD bump alert (inside ai_admin_cog) off - auto_cog has the new one"""
        if isinstance(getattr(ai, "bump", None), dict):
            ai.bump["on"] = False
        for name, event in (("bump_watch", "on_message"), ("bump_watch_edit", "on_message_edit")):
            fn = getattr(ai, name, None)
            if fn is not None:
                try:
                    self.bot.remove_listener(fn, event)
                except Exception:
                    pass
        for t in list(getattr(ai, "bump_tasks", {}).values()):
            try:
                t.cancel()
            except Exception:
                pass
        auto = self.bot.get_cog("Automation")
        if auto is not None:
            ai.t_set_bump_alert = auto.t_set_bump_alert  # the AI tool now talks to the new bump alert

    def _bind(self, ai):
        auto = self.bot.get_cog("Automation")
        if auto is not None:
            for n in AUTO_TOOL_METHODS:
                if hasattr(auto, n):
                    setattr(ai, n, getattr(auto, n))
        for n in VOICE_TOOL_METHODS:
            setattr(ai, n, getattr(self, n))

    def _wrap(self, ai):
        orig_agent, orig_system, fix = ai._agent, ai._system, self

        async def _agent(ctx, text):
            try:
                if ai._authorized(ctx["author"]):
                    quick = fix._quick_greet(text) or await fix._quick_voice(ctx, text) or await fix._quick_auto(ctx, text)
                    if quick:
                        ai._remember(ctx, text, quick)
                        return quick
            except Exception:
                log.exception("ai_fix quick path failed")
            return await orig_agent(ctx, text)

        ai._agent = _agent
        ai._system = lambda ctx: orig_system(ctx) + EXTRA_PROMPT
        # while automations exist the AI always sees list / remove / pause (before: "I have no tool for that")
        import ai_admin_cog as m

        orig_tools_for = ai._tools_for

        def _tools_for(text):
            tools = orig_tools_for(text)
            auto = fix.bot.get_cog("Automation")
            if auto is not None and auto.data["rules"] and tools is not m.TOOLS:
                have = {t["function"]["name"] for t in tools}
                want = ("auto_list", "auto_remove", "auto_toggle")
                tools = list(tools) + [t for t in m.TOOLS if t["function"]["name"] in want and t["function"]["name"] not in have]
            return tools

        ai._tools_for = _tools_for
        ai._complete = self._complete  # same as the original, plus the repair of "got null" tool calls

        # remember more of the conversation (the original keeps only the last 4 exchanges)
        bigger = defaultdict(lambda: deque(maxlen=12))
        for k, v in ai.history.items():
            bigger[k].extend(v)
        ai.history = bigger

        # "@bot" alone as the answer to the bot's own question must not reset the chat with "Yo! How can I help?"
        orig_on_message = ai.on_message
        self.bot.remove_listener(orig_on_message, "on_message")

        async def on_message(message):
            try:
                if await fix._bare_mention(message):
                    return
            except Exception:
                log.exception("ai_fix bare mention failed")
            await orig_on_message(message)

        self.bot.add_listener(on_message, "on_message")

    async def _bare_mention(self, message):
        """True if this message was a reply to a QUESTION of the bot containing nothing but a mention of the bot"""
        ai, me = self.ai, self.bot.user
        if message.author.bot or not message.guild or message.content.startswith("!") or me not in message.mentions:
            return False
        if re.sub(rf"<@!?{me.id}>", "", message.content).strip():
            return False
        ref = message.reference
        replied = ref.resolved if ref is not None and isinstance(ref.resolved, discord.Message) else None
        if replied is None or replied.author.id != me.id or "?" not in (replied.content or ""):
            return False
        if not ai._authorized(message.author):
            return False
        await ai._handle(message, f"@{me.display_name} (id {me.id}) - that is you, the bot", replied)
        return True

    # ------------------------------------------------------------------ AI call with repair
    @staticmethod
    def _recover(e):
        """Groq rejected a tool call because an optional argument was null -> rebuild the call without the nulls"""
        text = str(e)
        if "null" not in text:
            return None
        body = getattr(e, "body", None)
        fg = body.get("failed_generation") if isinstance(body, dict) else None
        if not fg:
            mm = re.search(r"'failed_generation': '(.*)'\}", text, re.S)
            fg = mm.group(1).encode().decode("unicode_escape") if mm else None
        if not isinstance(fg, str) or "{" not in fg:
            return None
        try:
            obj = json.loads(fg[fg.index("{"): fg.rindex("}") + 1])
            name, args = obj["name"], obj.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args or "{}")
            if not isinstance(name, str) or not isinstance(args, dict):
                return None
        except (ValueError, KeyError, TypeError):
            return None
        args = {k: v for k, v in args.items() if v is not None}
        tc = SimpleNamespace(id="call_" + uuid.uuid4().hex[:10], type="function",
                             function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        log.info("ai_fix: repaired a tool call with null arguments: %s %s", name, args)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[tc]))])

    async def _complete(self, msgs, tools):
        ai = self.ai
        import ai_admin_cog as m

        model, plain, switched, bad, net = m.MODEL, False, False, 0, 0
        while True:
            try:
                return await ai.ai.chat.completions.create(
                    model=model, max_tokens=1500, temperature=0.3, messages=msgs, tools=tools, tool_choice="auto",
                    **ai._extra(model, plain),
                )
            except openai.RateLimitError:
                # limits are per model: if the main one is used up, the backup model still has room
                if switched or not m.FALLBACK_MODEL or m.FALLBACK_MODEL == model:
                    raise
                model, switched = m.FALLBACK_MODEL, True
            except openai.BadRequestError as e:
                fixed = self._recover(e)
                if fixed is not None:
                    return fixed
                bad += 1  # the model sometimes writes a broken tool call - retry
                if bad > 2:
                    raise
                plain = True
                await asyncio.sleep(0.5)
            except (openai.APIConnectionError, openai.InternalServerError):
                net += 1
                if net > 1:
                    raise
                await asyncio.sleep(1)

    # ------------------------------------------------------------------ voice tools (the AI calls these)
    def _voice(self):
        return self.bot.get_cog("VoiceDJ")

    @staticmethod
    def _is_me(q):
        """empty / 'me' / 'here' ... = the staff member himself"""
        return q is None or str(q).strip().lower() in (
            "", "me", "i", "myself", "my", "mine", "here", "us", "this", "my vc", "my channel", "my voice channel", "my voice",
        )

    def _voice_chan_for(self, ctx, q):
        """the voice channel named in q (None if q is not a voice channel)"""
        q = str(q).strip().strip('"')
        if self._is_me(q):
            return None
        ch = self.ai._find_channel(ctx["guild"], q)
        return ch if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) else None

    async def t_bot_join_voice(self, ctx, channel=None, member=None):
        v = self._voice()
        if v is None:
            return {"error": VOICE_MISSING}
        g, ch, mem = ctx["guild"], None, None
        if not self._is_me(channel):
            ch = self._voice_chan_for(ctx, channel)
            if ch is None:
                return {"error": f"Voice channel '{channel}' not found."}
        if not self._is_me(member):
            mem, err = await self.ai._one_member(g, member)
            if err:
                return err
        return await v.api_join(ctx, channel=ch, member=mem)

    async def t_bot_follow(self, ctx, member=None):
        v = self._voice()
        if v is None:
            return {"error": VOICE_MISSING}
        mem = None
        if not self._is_me(member):
            mem, err = await self.ai._one_member(ctx["guild"], member)
            if err:
                return err
        return await v.api_follow(ctx, member=mem)

    async def t_bot_unfollow(self, ctx):
        v = self._voice()
        return await v.api_unfollow(ctx) if v is not None else {"error": VOICE_MISSING}

    async def t_bot_leave_voice(self, ctx):
        v = self._voice()
        return await v.api_leave(ctx) if v is not None else {"error": VOICE_MISSING}

    async def t_bot_voice_status(self, ctx):
        v = self._voice()
        return await v.api_status(ctx) if v is not None else {"error": VOICE_MISSING}

    async def t_bot_speak(self, ctx, text, lang=None, voice=None):
        v = self._voice()
        if v is None:
            return {"error": VOICE_MISSING}
        return await v.api_say(ctx, text, lang=lang, voice=voice)

    async def t_bot_music(self, ctx, action, query=None, volume=None, enabled=None):
        v = self._voice()
        if v is None:
            return {"error": VOICE_MISSING}
        a = re.sub(r"[\s_-]+", "", str(action).lower())
        a = {"next": "skip", "np": "nowplaying", "current": "nowplaying", "unpause": "resume", "start": "play",
             "add": "play", "list": "queue", "vol": "volume", "repeat": "loop"}.get(a, a)
        return await v.api_music(ctx, a, query=query, volume=volume, enabled=enabled)

    # ------------------------------------------------------------------ instant shortcuts (no AI call)
    @staticmethod
    def _quick_greet(text):
        if _GREET.fullmatch(text):
            return random.choice(["Yo! \U0001F44B What do you need?", "Hey! What can I do for you?", "Yo, I'm here - what's up?"])
        return None

    async def _quick_voice(self, ctx, text):
        """follow me / join me / join voice 3 / leave / skip / play <song> / volume 60 / say hi out loud"""
        t = text.strip()
        call = None
        if _VQ_UNFOLLOW.match(t):
            call = ("bot_unfollow", {})
        elif _VQ_FOLLOW.match(t):
            call = ("bot_follow", {})
        elif _VQ_LEAVE.match(t):
            call = ("bot_leave_voice", {})
        elif _VQ_JOIN.match(t):
            call = ("bot_join_voice", {})
        elif (m := _VQ_JOINCH.match(t)) and (vch := self._voice_chan_for(ctx, m.group("c"))) is not None:
            call = ("bot_join_voice", {"channel": str(vch.id)})
        else:
            m = _VQ_CTRL.match(t)
            if m:
                c = re.sub(r"\s+", " ", m.group("c").lower().replace("’", "'"))
                if c.startswith("stop"):
                    a = "stop"
                elif c in ("now playing", "what's playing"):
                    a = "nowplaying"
                else:
                    a = {"next": "skip", "unpause": "resume"}.get(c, c)
                call = ("bot_music", {"action": a})
            elif _VQ_VOL.match(t):
                call = ("bot_music", {"action": "volume", "volume": int(_VQ_VOL.match(t).group("v"))})
            elif _VQ_PLAY.match(t):
                call = ("bot_music", {"action": "play", "query": _VQ_PLAY.match(t).group("q")})
            else:
                m = _VQ_SPEAK.match(t) or _VQ_SPEAK2.match(t)
                if m:
                    call = ("bot_speak", {"text": m.group("t").strip().strip('"')})
        if call is None:
            return None
        return self.ai._quick_result(await self.ai._dispatch(call[0], call[1], ctx))

    # ------------------------------------------------------------------ bump alert: ping on every bump
    def _fix_bump(self, auto, ai):
        cfg_of = lambda: auto.data["bump"]  # noqa: E731

        async def _bump_handle(m):
            cfg, ch, g = cfg_of(), m.channel, m.guild
            await asyncio.sleep(random.uniform(0.2, 1.2))  # a 2nd copy of the bot: the slower one sees the answer and stops
            if await auto._bump_answered(m):
                log.info("bump alert: already answered by another copy of the bot, skipping")
                return
            roles = auto._bump_roles(g)
            if not roles:
                log.warning("bump alert: none of the configured roles exist in this server")
                return
            who = auto._bumper(m)
            count = 0
            if who is not None:
                k = str(who.id)
                cfg["counts"][k] = cfg["counts"].get(k, 0) + 1
                count = cfg["counts"][k]
            cid = str(ch.id)
            due = int(time.time()) + cfg["remind"] * 60 if cfg["remind"] > 0 else 0
            thanks = auto._thanks(who, count, due)
            ping = bool(cfg["now"])
            waiting = await auto._find_reminder(ch)
            sent = False
            if waiting is not None:
                if ping:
                    await auto._del(waiting)  # the new message pings the roles - the old reminder just goes away
                else:
                    try:
                        await waiting.edit(content=thanks, allowed_mentions=discord.AllowedMentions.none())
                        sent = True
                    except discord.HTTPException:
                        sent = False
            if not sent:
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
                auto._schedule_reminder(ch.id, due)
            else:
                cfg["due"].pop(cid, None)
                auto._cancel_reminder(ch.id)
            auto._dirty()

        auto._bump_handle = _bump_handle

        # the status text of the tool must describe the NEW behaviour
        orig = auto.t_set_bump_alert

        async def t_set_bump_alert(ctx, channel=None, roles=None, remind_minutes=None, ping_now=None, enabled=None):
            res = await orig(ctx, channel=channel, roles=roles, remind_minutes=remind_minutes, ping_now=ping_now, enabled=enabled)
            if isinstance(res, dict) and isinstance(res.get("summary"), str):
                cfg = cfg_of()
                sm = res["summary"]
                sm = re.sub(r"\n- \*\*\d+ min\*\* after a bump I tag the roles once[^\n]*",
                            f"\n- **{cfg['remind']} min** after a bump I tag the roles: \"time to bump again\".", sm)
                new_line = "\n- I tag the roles right after every bump." if cfg["now"] else "\n- Thank-yous are quiet (no ping at the bump)."
                if "If nobody was reminded yet" in sm:
                    sm = re.sub(r"\n- If nobody was reminded yet[^\n]*", new_line, sm)
                elif cfg["on"] and cfg["roles"]:
                    sm = sm.replace("\n- I thank the bumper and count their bumps.", "\n- I thank the bumper and count their bumps." + new_line, 1)
                res["summary"] = sm
            return res

        auto.t_set_bump_alert = t_set_bump_alert
        ai.t_set_bump_alert = t_set_bump_alert

    def _bump_defaults(self, auto):
        """one-time: tag the roles when it is time to bump again (DISBOARD cooldown 120 min), not at the bump itself"""
        cfg = auto.data["bump"]
        if cfg.get("fix_v3"):
            return  # already done once - later changes you make by chat are kept
        old = int(cfg.get("remind", 0) or 0)
        cfg["now"], cfg["remind"], cfg["fix_v3"] = False, 120, True
        delta = (120 - old) * 60 if old > 0 else 0
        for cid, due in list(cfg["due"].items()):  # a reminder that is already waiting moves to the new cooldown
            cfg["due"][cid] = int(due) + delta
            if self.bot.is_ready():
                auto._schedule_reminder(int(cid), cfg["due"][cid])
        auto._dirty()

    # ------------------------------------------------------------------ Railway volume
    async def _volume(self, auto):
        """a Railway volume is mounted -> keep the automations on it (survives redeploys)"""
        import auto_cog

        mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        if not mount or os.getenv("AUTO_DATA_FILE", "").strip() or not os.path.isdir(mount):
            return
        new = os.path.join(mount, "auto_data.json")
        if os.path.abspath(auto_cog.DATA_FILE) == os.path.abspath(new):
            return
        auto_cog.DATA_FILE = new
        auto._persist_note = lambda: ""  # no more "Railway wipes files" warning
        if os.path.exists(new):  # saved settings from an earlier run: use them
            for rid in list(auto.loops):
                auto._stop_loop(rid)
            for cid in list(auto.bump_tasks):
                auto._cancel_reminder(cid)
            await auto._load()
            if self.bot.is_ready():
                await auto._boot()
        else:
            auto._dirty()  # first run on the volume: write the current settings there
        log.info("ai_fix: automations are saved on the Railway volume: %s", new)

    # ------------------------------------------------------------------ automations: instant management
    async def _quick_auto(self, ctx, text):
        """list automations / remove r1 / take it off / pause r2 / resume r2 - instant, no AI call"""
        auto, ai = self.bot.get_cog("Automation"), self.ai
        if auto is None:
            return None
        t = text.strip()
        if _AQ_LIST.match(t):
            return ai._quick_result(await ai._dispatch("auto_list", {}, ctx))
        m = _AQ_VERB.match(t)
        if not m:
            return None
        verb = re.sub(r"\s+", " ", m.group("v").lower())
        rest = (m.group("rest") or "").strip().lower()
        rest = re.sub(r"\b(?:from|in|on|at|for)\b.*$", "", rest).strip(" .!?,")  # "... from test" is only a label
        rest = re.sub(r"^(?:the|that|this|my)\s+", "", rest)
        rid = re.search(r"\br\d+\b", rest)
        noun = re.search(r"\b(?:automations?|loops?|rules?)\b", rest)
        every = re.search(r"\b(?:all|everything|every)\b", rest)
        rules = auto.data["rules"]
        if not (rid or noun):  # only "it" / nothing: accept it when we were just talking about an automation
            hist = ai.history.get(ctx["channel"].id)
            last = hist[-1].get("content", "") if hist and hist[-1].get("role") == "assistant" else ""
            if not (rest in ("", "it", "that", "this", "them") and rules and re.search(r"loop|automation|rule|\br\d+\b", last, re.I)):
                return None
        if not rules:
            return "There are no automations."
        toggle = verb in ("pause", "resume", "unpause", "start")
        if rid:
            target = [r for r in rules if r["id"].lower() == rid.group()]
            if not target:
                return f"No automation `{rid.group()}`. Existing: " + ", ".join(f"`{r['id']}`" for r in rules)
        elif every and noun:
            target = list(rules)
        else:
            pool = [r for r in rules if r["kind"] == "loop"] if noun and noun.group().startswith("loop") else rules
            if len(pool) != 1:
                return "Which one?\n" + "\n".join("- " + auto._describe(r) for r in (pool or rules)[:10])
            target = pool
        if toggle:
            outs = [await ai._dispatch("auto_toggle", {"rule_id": r["id"], "enabled": verb != "pause"}, ctx) for r in target]
            return "\n".join(ai._quick_result(o) for o in outs)
        arg = "all" if (len(target) == len(rules) and every and noun) else target[0]["id"]
        return ai._quick_result(await ai._dispatch("auto_remove", {"rule_id": arg}, ctx))

    # ------------------------------------------------------------------ health report
    @commands.command(name="aicheck")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def aicheck(self, ctx: commands.Context):
        """what is loaded / what is missing (administrator)"""
        import ai_admin_cog as m

        ai, auto, voice = self.bot.get_cog("AIAdmin"), self.bot.get_cog("Automation"), self.bot.get_cog("VoiceDJ")
        ok = lambda b: "OK" if b else "MISSING"  # noqa: E731
        lines = [
            f"ai_admin_cog: {ok(ai)} | auto_cog: {ok(auto)} | voice_cog: {ok(voice)}",
            f"voice tools for the AI: {ok('bot_follow' in m.TOOL_NAMES)} | automation tools: {ok('auto_add' in m.TOOL_NAMES)}",
            f"old bump alert off: {ok(ai is not None and not (isinstance(getattr(ai, 'bump', None), dict) and ai.bump.get('on')))}",
            f"ffmpeg: {ok(shutil.which('ffmpeg'))} | opus: {ok(discord.opus.is_loaded())}",
            f"edge-tts: {ok(importlib.util.find_spec('edge_tts'))} | yt-dlp: {ok(importlib.util.find_spec('yt_dlp'))}"
            f" | PyNaCl: {ok(importlib.util.find_spec('nacl'))}",
        ]
        lines.append(f"owner protection (!guard): {ok(self.bot.get_cog('OwnerGuard'))}")
        if auto is not None:
            import auto_cog

            b = auto.data["bump"]
            lines.append(
                f"bump alert (auto_cog): {'on' if b['on'] else 'off'}, ping at every bump {'yes' if b['now'] else 'no'}, "
                f"roles {len(b['roles'])}, channels {len(b['channels'])}, remind {b['remind']} min, rules {len(auto.data['rules'])}"
            )
            lines.append(f"automations are saved in: {auto_cog.DATA_FILE}"
                         + ("  (Railway volume)" if os.getenv("RAILWAY_VOLUME_MOUNT_PATH") else "  (NO volume - lost on redeploy)"))
        await ctx.reply("```\n" + "\n".join(lines) + "\n```", mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIFix(bot))
    if "owner_guard" not in bot.extensions and importlib.util.find_spec("owner_guard") is not None:
        await bot.load_extension("owner_guard")  # so ONE load line in your main file is enough
