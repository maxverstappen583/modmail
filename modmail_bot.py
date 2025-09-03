# modmail_bot.py
# Full modmail bot (drop-in). Read comments slowly and deploy.

# ---- small patch for Python 3.13 hosts that lack audioop (Render sometimes uses 3.13) ----
import sys
try:
    # audioop-lts provides a drop-in replacement for audioop on Python 3.13+
    import audioop_lts as _audioop_lts
    sys.modules["audioop"] = _audioop_lts
except Exception:
    # if not installed, continue — discord.py may still import audioop and fail,
    # so ensure audioop-lts is in requirements.txt on hosts that need it.
    pass

import os
import json
import asyncio
import aiohttp
from io import BytesIO
from datetime import datetime, timezone
from threading import Thread

import discord
from discord.ext import commands
from discord import ui
from flask import Flask

# Pillow (PIL) for color extraction
from PIL import Image

# optional dotenv
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# -----------------------------
# Environment / basic config
# -----------------------------
DISCORD_TOKEN = (
    os.getenv("DISCORD_TOKEN")
    or os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("TOKEN")
)
# replace default with your guild id if you want to hardcode
PRIMARY_GUILD_ID = int(os.getenv("PRIMARY_GUILD_ID", "1364371104755613837"))
OWNER_ID = int(os.getenv("OWNER_ID", "1319292111325106296"))
PORT = int(os.getenv("PORT", "10000"))

if not DISCORD_TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable not set.")
    raise SystemExit(1)

print("Using DISCORD_TOKEN from environment.")

# -----------------------------
# Flask keepalive for hosting
# -----------------------------
app = Flask("modmail_keepalive")

@app.route("/")
def home():
    return "Modmail bot running."

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

Thread(target=run_flask, daemon=True).start()

# -----------------------------
# Persistence
# -----------------------------
DATA_FILE = "modmail_data.json"
DEFAULT_DATA = {
    "category_id": None,
    "staff_role_id": None,
    "log_channel_id": None,
    "solve_keyword": "solved",
    "close_keyword": "close",
    "tickets": {}  # "user_id" -> channel_id
}

def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_DATA, f, indent=4)
        return DEFAULT_DATA.copy()
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)
    # ensure keys
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v
    return d

def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=4)

data = load_data()

# in-memory session logs for transcripts: channel_id -> list of entries
# entry: {"author_name","author_id","avatar_url","color","content","attachments":[{"filename","url"}],"ts"}
session_logs = {}

# -----------------------------
# helpers: time / http / color
# -----------------------------
def now_ts():
    # dd/mm/yy, 12-hour format with AM/PM
    return datetime.now(timezone.utc).strftime("%d/%m/%y, %I:%M %p")

async def fetch_bytes(url: str):
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception:
        return None
    return None

def dominant_color_from_bytes(b: bytes):
    try:
        img = Image.open(BytesIO(b)).convert("RGBA")
        img_small = img.resize((40, 40)).convert("RGB")
        colors = img_small.getcolors(40*40)
        if not colors:
            return (120,120,120)
        colors.sort(reverse=True, key=lambda x: x[0])
        for count, col in colors:
            r,g,b = col
            # skip very light (likely background)
            if not (r > 240 and g > 240 and b > 240):
                return col
        return colors[0][1]
    except Exception:
        return (120,120,120)

async def get_user_color(user: discord.User):
    # Try accent/banner color via fetch, else dominant color from avatar, else fallback
    try:
        full = await user.fetch()
        ac = getattr(full, "accent_color", None)
        if ac:
            try:
                # accent_color may already be discord.Color
                if isinstance(ac, discord.Color):
                    return ac
                # if int
                return discord.Color(ac)
            except Exception:
                pass
    except Exception:
        pass

    try:
        url = str(user.display_avatar.url)
        b = await fetch_bytes(url)
        if b:
            r,g,bcol = dominant_color_from_bytes(b)
            return discord.Color.from_rgb(r, g, bcol)
    except Exception:
        pass

    return discord.Color.dark_grey()

def color_to_hex(color):
    try:
        if isinstance(color, discord.Color):
            return "#{:06x}".format(color.value)
        if isinstance(color, tuple) and len(color) == 3:
            return "#{:02x}{:02x}{:02x}".format(*color)
        if isinstance(color, int):
            return "#{:06x}".format(color)
    except Exception:
        pass
    return "#777777"

def mention_to_id(s):
    if not s:
        return None
    s = s.strip()
    if s.startswith("<#") and s.endswith(">"):
        s = s[2:-1]
    if s.startswith("<@&") and s.endswith(">"):
        s = s[3:-1]
    try:
        return int(s)
    except Exception:
        return None

async def try_dm(user: discord.User, text: str):
    try:
        await user.send(text)
    except Exception:
        pass

# -----------------------------
# bot creation (must come before decorators)
# -----------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -----------------------------
# embed/card builder
# -----------------------------
def build_card(author_name: str, avatar_url: str, color: discord.Color, content: str, ts_str: str):
    embed = discord.Embed(description=content or " ", color=color)
    embed.set_author(name=author_name, icon_url=avatar_url)
    embed.set_footer(text=ts_str)
    return embed

