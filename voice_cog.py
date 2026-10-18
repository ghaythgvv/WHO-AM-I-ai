"""
voice_cog.py  -  the bot's own VOICE: follow me / come / speak / play music        (works together with ai_admin_cog)

It has NO commands of its own (so it can't clash with your other music bots). ai_admin_cog controls it, in any language:

    follow me            the bot joins you and moves with you from voice channel to voice channel
    unfollow             stop following
    come / join          join the voice channel you are in
    leave                disconnect
    speak hello everyone / say hello out loud / say hi in vc      text-to-speech in the voice channel
    play <song or link>  / put music <song>      queue + play (also: skip, pause, resume, stop, volume 60, loop, queue, now playing)

Only the server owner and members whose top role is ABOVE the bot's role can use it (same rule as ai_admin_cog).

SETUP
  requirements.txt:   discord.py[voice]   edge-tts   yt-dlp   openai
                      ("discord.py[voice]" installs PyNaCl + davey, which Discord's voice encryption needs)
  The server needs ffmpeg and the opus library. On Railway add this SERVICE VARIABLE (Railpack builder):
                      RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg libopus0
  Intents: voice_states must be ON (it is with discord.Intents.default()).
  Bot permissions in the voice channels: View Channel, Connect, Speak.
  Load it:  await bot.load_extension("voice_cog")      (next to ai_admin_cog)

OPTIONAL RAILWAY VARIABLES
  MUSIC_SOURCE            ytsearch (default, YouTube) or scsearch (SoundCloud). YouTube often blocks cloud servers
                          ("confirm you're not a bot"); if that happens set MUSIC_SOURCE=scsearch
  YTDLP_COOKIES           path of a cookies.txt file for yt-dlp (helps with YouTube blocks)
  VOICE_DEFAULT_VOLUME    music volume in percent, default 50 (max 150)
  TTS_VOICE_EN / TTS_VOICE_AR / TTS_VOICE_FR     edge-tts voice names (defaults: en-US-AriaNeural, ar-TN-ReemNeural,
                          fr-FR-DeniseNeural)
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from collections import deque

import discord
from discord.ext import commands

try:
    import edge_tts  # pip install edge-tts
except ImportError:
    edge_tts = None
try:
    import yt_dlp  # pip install yt-dlp
except ImportError:
    yt_dlp = None

log = logging.getLogger("voice_dj")


def _int_env(name, default, lo, hi):
    v = os.getenv(name, "").strip()
    return max(lo, min(hi, int(v))) if v.isdigit() else default


MUSIC_SOURCE = os.getenv("MUSIC_SOURCE", "ytsearch").strip().lower()
if MUSIC_SOURCE not in ("ytsearch", "scsearch"):
    MUSIC_SOURCE = "ytsearch"
COOKIES = os.getenv("YTDLP_COOKIES", "").strip()
DEFAULT_VOLUME = _int_env("VOICE_DEFAULT_VOLUME", 50, 1, 150)
TTS_VOICES = {
    "en": os.getenv("TTS_VOICE_EN", "en-US-AriaNeural"),
    "en_male": "en-US-GuyNeural",
    "ar": os.getenv("TTS_VOICE_AR", "ar-TN-ReemNeural"),
    "ar_male": "ar-TN-HediNeural",
    "fr": os.getenv("TTS_VOICE_FR", "fr-FR-DeniseNeural"),
    "fr_male": "fr-FR-HenriNeural",
}
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
MAX_QUEUE = 50
MAX_TTS_CHARS = 400
ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
URL_RE = re.compile(r"^https?://", re.I)
NOT_ALLOWED = "Only the owner and roles above mine can control my voice."


class VoiceError(Exception):
    pass


class GState:
    """everything the bot remembers about voice in ONE server"""

    def __init__(self):
        self.queue = deque()  # items: {"kind": "music"|"tts", ...}
        self.wake = asyncio.Event()
        self.task = None
        self.current = None  # the item playing now
        self.volume = DEFAULT_VOLUME / 100
        self.loop = False
        self.follow_id = None  # member id the bot follows
        self.skipped = False  # the current item was skipped / interrupted: don't re-add it for loop
        self.text_channel_id = None  # where to report problems


def _dur(sec):
    if not sec:
        return ""
    sec = int(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class VoiceDJ(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states = {}  # guild id -> GState

    async def cog_load(self):
        if not discord.opus.is_loaded():
            for name in ("libopus.so.0", "libopus.so", "opus"):
                try:
                    discord.opus.load_opus(name)
                    break
                except Exception:
                    continue
        if not discord.opus.is_loaded():
            log.warning("voice: the opus library isn't loaded - install it (Railway: RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg libopus0)")
        if not shutil.which("ffmpeg"):
            log.warning("voice: ffmpeg not found - install it (Railway: RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg libopus0)")

    async def cog_unload(self):
        for g in list(self.bot.guilds):
            vc = g.voice_client
            if vc is not None:
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
        for st in self.states.values():
            if st.task and not st.task.done():
                st.task.cancel()
            self._drop_queue(st)

    # ------------------------------------------------------------ helpers
    def st(self, gid) -> GState:
        if gid not in self.states:
            self.states[gid] = GState()
        return self.states[gid]

    def can_control(self, member) -> bool:
        """same rule as ai_admin_cog: owner, or a top role ABOVE the bot's top role"""
        admin = self.bot.get_cog("AIAdmin")
        if admin is not None:
            return admin._authorized(member)
        g = member.guild
        return isinstance(member, discord.Member) and (member.id == g.owner_id or member.top_role > g.me.top_role)

    @staticmethod
    def _cleanup(item):
        if item and item.get("kind") == "tts":
            try:
                os.remove(item["path"])
            except OSError:
                pass

    def _drop_queue(self, st):
        for it in st.queue:
            self._cleanup(it)
        st.queue.clear()

    @staticmethod
    def _nice_err(e):
        t = str(e)
        low = t.lower()
        if "confirm you" in low and "bot" in low or "sign in to confirm" in low:
            return ("YouTube is blocking this server (bot check). Set `MUSIC_SOURCE=scsearch` in Railway to use SoundCloud, "
                    "or give yt-dlp a cookies file with `YTDLP_COOKIES`.")
        if isinstance(e, asyncio.TimeoutError):
            return "That took too long - try again or use another song."
        return (t.replace("ERROR: ", "")[:200] or type(e).__name__)

    async def _say_in_text(self, guild, st, msg):
        ch = guild.get_channel(st.text_channel_id) if st.text_channel_id else None
        if ch is not None:
            try:
                await ch.send(msg[:1900], allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    async def _connect(self, guild, channel):
        perms = channel.permissions_for(guild.me)
        if not perms.connect:
            raise VoiceError(f"I don't have permission to join **{channel.name}** (give the bot Connect there).")
        vc = guild.voice_client
        if vc is not None and vc.is_connected():
            if vc.channel.id != channel.id:
                await vc.move_to(channel)
            return vc
        if vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
        return await channel.connect(self_deaf=True, timeout=20.0, reconnect=True)

    async def _ensure_connected(self, ctx):
        """(voice client, None) or (None, error dict). Joins the staff member's channel if the bot isn't in voice."""
        g = ctx["guild"]
        vc = g.voice_client
        if vc is not None and vc.is_connected():
            return vc, None
        who = ctx["author"]
        if not isinstance(who, discord.Member) or not who.voice or not who.voice.channel:
            return None, {"error": "Join a voice channel first (or tell me which one) - I'm not in voice right now."}
        try:
            return await self._connect(g, who.voice.channel), None
        except VoiceError as e:
            return None, {"error": str(e)}
        except Exception as e:
            log.exception("voice connect failed")
            return None, {"error": f"Couldn't join the voice channel: {type(e).__name__}: {e}"}

    def _ensure_task(self, guild):
        st = self.st(guild.id)
        if st.task is None or st.task.done():
            st.task = asyncio.create_task(self._player(guild))

    # ------------------------------------------------------------- player
    async def _player(self, guild):
        st = self.st(guild.id)
        loop = asyncio.get_running_loop()
        while True:
            if not st.queue:
                st.current = None
                st.wake.clear()
                await st.wake.wait()
                continue
            vc = guild.voice_client
            if vc is None or not vc.is_connected():
                self._drop_queue(st)  # nobody to play to
                continue
            item = st.queue.popleft()
            st.current, st.skipped = item, False
            try:
                if item["kind"] == "tts":
                    src = discord.FFmpegPCMAudio(item["path"])
                else:
                    url = await self._stream_url(item)
                    src = discord.PCMVolumeTransformer(
                        discord.FFmpegPCMAudio(url, before_options=FFMPEG_BEFORE, options="-vn"), volume=st.volume
                    )
            except Exception as e:
                log.warning("voice: couldn't open %s: %s", item.get("title"), e)
                await self._say_in_text(guild, st, f"Couldn't play **{item.get('title', 'that')}**: {self._nice_err(e)}")
                self._cleanup(item)
                st.current = None
                continue
            done = loop.create_future()

            def after(err, fut=done):
                loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(err))

            try:
                vc.play(src, after=after)
            except Exception as e:
                log.warning("voice: play failed: %s", e)
                await self._say_in_text(guild, st, f"Couldn't start playing: {self._nice_err(e)}")
                self._cleanup(item)
                st.current = None
                continue
            err = await done
            if err:
                log.warning("voice: player error: %s", err)
            self._cleanup(item)
            if item["kind"] == "music" and st.loop and not st.skipped:
                st.queue.append(item)
            st.current = None

    # ------------------------------------------------------------ yt-dlp
    def _extract(self, target):
        opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 15,
            "default_search": MUSIC_SOURCE,
        }
        if COOKIES and os.path.exists(COOKIES):
            opts["cookiefile"] = COOKIES
        with yt_dlp.YoutubeDL(opts) as y:
            return y.extract_info(target, download=False)

    async def _run(self, fn, arg):
        return await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, fn, arg), timeout=45)

    @staticmethod
    def _first(info):
        if info and info.get("entries"):
            return next((e for e in info["entries"] if e), None)
        return info

    async def _resolve(self, q):
        target = q if URL_RE.match(q) else f"{MUSIC_SOURCE}1:{q}"
        info = self._first(await self._run(self._extract, target))
        if not info:
            raise RuntimeError("Nothing found for that.")
        return {
            "kind": "music",
            "title": info.get("title") or "Unknown",
            "page_url": info.get("webpage_url") or info.get("original_url") or (q if URL_RE.match(q) else None),
            "duration": info.get("duration"),
            "stream": info.get("url"),
            "at": time.time(),
        }

    async def _stream_url(self, item):
        if item.get("stream") and time.time() - item.get("at", 0) < 900:  # stream links expire - refresh after 15 min
            return item["stream"]
        if not item.get("page_url"):
            raise RuntimeError("Couldn't get the audio stream.")
        info = self._first(await self._run(self._extract, item["page_url"]))
        if not info or not info.get("url"):
            raise RuntimeError("Couldn't get the audio stream.")
        item["stream"], item["at"] = info["url"], time.time()
        return item["stream"]

    # --------------------------------------------------------------- follow
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        g = member.guild
        st = self.states.get(g.id)
        if st is None:
            return
        if member.id == self.bot.user.id:
            if after.channel is None and before.channel is not None:  # the bot was disconnected / kicked
                self._drop_queue(st)
            return
        if st.follow_id != member.id or after.channel is None or after.channel == before.channel:
            return
        try:
            await self._connect(g, after.channel)  # the followed person moved or joined: go there too
        except Exception as e:
            log.warning("voice follow failed: %s", e)

    # ------------------------------------------------------------ the API
    # (called by the tools of ai_admin_cog; every method returns {"ok": True, "summary": ...} or {"error": ...})
    async def api_join(self, ctx, channel=None, member=None):
        if not self.can_control(ctx["author"]):
            return {"error": NOT_ALLOWED}
        g = ctx["guild"]
        target = channel
        if target is None:
            who = member or ctx["author"]
            if not isinstance(who, discord.Member) or not who.voice or not who.voice.channel:
                return {"error": f"{getattr(who, 'display_name', 'You')} isn't in a voice channel - join one first, or tell me which channel."}
            target = who.voice.channel
        if not isinstance(target, (discord.VoiceChannel, discord.StageChannel)):
            return {"error": "That's not a voice channel."}
        try:
            await self._connect(g, target)
        except VoiceError as e:
            return {"error": str(e)}
        except Exception as e:
            log.exception("voice join failed")
            return {"error": f"Couldn't join voice: {type(e).__name__}: {e}"}
        return {"ok": True, "summary": f"Joined **{target.name}**."}

    async def api_follow(self, ctx, member=None):
        if not self.can_control(ctx["author"]):
            return {"error": NOT_ALLOWED}
        g = ctx["guild"]
        who = member or ctx["author"]
        if not isinstance(who, discord.Member):
            return {"error": "That member isn't in the server."}
        st = self.st(g.id)
        st.follow_id = who.id
        st.text_channel_id = ctx["channel"].id
        note = " Say `unfollow` to stop."
        if who.voice and who.voice.channel:
            try:
                await self._connect(g, who.voice.channel)
            except VoiceError as e:
                return {"error": str(e)}
            except Exception as e:
                log.exception("voice follow join failed")
                return {"error": f"Couldn't join voice: {type(e).__name__}: {e}"}
            return {"ok": True, "summary": f"Following **{who.display_name}** - I'm in **{who.voice.channel.name}** and I'll move when they do.{note}"}
        return {"ok": True, "summary": f"Following **{who.display_name}** - I'll join as soon as they are in a voice channel.{note}"}

    async def api_unfollow(self, ctx):
        if not self.can_control(ctx["author"]):
            return {"error": NOT_ALLOWED}
        st = self.st(ctx["guild"].id)
        if st.follow_id is None:
            return {"ok": True, "summary": "I wasn't following anyone."}
        st.follow_id = None
        return {"ok": True, "summary": "Okay, I stopped following."}

    async def api_leave(self, ctx):
        if not self.can_control(ctx["author"]):
            return {"error": NOT_ALLOWED}
        g = ctx["guild"]
        st = self.st(g.id)
        st.follow_id, st.loop = None, False
        self._drop_queue(st)
        vc = g.voice_client
        if vc is None:
            return {"ok": True, "summary": "I'm not in a voice channel."}
        name = getattr(vc.channel, "name", "voice")
        try:
            vc.stop()
            await vc.disconnect(force=True)
        except Exception as e:
            return {"error": f"Couldn't leave: {e}"}
        return {"ok": True, "summary": f"Left **{name}**."}

    async def api_status(self, ctx):
        g = ctx["guild"]
        st = self.st(g.id)
        vc = g.voice_client
        if vc is None or not vc.is_connected():
            return {"ok": True, "summary": "I'm not in a voice channel."}
        parts = [f"In **{vc.channel.name}**"]
        if st.follow_id:
            m = g.get_member(st.follow_id)
            parts.append(f"following **{m.display_name if m else st.follow_id}**")
        if st.current:
            parts.append(f"playing **{st.current.get('title', 'audio')}**")
        if st.queue:
            parts.append(f"{len(st.queue)} in the queue")
        parts.append(f"volume {int(st.volume * 100)}%")
        if st.loop:
            parts.append("loop on")
        return {"ok": True, "summary": ", ".join(parts) + "."}

    async def api_say(self, ctx, text, lang=None, voice=None):
        if not self.can_control(ctx["author"]):
            return {"error": NOT_ALLOWED}
        if edge_tts is None:
            return {"error": "Text-to-speech isn't installed - add `edge-tts` to requirements.txt."}
        if not shutil.which("ffmpeg"):
            return {"error": "ffmpeg isn't installed on the server - set RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg libopus0 in Railway."}
        text = (text or "").strip()[:MAX_TTS_CHARS]
        if not text:
            return {"error": "Tell me what to say."}
        g = ctx["guild"]
        vc, err = await self._ensure_connected(ctx)
        if err:
            return err
        st = self.st(g.id)
        st.text_channel_id = ctx["channel"].id
        lang = (lang or "").lower()[:2]
        if lang not in ("en", "ar", "fr"):
            lang = "ar" if ARABIC_RE.search(text) else "en"
        key = f"{lang}_male" if str(voice or "").lower().startswith("m") and f"{lang}_male" in TTS_VOICES else lang
        path = os.path.join(tempfile.gettempdir(), f"tts_{g.id}_{int(time.time() * 1000)}.mp3")
        try:
            await asyncio.wait_for(edge_tts.Communicate(text, TTS_VOICES[key]).save(path), timeout=30)
        except Exception as e:
            log.warning("tts failed: %s", e)
            return {"error": f"Text-to-speech failed: {type(e).__name__}: {e}"}
        item = {"kind": "tts", "path": path, "text": text, "title": "speech"}
        music_now = st.current is not None and st.current["kind"] == "music" and (vc.is_playing() or vc.is_paused())
        if music_now:  # speech cuts in; the song starts again right after it
            st.queue.appendleft(dict(st.current))
            st.queue.appendleft(item)
            st.skipped = True
            vc.stop()
        else:
            idx = next((i for i, x in enumerate(st.queue) if x["kind"] == "music"), len(st.queue))
            st.queue.insert(idx, item)  # after other speech, before the songs
        self._ensure_task(g)
        st.wake.set()
        return {"ok": True, "summary": f"Saying it out loud in **{vc.channel.name}**."}

    async def api_music(self, ctx, action, query=None, volume=None, enabled=None):
        if not self.can_control(ctx["author"]):
            return {"error": NOT_ALLOWED}
        g = ctx["guild"]
        st = self.st(g.id)
        vc = g.voice_client
        connected = vc is not None and vc.is_connected()
        action = (action or "").lower()
        if action == "play":
            q = (query or "").strip()
            if not q:
                return {"error": "Tell me what to play (a song name or a link)."}
            if yt_dlp is None:
                return {"error": "Music isn't installed - add `yt-dlp` to requirements.txt."}
            if not shutil.which("ffmpeg"):
                return {"error": "ffmpeg isn't installed on the server - set RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg libopus0 in Railway."}
            if len(st.queue) >= MAX_QUEUE:
                return {"error": f"The queue is full ({MAX_QUEUE})."}
            try:
                track = await self._resolve(q)
            except Exception as e:
                log.warning("music resolve failed: %s", e)
                return {"error": self._nice_err(e)}
            vc, err = await self._ensure_connected(ctx)
            if err:
                return err
            track["by"] = getattr(ctx["author"], "display_name", "?")
            busy = st.current is not None or bool(st.queue)
            st.queue.append(track)
            st.text_channel_id = ctx["channel"].id
            self._ensure_task(g)
            st.wake.set()
            d = f" ({_dur(track['duration'])})" if track.get("duration") else ""
            if busy:
                return {"ok": True, "summary": f"Added **{track['title']}**{d} to the queue (#{len(st.queue)})."}
            return {"ok": True, "summary": f"Playing **{track['title']}**{d} in **{vc.channel.name}**."}
        if action == "queue":
            if not st.current and not st.queue:
                return {"ok": True, "summary": "The queue is empty."}
            lines = []
            if st.current and st.current["kind"] == "music":
                lines.append(f"Now: **{st.current['title']}**")
            for i, it in enumerate([x for x in st.queue if x["kind"] == "music"][:10], 1):
                lines.append(f"{i}. {it['title']}" + (f" ({_dur(it['duration'])})" if it.get("duration") else ""))
            return {"ok": True, "summary": "\n".join(lines) or "The queue is empty."}
        if action == "nowplaying":
            cur = st.current
            if not cur or cur["kind"] != "music":
                return {"ok": True, "summary": "Nothing is playing."}
            d = f" ({_dur(cur['duration'])})" if cur.get("duration") else ""
            return {"ok": True, "summary": f"Now playing **{cur['title']}**{d} - asked by {cur.get('by', '?')}."}
        if not connected:
            return {"error": "I'm not in a voice channel."}
        if action == "skip":
            if not (vc.is_playing() or vc.is_paused()):
                return {"ok": True, "summary": "Nothing is playing."}
            st.skipped = True
            vc.stop()
            return {"ok": True, "summary": "Skipped."}
        if action == "pause":
            if vc.is_playing():
                vc.pause()
                return {"ok": True, "summary": "Paused."}
            return {"ok": True, "summary": "Nothing is playing."}
        if action == "resume":
            if vc.is_paused():
                vc.resume()
                return {"ok": True, "summary": "Resumed."}
            return {"ok": True, "summary": "Nothing is paused."}
        if action == "stop":
            st.loop = False
            for it in [x for x in st.queue if x["kind"] == "music"]:
                st.queue.remove(it)
            if st.current is not None and (vc.is_playing() or vc.is_paused()):
                st.skipped = True
                vc.stop()
            return {"ok": True, "summary": "Stopped the music and cleared the queue."}
        if action == "volume":
            if volume is None:
                return {"ok": True, "summary": f"Volume is **{int(st.volume * 100)}%**."}
            v = max(1, min(int(volume), 150))
            st.volume = v / 100
            src = vc.source
            if isinstance(src, discord.PCMVolumeTransformer):
                src.volume = st.volume
            return {"ok": True, "summary": f"Volume set to **{v}%**."}
        if action == "loop":
            st.loop = (not st.loop) if enabled is None else bool(enabled)
            return {"ok": True, "summary": f"Loop is **{'on' if st.loop else 'off'}**."}
        return {"error": f"Unknown music action '{action}'."}


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceDJ(bot))