# -----------------------------
# transcript HTML generator
# -----------------------------
def generate_html_transcript(channel: discord.TextChannel, entries: list):
    os.makedirs("transcripts", exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"transcripts/transcript_{channel.id}_{ts}.html"
    parts = []
    parts.append("<!doctype html><html><head><meta charset='utf-8'><title>Transcript</title>")
    parts.append("<style>")
    parts.append("""
        body{background:#0f1115;color:#e6eef6;font-family:Segoe UI, Roboto, Arial, sans-serif;padding:18px}
        .msg{border-radius:8px;padding:12px;margin:10px 0;display:flex;gap:12px;align-items:flex-start;background:#111214}
        .avatar{width:36px;height:36px;border-radius:50%}
        .content{flex:1}
        .meta{font-size:12px;color:#98a0aa;margin-bottom:6px}
        .text{white-space:pre-wrap}
        .att img{max-width:380px;border-radius:6px;margin-top:8px}
        .att a{color:#9bd;display:block;margin-top:6px}
    """)
    parts.append("</style></head><body>")
    parts.append(f"<h2>Transcript for #{channel.name} ({channel.id})</h2>")
    for e in entries:
        color_hex = e.get("color", "#777777")
        parts.append(f"<div class='msg' style='border-left:6px solid {color_hex};'>")
        parts.append(f"<img class='avatar' src='{e.get('avatar_url','')}' alt='avatar'/>")
        parts.append("<div class='content'>")
        parts.append(f"<div class='meta'><strong>{discord.utils.escape_markdown(e.get('author_name',''))}</strong> • {e.get('ts')}</div>")
        parts.append(f"<div class='text'>{discord.utils.escape_markdown(e.get('content',''))}</div>")
        atts = e.get("attachments", [])
        if atts:
            parts.append("<div class='att'>")
            for a in atts:
                url = a.get("url","")
                fn = a.get("filename","attachment")
                ext = fn.split(".")[-1].lower() if "." in fn else ""
                if ext in ("png","jpg","jpeg","gif","webp","bmp","svg"):
                    parts.append(f"<img src='{url}' alt='{fn}'/>")
                else:
                    parts.append(f"<a href='{url}' target='_blank'>{fn}</a>")
            parts.append("</div>")
        parts.append("</div></div>")
    parts.append("</body></html>")
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    return filename

# -----------------------------
# Logging helper: posts small events to log channel if set
# -----------------------------
async def log_event(guild: discord.Guild | None, title: str, description: str, channel_id: int | None = None):
    log_chan_id = data.get("log_channel_id")
    if not log_chan_id:
        return
    try:
        # prefer passing guild channel when possible
        log_chan = guild.get_channel(log_chan_id) if guild else bot.get_channel(log_chan_id)
        if not log_chan:
            log_chan = bot.get_channel(log_chan_id)
        if not log_chan:
            return
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        if channel_id:
            embed.add_field(name="Ticket Channel ID", value=str(channel_id), inline=False)
        await log_chan.send(embed=embed)
    except Exception:
        pass

# -----------------------------
# ticket creation helper (robust)
# -----------------------------
async def create_ticket_channel_for_user(guild: discord.Guild, user: discord.User):
    cat_id = data.get("category_id")
    category = guild.get_channel(cat_id) if cat_id else None
    if not category or not isinstance(category, discord.CategoryChannel):
        # no valid category configured → inform admins and user fallback
        try:
            await try_dm(user, "❌ Ticket category is not set or invalid. Ask an admin to run `!setup`.")
        except Exception:
            pass
        return None

    staff_role = guild.get_role(data.get("staff_role_id")) if data.get("staff_role_id") else None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    }

    # if the user is a member in the guild, allow them to see their own ticket
    member = guild.get_member(user.id)
    if member:
        overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    channel_name = f"ticket-{user.id}"
    try:
        ch = await category.create_text_channel(channel_name, overwrites=overwrites, reason="Modmail ticket created")
    except Exception:
        # fallback: try creating at guild root
        try:
            ch = await guild.create_text_channel(channel_name, overwrites=overwrites, reason="Modmail ticket created (fallback)")
        except Exception:
            return None

    data["tickets"][str(user.id)] = ch.id
    save_data(data)
    session_logs[ch.id] = []
    await log_event(guild, "Ticket Created", f"Ticket channel {ch.mention} created for {user} ({user.id}).", ch.id)
    return ch

# -----------------------------
# DM confirmation view (15s countdown with editing)
# -----------------------------
class DMConfirmView(ui.View):
    def __init__(self, user: discord.User, orig_message: discord.Message, timeout: int = 15):
        super().__init__(timeout=timeout)
        self.user = user
        self.orig_message = orig_message
        self._message = None
        self._task = None
        self._confirmed = False
        self._cancelled = False
        self._timeout = timeout

    async def start_countdown(self, sent_message: discord.Message):
        self._message = sent_message
        self._task = asyncio.create_task(self._countdown_loop())

    async def _countdown_loop(self):
        remaining = self._timeout
        try:
            while remaining > 0:
                if self._message is None:
                    break
                embed = discord.Embed(title="Confirm: Create Support Ticket?", description=self.orig_message.content or "[attachment]", color=discord.Color.gold())
                try:
                    embed.set_thumbnail(url=self.user.display_avatar.url)
                except Exception:
                    pass
                if self.orig_message.attachments:
                    embed.add_field(name="Attachments", value="\n".join([f"- {a.filename}" for a in self.orig_message.attachments]), inline=False)
                embed.set_footer(text=f"Press ✅ confirm or ❎ cancel. Auto-cancels in {remaining}s.")
                try:
                    await self._message.edit(embed=embed, view=self)
                except Exception:
                    pass
                await asyncio.sleep(1)
                remaining -= 1
                if self._confirmed or self._cancelled or self.is_finished():
                    return
            # timed out
            if not self._confirmed:
                self._cancelled = True
                try:
                    self.clear_items()
                    if self._message:
                        embed = discord.Embed(title="Timed out", description="Ticket not created — confirmation timed out.", color=discord.Color.dark_grey())
                        try:
                            embed.set_thumbnail(url=self.user.display_avatar.url)
                        except Exception:
                            pass
                        await self._message.edit(embed=embed, view=self)
                except Exception:
                    pass
                try:
                    await self.user.send("⏳ Confirmation timed out after 15 seconds. Ticket not created. Send your message again to try.")
                except Exception:
                    pass
                self.stop()
        except asyncio.CancelledError:
            return
        except Exception:
            return

    @ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("This confirmation is for the original user.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        self._confirmed = True
        if self._task and not self._task.done():
            self._task.cancel()

        guild = bot.get_guild(PRIMARY_GUILD_ID)
        if not guild:
            await interaction.followup.send("❌ Support unavailable right now.", ephemeral=True)
            self.stop()
            return

        ch = await create_ticket_channel_for_user(guild, self.user)
        if not ch:
            await interaction.followup.send("❌ Could not create ticket. Admins: set a valid category with `!setup`.", ephemeral=True)
            self.stop()
            return

        # prepare attachments and metadata
        files = []
        att_meta = []
        for att in self.orig_message.attachments:
            try:
                url = att.url
                f = await att.to_file()
                files.append(f)
                att_meta.append({"filename": f.filename, "url": url})
            except Exception:
                pass

        color = await get_user_color(self.user)
        card = build_card(str(self.user), str(self.user.display_avatar.url), color, self.orig_message.content or "[attachment]", now_ts())
        if att_meta:
            first = att_meta[0]["filename"]
            ext = first.split(".")[-1].lower() if "." in first else ""
            if ext in ("png","jpg","jpeg","gif","webp","bmp","svg"):
                card.set_image(url=f"attachment://{first}")

        try:
            if files:
                await ch.send(embed=card, files=files, view=TicketView(ticket_user_id=self.user.id))
            else:
                await ch.send(embed=card, view=TicketView(ticket_user_id=self.user.id))
        except Exception:
            pass

        # log the initial message to session_logs
        session_logs.setdefault(ch.id, []).append({
            "author_name": str(self.user),
            "author_id": self.user.id,
            "avatar_url": str(self.user.display_avatar.url),
            "color": color_to_hex(color),
            "content": self.orig_message.content or "",
            "attachments": att_meta,
            "ts": now_ts()
        })

        try:
            await interaction.followup.send(f"✅ Ticket created: {ch.mention}. Staff will respond there.", ephemeral=True)
        except Exception:
            pass

        self.clear_items()
        try:
            if self._message:
                await self._message.edit(embed=discord.Embed(title="Confirmed", description="Ticket created.", color=discord.Color.green()), view=self)
        except Exception:
            pass
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❎")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("This confirmation is for the original user.", ephemeral=True)
        self._cancelled = True
        if self._task and not self._task.done():
            self._task.cancel()
        try:
            self.clear_items()
            if self._message:
                await self._message.edit(embed=discord.Embed(title="Canceled", description="Ticket creation canceled.", color=discord.Color.dark_grey()), view=self)
        except Exception:
            pass
        try:
            await interaction.response.send_message("❎ Ticket creation canceled.", ephemeral=True)
        except Exception:
            pass
        self.stop()

# -----------------------------
# TicketView for staff buttons
# -----------------------------
class TicketView(ui.View):
    def __init__(self, ticket_user_id: int, timeout: int | None = None):
        super().__init__(timeout=timeout)
        self.ticket_user_id = ticket_user_id

    @ui.button(label="Mark Solved", style=discord.ButtonStyle.success, emoji="✅")
    async def mark_solved(self, interaction: discord.Interaction, button: ui.Button):
        if not (is_staff(interaction.user) or interaction.user.guild_permissions.administrator):
            return await interaction.response.send_message("❎ You are not staff.", ephemeral=True)
        embed = discord.Embed(title="✅ Problem Marked Solved", description=f"Marked solved by {interaction.user.mention}", color=discord.Color.green())
        embed.add_field(name="Time", value=now_ts())
        await interaction.channel.send(embed=embed)
        # DM user and log
        ch_id = interaction.channel.id
        uid = None
        for k,v in data.get("tickets", {}).items():
            if v == ch_id:
                uid = int(k); break
        if uid:
            try:
                u = await bot.fetch_user(uid)
                await u.send("✅ Your ticket has been marked solved by staff. Reply here to re-open.")
            except Exception:
                pass
        session_logs.setdefault(ch_id, []).append({
            "author_name": str(interaction.user),
            "author_id": interaction.user.id,
            "avatar_url": str(interaction.user.display_avatar.url),
            "color": color_to_hex(interaction.user.color),
            "content": "(marked solved)",
            "attachments": [],
            "ts": now_ts()
        })
        await interaction.response.send_message("✅ Marked solved.", ephemeral=True)

    @ui.button(label="Close Ticket (delete)", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if not (is_staff(interaction.user) or interaction.user.guild_permissions.administrator):
            return await interaction.response.send_message("❎ You are not staff.", ephemeral=True)
        await interaction.response.send_message("✅ Closing ticket — transcript will be created and channel deleted in 5s.", ephemeral=True)
        ch = interaction.channel
        ch_id = ch.id
        logs = session_logs.get(ch_id, [])
        try:
            path = generate_html_transcript(ch, logs)
            await log_event(ch.guild, "Ticket Closed", f"Ticket channel {ch.name} ({ch.id}) closed by {interaction.user}.", ch.id)
            # send transcript to configured log channel if set
            log_chan_id = data.get("log_channel_id")
            if log_chan_id:
                try:
                    log_chan = ch.guild.get_channel(log_chan_id) or bot.get_channel(log_chan_id)
                    if log_chan:
                        await log_chan.send(content=f"Transcript for {ch.name} ({ch.id}):", file=discord.File(path))
                except Exception:
                    pass
        except Exception:
            pass
        # remove mapping
        removed_uid = None
        for uid,cid in list(data.get("tickets", {}).items()):
            if cid == ch_id:
                removed_uid = uid; break
        if removed_uid:
            data["tickets"].pop(removed_uid, None)
            save_data(data)
        session_logs.pop(ch_id, None)
        await asyncio.sleep(5)
        try:
            await ch.delete(reason=f"Ticket closed by {interaction.user}")
        except Exception:
            pass

# -----------------------------
# Message handling: DMs and ticket channels
# -----------------------------
@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.author.bot:
        return

    # DMs
    if isinstance(message.channel, discord.DMChannel):
        user = message.author
        acct_days = (datetime.now(timezone.utc) - user.created_at).days
        if acct_days < 35:
            try:
                await user.send("❌ You cannot open a ticket — your Discord account is under 5 weeks old.")
            except Exception:
                pass
            return

        guild = bot.get_guild(PRIMARY_GUILD_ID)
        if not guild:
            await try_dm(user, "❌ Support unavailable right now. Try again later.")
            return

        uid = str(user.id)
        ch_id = data.get("tickets", {}).get(uid)
        channel = guild.get_channel(ch_id) if ch_id else None

        if channel:
            # forward to existing ticket
            color = await get_user_color(user)
            files = []
            att_meta = []
            for att in message.attachments:
                try:
                    url = att.url
                    f = await att.to_file()
                    files.append(f)
                    att_meta.append({"filename": f.filename, "url": url})
                except Exception:
                    pass
            card = build_card(str(user), str(user.display_avatar.url), color, message.content or "[attachment]", now_ts())
            if att_meta:
                first = att_meta[0]["filename"]
                ext = first.split(".")[-1].lower() if "." in first else ""
                if ext in ("png","jpg","jpeg","gif","webp","bmp","svg"):
                    card.set_image(url=f"attachment://{first}")
            try:
                if files:
                    await channel.send(embed=card, files=files)
                else:
                    await channel.send(embed=card)
            except Exception:
                pass
            session_logs.setdefault(channel.id, []).append({
                "author_name": str(user),
                "author_id": user.id,
                "avatar_url": str(user.display_avatar.url),
                "color": color_to_hex(color),
                "content": message.content or "",
                "attachments": att_meta,
                "ts": now_ts()
            })
            return

        # No ticket yet -> confirmation dialog with countdown
        confirm_embed = discord.Embed(title="Confirm: Create Support Ticket?", description=message.content or "[attachment]", color=discord.Color.gold())
        try:
            confirm_embed.set_thumbnail(url=user.display_avatar.url)
        except Exception:
            pass
        if message.attachments:
            confirm_embed.add_field(name="Attachments", value="\n".join([f"- {a.filename}" for a in message.attachments]), inline=False)
        confirm_embed.set_footer(text="Press ✅ confirm or ❎ cancel. This request times out in 15s.")
        preview_files = []
        for att in message.attachments:
            try:
                preview_files.append(await att.to_file())
            except Exception:
                pass
        view = DMConfirmView(user=user, orig_message=message, timeout=15)
        try:
            if preview_files:
                sent = await user.send(embed=confirm_embed, view=view, files=preview_files)
            else:
                sent = await user.send(embed=confirm_embed, view=view)
            await view.start_countdown(sent)
        except Exception:
            pass
        return

    # In-guild ticket channels
    if message.guild and message.guild.id == PRIMARY_GUILD_ID:
        ch_id = message.channel.id
        tickets_map = data.get("tickets", {})
        if ch_id in list(tickets_map.values()):
            member = message.author
            # staff-only forwarding
            if not (is_staff(member) or member.guild_permissions.administrator):
                return

            content = message.content or ""
            solve_kw = (data.get("solve_keyword") or "solved").lower().strip()
            close_kw = (data.get("close_keyword") or "close").lower().strip()
            lc = content.strip().lower()
            matched_solve = (lc == solve_kw) or lc.startswith(solve_kw)
            matched_close = (lc == close_kw) or lc.startswith(close_kw)

            if matched_solve:
                embed = discord.Embed(title="✅ Problem Marked Solved", description=f"Marked solved by {member.mention}", color=discord.Color.green())
                embed.add_field(name="Time", value=now_ts())
                await message.channel.send(embed=embed)
                try:
                    await message.delete()
                except Exception:
                    pass
                target_uid = None
                for uid,cid in tickets_map.items():
                    if cid == ch_id:
                        target_uid = int(uid); break
                if target_uid:
                    try:
                        u = await bot.fetch_user(target_uid)
                        await u.send("✅ Your ticket has been marked solved by staff. If it's still an issue, reply here to re-open.")
                    except Exception:
                        pass
                session_logs.setdefault(ch_id, []).append({
                    "author_name": str(member),
                    "author_id": member.id,
                    "avatar_url": str(member.display_avatar.url),
                    "color": color_to_hex(member.color),
                    "content": "(marked solved)",
                    "attachments": [],
                    "ts": now_ts()
                })
                return

            if matched_close:
                embed = discord.Embed(title="🗑️ Ticket Closed", description=f"Closed by {member.mention}", color=discord.Color.dark_gray())
                embed.add_field(name="Time", value=now_ts())
                await message.channel.send(embed=embed)
                logs = session_logs.get(ch_id, [])
                try:
                    path = generate_html_transcript(message.channel, logs)
                    # post transcript to configured log channel (if set)
                    log_chan_id = data.get("log_channel_id")
                    if log_chan_id:
                        try:
                            log_chan = message.guild.get_channel(log_chan_id) or bot.get_channel(log_chan_id)
                            if log_chan:
                                await log_chan.send(content=f"Transcript for {message.channel.name} ({ch_id}):", file=discord.File(path))
                        except Exception:
                            pass
                except Exception:
                    pass
                # remove mapping and delete
                removed_uid = None
                for uid,cid in list(tickets_map.items()):
                    if cid == ch_id:
                        removed_uid = uid; break
                if removed_uid:
                    data["tickets"].pop(removed_uid, None)
                    save_data(data)
                    session_logs.pop(ch_id, None)
                await asyncio.sleep(3)
                try:
                    await message.channel.delete(reason=f"Closed by keyword by {member}")
                except Exception:
                    pass
                await log_event(message.guild, "Ticket Closed", f"Ticket {message.channel.name} ({ch_id}) closed by {member}.", ch_id)
                return

            # normal staff message -> forward to user
            target_uid = None
            for uid,cid in tickets_map.items():
                if cid == ch_id:
                    target_uid = int(uid); break
            if target_uid:
                try:
                    u = await bot.fetch_user(target_uid)
                    files = []
                    att_meta = []
                    for att in message.attachments:
                        try:
                            url = att.url
                            f = await att.to_file()
                            files.append(f)
                            att_meta.append({"filename": f.filename, "url": url})
                        except Exception:
                            pass
                    staff_color = member.color or discord.Color.dark_grey()
                    card = build_card(str(member), str(member.display_avatar.url), staff_color, content or "[attachment]", now_ts())
                    if att_meta:
                        first = att_meta[0]["filename"]
                        ext = first.split(".")[-1].lower() if "." in first else ""
                        if ext in ("png","jpg","jpeg","gif","webp","bmp","svg"):
                            card.set_image(url=f"attachment://{first}")
                    if files:
                        await u.send(embed=card, files=files)
                    else:
                        await u.send(embed=card)
                    try:
                        await message.add_reaction("✅")
                    except Exception:
                        pass
                    session_logs.setdefault(ch_id, []).append({
                        "author_name": str(member),
                        "author_id": member.id,
                        "avatar_url": str(member.display_avatar.url),
                        "color": color_to_hex(member.color),
                        "content": content or "",
                        "attachments": att_meta,
                        "ts": now_ts()
                    })
                except Exception:
                    try:
                        await message.channel.send("❌ Could not DM the user (DMS may be closed).")
                    except Exception:
                        pass
                return

# -----------------------------
# is_staff helper
# -----------------------------
def is_staff(member: discord.Member):
    r_id = data.get("staff_role_id")
    if not r_id:
        return False
    return any(r.id == r_id for r in member.roles)

# -----------------------------
# Commands (prefix & slash)
# -----------------------------
@bot.tree.command(name="setup", description="Set ticket category and staff role (admin only).")
async def setup_slash(interaction: discord.Interaction, category: discord.CategoryChannel, staff_role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    data["category_id"] = category.id
    data["staff_role_id"] = staff_role.id
    save_data(data)
    await interaction.response.send_message(f"✅ Category set to **{category.name}** and staff role set to **{staff_role.name}** — {now_ts()}", ephemeral=True)
    await log_event(interaction.guild, "Settings Updated", f"Category set to {category.name} ({category.id}), staff role set to {staff_role.name} ({staff_role.id}).")

@bot.command(name="setup")
@commands.has_permissions(administrator=True)
async def setup_prefix(ctx: commands.Context, category_arg: str, staff_role_arg: str):
    cat_id = mention_to_id(category_arg) or None
    role_id = mention_to_id(staff_role_arg) or None
    if not cat_id or not role_id:
        return await ctx.send("❌ Invalid args. Use `!setup <category_id|#mention> <role_id|@mention>`")
    data["category_id"] = int(cat_id)
    data["staff_role_id"] = int(role_id)
    save_data(data)
    await ctx.send(f"✅ Category and staff role set. Category ID: `{cat_id}`, Role ID: `{role_id}`")
    await log_event(ctx.guild, "Settings Updated", f"Category set (ID {cat_id}), staff role set (ID {role_id}).")

@bot.tree.command(name="set_log_channel", description="Set the log channel where transcripts/events are posted (admin only).")
async def set_log_channel_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    data["log_channel_id"] = channel.id
    save_data(data)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)
    await log_event(interaction.guild, "Settings Updated", f"Log channel set to {channel.name} ({channel.id}).")

@bot.command(name="set_log_channel")
@commands.has_permissions(administrator=True)
async def set_log_channel_prefix(ctx: commands.Context, channel_arg: str):
    cid = mention_to_id(channel_arg) or None
    if not cid:
        return await ctx.send("❌ Invalid channel arg. Use `!set_log_channel #logs` or channel ID.")
    data["log_channel_id"] = int(cid)
    save_data(data)
    await ctx.send(f"✅ Log channel set. Channel ID: `{cid}`")
    await log_event(ctx.guild, "Settings Updated", f"Log channel set (ID {cid}).")

@bot.tree.command(name="settings", description="Show current modmail settings (admin only).")
async def settings_slash(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    guild = bot.get_guild(PRIMARY_GUILD_ID)
    category = guild.get_channel(data.get("category_id")) if data.get("category_id") else None
    staff_role = guild.get_role(data.get("staff_role_id")) if data.get("staff_role_id") else None
    log_chan = guild.get_channel(data.get("log_channel_id")) if data.get("log_channel_id") else None
    embed = discord.Embed(title="⚙️ Modmail Settings", color=discord.Color.blue())
    embed.add_field(name="Category", value=(f"{category.name} ({category.id})" if category else "❎ Not set"), inline=False)
    embed.add_field(name="Staff Role", value=(f"{staff_role.name} ({staff_role.id})" if staff_role else "❎ Not set"), inline=False)
    embed.add_field(name="Log Channel", value=(f"{log_chan.mention}" if log_chan else "❎ Not set"), inline=False)
    embed.add_field(name="Solve Keyword", value=f"`{data.get('solve_keyword')}`", inline=True)
    embed.add_field(name="Close Keyword", value=f"`{data.get('close_keyword')}`", inline=True)
    embed.add_field(name="Open Tickets", value=str(len(data.get("tickets", {}))), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.command(name="settings")
@commands.has_permissions(administrator=True)
async def settings_prefix(ctx: commands.Context):
    guild = bot.get_guild(PRIMARY_GUILD_ID)
    category = guild.get_channel(data.get("category_id")) if data.get("category_id") else None
    staff_role = guild.get_role(data.get("staff_role_id")) if data.get("staff_role_id") else None
    log_chan = guild.get_channel(data.get("log_channel_id")) if data.get("log_channel_id") else None
    embed = discord.Embed(title="⚙️ Modmail Settings", color=discord.Color.blue())
    embed.add_field(name="Category", value=(f"{category.name} ({category.id})" if category else "❎ Not set"), inline=False)
    embed.add_field(name="Staff Role", value=(f"{staff_role.name} ({staff_role.id})" if staff_role else "❎ Not set"), inline=False)
    embed.add_field(name="Log Channel", value=(f"{log_chan.mention}" if log_chan else "❎ Not set"), inline=False)
    embed.add_field(name="Solve Keyword", value=f"`{data.get('solve_keyword')}`", inline=True)
    embed.add_field(name="Close Keyword", value=f"`{data.get('close_keyword')}`", inline=True)
    embed.add_field(name="Open Tickets", value=str(len(data.get("tickets", {}))), inline=False)
    await ctx.send(embed=embed)

# set keywords
@bot.tree.command(name="set_solve_keyword", description="Set staff keyword to mark solved (admin only).")
async def set_solve_keyword_slash(interaction: discord.Interaction, keyword: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    data["solve_keyword"] = keyword.strip()
    save_data(data)
    await interaction.response.send_message(f"✅ Solve keyword set to `{keyword}` — {now_ts()}", ephemeral=True)

@bot.command(name="set_solve_keyword")
@commands.has_permissions(administrator=True)
async def set_solve_keyword_prefix(ctx: commands.Context, keyword: str):
    data["solve_keyword"] = keyword.strip()
    save_data(data)
    await ctx.send(f"✅ Solve keyword set to `{keyword}`")

@bot.tree.command(name="set_close_keyword", description="Set staff keyword to close ticket (admin only).")
async def set_close_keyword_slash(interaction: discord.Interaction, keyword: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    data["close_keyword"] = keyword.strip()
    save_data(data)
    await interaction.response.send_message(f"✅ Close keyword set to `{keyword}` — {now_ts()}", ephemeral=True)

@bot.command(name="set_close_keyword")
@commands.has_permissions(administrator=True)
async def set_close_keyword_prefix(ctx: commands.Context, keyword: str):
    data["close_keyword"] = keyword.strip()
    save_data(data)
    await ctx.send(f"✅ Close keyword set to `{keyword}`")

@bot.tree.command(name="list_tickets", description="List open tickets (admin only).")
async def list_tickets_slash(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    tickets = data.get("tickets", {})
    if not tickets:
        return await interaction.response.send_message("❎ No open tickets.", ephemeral=True)
    lines = [f"User `{uid}` → Channel ID `{cid}`" for uid,cid in tickets.items()]
    await interaction.response.send_message("Open tickets:\n" + "\n".join(lines), ephemeral=True)

@bot.command(name="list_tickets")
@commands.has_permissions(administrator=True)
async def list_tickets_prefix(ctx: commands.Context):
    tickets = data.get("tickets", {})
    if not tickets:
        return await ctx.send("❎ No open tickets.")
    lines = [f"User `{uid}` → Channel ID `{cid}`" for uid,cid in tickets.items()]
    await ctx.send("Open tickets:\n" + "\n".join(lines))

# force close
@bot.tree.command(name="force_close", description="Force close this ticket (staff/admin only).")
async def force_close_slash(interaction: discord.Interaction):
    ch = interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message("❎ Must be used in a ticket channel.", ephemeral=True)
    if not (is_staff(interaction.user) or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("❎ Staff only.", ephemeral=True)
    await interaction.response.send_message("✅ Force closing and deleting channel in 4s...", ephemeral=True)
    ch_id = ch.id
    logs = session_logs.get(ch_id, [])
    try:
        path = generate_html_transcript(ch, logs)
        log_chan_id = data.get("log_channel_id")
        if log_chan_id:
            try:
                log_chan = ch.guild.get_channel(log_chan_id) or bot.get_channel(log_chan_id)
                if log_chan:
                    await log_chan.send(content=f"Transcript for {ch.name} ({ch_id}):", file=discord.File(path))
            except Exception:
                pass
    except Exception:
        pass
    removed = None
    for uid,cid in list(data.get("tickets", {}).items()):
        if cid == ch_id:
            removed = uid; break
    if removed:
        data["tickets"].pop(removed, None)
        save_data(data)
    session_logs.pop(ch_id, None)
    await asyncio.sleep(4)
    try:
        await ch.delete(reason=f"Force-closed by {interaction.user}")
    except Exception:
        pass
    await log_event(ch.guild, "Ticket Force-Closed", f"{ch.name} ({ch.id}) force-closed by {interaction.user}.", ch_id)

@bot.command(name="force_close")
@commands.has_permissions(manage_channels=True)
async def force_close_prefix(ctx: commands.Context):
    ch = ctx.channel
    if not isinstance(ch, discord.TextChannel):
        return await ctx.send("❎ Must be used in a ticket channel.")
    if not (is_staff(ctx.author) or ctx.author.guild_permissions.administrator):
        return await ctx.send("❎ Staff only.")
    await ctx.send("✅ Force closing and deleting channel in 4s...")
    ch_id = ch.id
    logs = session_logs.get(ch_id, [])
    try:
        path = generate_html_transcript(ch, logs)
        log_chan_id = data.get("log_channel_id")
        if log_chan_id:
            try:
                log_chan = ctx.guild.get_channel(log_chan_id) or bot.get_channel(log_chan_id)
                if log_chan:
                    await log_chan.send(content=f"Transcript for {ch.name} ({ch_id}):", file=discord.File(path))
            except Exception:
                pass
    except Exception:
        pass
    removed = None
    for uid,cid in list(data.get("tickets", {}).items()):
        if cid == ch_id:
            removed = uid; break
    if removed:
        data["tickets"].pop(removed, None)
        save_data(data)
    session_logs.pop(ch_id, None)
    await asyncio.sleep(4)
    try:
        await ch.delete(reason=f"Force-closed by {ctx.author}")
    except Exception:
        pass
    await log_event(ctx.guild, "Ticket Force-Closed", f"{ch.name} ({ch.id}) force-closed by {ctx.author}.", ch_id)

# owner tools: refresh & restart
@bot.command(name="refresh")
async def refresh_prefix(ctx: commands.Context):
    if ctx.author.id != OWNER_ID:
        return await ctx.send("❎ Owner only.")
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=PRIMARY_GUILD_ID))
        msg = f"✅ Refreshed {len(synced)} commands for guild {PRIMARY_GUILD_ID}"
        await ctx.send(msg)
    except Exception as e:
        await ctx.send(f"❌ Refresh failed: {e}")

@bot.tree.command(name="refresh", description="Refresh/sync slash commands for primary guild (owner only).")
async def refresh_slash(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❎ Owner only.", ephemeral=True)
    await interaction.response.send_message("🔁 Refreshing commands for primary guild...", ephemeral=True)
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=PRIMARY_GUILD_ID))
        await interaction.followup.send(f"✅ Synced {len(synced)} commands to primary guild — {now_ts()}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Sync error: {e}", ephemeral=True)

@bot.command(name="restart")
async def restart_prefix(ctx: commands.Context):
    if ctx.author.id != OWNER_ID:
        return await ctx.send("❎ Owner only.")
    await ctx.send("🔄 Restarting bot now...")
    await asyncio.sleep(1)
    try:
        await bot.close()
    except Exception:
        pass
    try:
        python = sys.executable
        os.execv(python, [python] + sys.argv)
    except Exception as e:
        await ctx.send(f"❌ Restart failed: {e}. Exiting instead.")
        sys.exit(0)

@bot.tree.command(name="restart", description="Restart the bot (owner only).")
async def restart_slash(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❎ Owner only.", ephemeral=True)
    await interaction.response.send_message("🔄 Restarting bot now...", ephemeral=True)
    await asyncio.sleep(1)
    try:
        await bot.close()
    except Exception:
        pass
    try:
        python = sys.executable
        os.execv(python, [python] + sys.argv)
    except Exception as e:
        await interaction.followup.send(f"❌ Restart failed: {e}. Exiting instead.", ephemeral=True)
        sys.exit(0)

# commands list/help
@bot.command(name="commands")
async def commands_list_prefix(ctx: commands.Context):
    embed = discord.Embed(title="📚 Modmail Commands (prefix & slash)", color=discord.Color.blurple())
    embed.add_field(name="Setup", value="`!setup <category_id|#mention> <role_id|@mention>`\n`/setup category:<category> staff_role:<role>`", inline=False)
    embed.add_field(name="Set Log Channel", value="`!set_log_channel <#channel|id>`\n`/set_log_channel channel:<channel>`", inline=False)
    embed.add_field(name="Settings", value="`!settings` / `/settings`", inline=False)
    embed.add_field(name="Ticket Flow", value="Users DM bot → confirm 15s → ticket created in category. Staff replies forward to user. Attachments forwarded.", inline=False)
    embed.add_field(name="Owner Tools", value="`!refresh` `!restart`", inline=False)
    await ctx.send(embed=embed)

@bot.tree.command(name="commands", description="Show modmail commands and usage (prefix & slash).")
async def commands_list_slash(interaction: discord.Interaction):
    embed = discord.Embed(title="📚 Modmail Commands (prefix & slash)", color=discord.Color.blurple())
    embed.add_field(name="Setup", value="`!setup <category_id|#mention> <role_id|@mention>`\n`/setup category:<category> staff_role:<role>`", inline=False)
    embed.add_field(name="Set Log Channel", value="`!set_log_channel <#channel|id>`\n`/set_log_channel channel:<channel>`", inline=False)
    embed.add_field(name="Settings", value="`!settings` / `/settings`", inline=False)
    embed.add_field(name="Ticket Flow", value="Users DM bot → confirm 15s → ticket created in category. Staff replies forward to user. Attachments forwarded.", inline=False)
    embed.add_field(name="Owner Tools", value="`!refresh` `!restart`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -----------------------------
# on_ready: sync & owner DM
# -----------------------------
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=PRIMARY_GUILD_ID))
        print(f"✅ Synced {len(synced)} commands to guild {PRIMARY_GUILD_ID}")
    except Exception as e:
        print(f"⚠️ Guild sync failed: {e}. Trying global sync.")
        try:
            all_synced = await bot.tree.sync()
            print(f"✅ Globally synced {len(all_synced)} commands")
        except Exception as e2:
            print(f"❌ Global sync failed: {e2}")
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        owner = await bot.fetch_user(OWNER_ID)
        host = os.getenv("HOSTNAME", "unknown-host")
        await owner.send(f"✅ Modmail bot logged in as **{bot.user}** on `{host}` — {now_ts()}")
    except Exception:
        pass

# -----------------------------
# Run the bot
# -----------------------------
if __name__ == "__main__":
    print("Starting modmail bot...")
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print(f"Bot failed to start: {e}")
        raise
