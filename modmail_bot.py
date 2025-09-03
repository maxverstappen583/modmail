# modmail_bot.py
# Full-featured modmail with 15s confirmation, embed styling, dominant color extraction,
# attachment forwarding, HTML transcript generation, and both prefix & slash commands.

import os
import sys
import json
import asyncio
import discord
import aiohttp
from discord.ext import commands
from discord import ui
from flask import Flask
from threading import Thread
from datetime import datetime, timezone
from io import BytesIO

# Pillow for image processing (dominant color)
from PIL import Image

# Optional OpenAI support (for AI draft button - optional)
try:
    import openai
except Exception:
    openai = None

# dotenv (optional)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# -------------------------
# Environment
# -------------------------
TOKEN = (
    os.getenv("DISCORD_TOKEN")
    or os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("TOKEN")
)
PRIMARY_GUILD_ID = int(os.getenv("PRIMARY_GUILD_ID", "1364371104755613837"))
OWNER_ID = int(os.getenv("OWNER_ID", "1319292111325106296"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", "10000"))

if not TOKEN:
    print("❌ ERROR: No Discord token found. Set DISCORD_TOKEN (or DISCORD_BOT_TOKEN)")
    sys.exit(1)

if OPENAI_API_KEY and openai:
    openai.api_key = OPENAI_API_KEY

# Helpful log
if os.getenv("DISCORD_TOKEN"):
    print("Using DISCORD_TOKEN from environment.")
elif os.getenv("DISCORD_BOT_TOKEN"):
    print("Using DISCORD_BOT_TOKEN from environment.")
elif os.getenv("TOKEN"):
    print("Using TOKEN from environment.")

# -------------------------
# Flask keepalive
# -------------------------
app = Flask("modmail_keepalive")

@app.route("/")
def home():
    return "Modmail bot running."

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

Thread(target=run_flask, daemon=True).start()

# -------------------------
# Persistence
# -------------------------
DATA_FILE = "modmail_data.json"
DEFAULT_DATA = {
    "category_id": None,
    "staff_role_id": None,
    "solve_keyword": "solved",
    "close_keyword": "close",
    "tickets": {}  # user_id (str) -> channel_id (int)
}

def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w") as f:
            json.dump(DEFAULT_DATA, f, indent=4)
        return DEFAULT_DATA.copy()
    with open(DATA_FILE, "r") as f:
        d = json.load(f)
    # ensure defaults
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v
    return d

def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=4)

data = load_data()

# session logs in-memory for transcripts: channel_id -> list of entries
# each entry: {author_name, author_id, avatar_url, color_hex, content, attachments: [{filename,url}], ts}
session_logs = {}

# -------------------------
# Helpers: time, colors, images
# -------------------------
def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

async def fetch_bytes(url: str) -> bytes | None:
    """Fetch raw bytes from url using aiohttp (returns None on error)."""
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception:
        return None
    return None

def dominant_color_from_bytes(b: bytes):
    """Return (r,g,b) dominant color using Pillow."""
    try:
        img = Image.open(BytesIO(b)).convert("RGBA")
        # remove fully transparent pixels
        pixels = [px for px in img.getdata() if px[3] > 0]
        if not pixels:
            return (100, 100, 100)
        # resize for speed
        img_small = img.resize((40, 40))
        img_small = img_small.convert("RGB")
        result = img_small.getcolors(40*40)  # list of (count, (r,g,b))
        if not result:
            return (100, 100, 100)
        # sort by count
        result.sort(reverse=True, key=lambda x: x[0])
        # pick first non-white-ish color
        for count, col in result:
            r,g,b = col
            # skip near-white background
            if r > 240 and g > 240 and b > 240:
                continue
            return (r,g,b)
        # fallback to most common even if whiteish
        return result[0][1]
    except Exception:
        return (100, 100, 100)

async def get_user_embed_color(user: discord.User):
    """Try accent/banner color, else compute dominant color from avatar."""
    # Try to fetch full user (may provide accent_color)
    try:
        full = await user.fetch()
        ac = getattr(full, "accent_color", None)
        if ac:
            # discord.Colour or similar - try to return discord.Color instance
            try:
                return ac
            except Exception:
                try:
                    # if ac is int
                    return discord.Color(ac)
                except Exception:
                    pass
    except Exception:
        pass

    # fallback: use dominant color from avatar bytes
    url = user.display_avatar.url
    b = await fetch_bytes(url)
    if b:
        r,g,bcol = dominant_color_from_bytes(b)
        try:
            return discord.Color.from_rgb(r, g, bcol)
        except Exception:
            return discord.Color.default()
    return discord.Color.default()

def color_to_hex(color):
    """Take a discord.Color or (r,g,b) and return hex string '#rrggbb'."""
    try:
        if isinstance(color, discord.Color):
            value = color.value
            return "#{:06x}".format(value)
        if isinstance(color, tuple) and len(color)==3:
            return "#{:02x}{:02x}{:02x}".format(*color)
        if isinstance(color, int):
            return "#{:06x}".format(color)
    except Exception:
        pass
    return "#777777"

# -------------------------
# Embed builders
# -------------------------
def build_embed_for_forward(author_name: str, avatar_url: str, color: discord.Color, content: str, ts: str, attachment_filenames=None):
    embed = discord.Embed(description=content or " ", color=color, timestamp=datetime.now(timezone.utc))
    embed.set_author(name=author_name, icon_url=avatar_url)
    embed.set_footer(text=ts)
    if attachment_filenames:
        # we don't set image here; the caller will set image using attachment (attachment://filename)
        pass
    return embed

# -------------------------
# Transcript generation (HTML)
# -------------------------
def generate_html_transcript(channel: discord.TextChannel, entries: list[dict]) -> str:
    """Create transcripts/transcript_{channel.id}_{ts}.html and return path."""
    os.makedirs("transcripts", exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"transcripts/transcript_{channel.id}_{ts}.html"
    # Basic HTML + inline CSS to approximate embed look
    html_parts = []
    html_parts.append("<!doctype html><html><head><meta charset='utf-8'><title>Transcript</title>")
    html_parts.append("<style>")
    html_parts.append("""
    body{background:#0f1115;color:#e6eef6;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:20px}
    .msg{border-radius:8px;padding:12px;margin:10px 0;display:flex;gap:12px;align-items:flex-start}
    .bar{width:6px;height:100%;border-radius:4px 0 0 4px}
    .avatar{width:36px;height:36px;border-radius:50%}
    .content{flex:1}
    .meta{font-size:12px;color:#98a0aa;margin-bottom:6px}
    .text{white-space:pre-wrap}
    .att img{max-width:320px;border-radius:6px;margin-top:8px}
    .att a{color:#9bd;display:block;margin-top:6px}
    """)
    html_parts.append("</style></head><body>")
    html_parts.append(f"<h2>Transcript for #{channel.name} ({channel.id})</h2>")
    for e in entries:
        color_hex = e.get("color", "#777777")
        html_parts.append(f"<div class='msg' style='background:#111214;border-left:6px solid {color_hex};'>")
        # avatar
        avatar = e.get("avatar_url", "")
        html_parts.append(f"<img class='avatar' src='{avatar}' alt='avatar'>")
        html_parts.append("<div class='content'>")
        html_parts.append(f"<div class='meta'><strong>{e.get('author_name')}</strong> • {e.get('ts')}</div>")
        html_parts.append(f"<div class='text'>{discord.utils.escape_markdown(e.get('content',''))}</div>")
        # attachments
        atts = e.get("attachments", [])
        if atts:
            html_parts.append("<div class='att'>")
            for a in atts:
                url = a.get("url")
                fn = a.get("filename")
                ext = fn.split(".")[-1].lower() if fn and "." in fn else ""
                if ext in ("png","jpg","jpeg","gif","webp","bmp","svg"):
                    html_parts.append(f"<img src='{url}' alt='{fn}'>")
                else:
                    html_parts.append(f"<a href='{url}' target='_blank'>{fn}</a>")
            html_parts.append("</div>")
        html_parts.append("</div></div>")
    html_parts.append("</body></html>")
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(html_parts))
    return filename

# -------------------------
# Create ticket channel helper
# -------------------------
async def create_ticket_channel(guild: discord.Guild, user: discord.User):
    cat = guild.get_channel(data.get("category_id")) if data.get("category_id") else None
    staff_role = guild.get_role(data.get("staff_role_id")) if data.get("staff_role_id") else None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    channel_name = f"ticket-{user.id}"
    if cat and isinstance(cat, discord.CategoryChannel):
        ch = await cat.create_text_channel(channel_name, overwrites=overwrites, reason="Modmail ticket created")
    else:
        ch = await guild.create_text_channel(channel_name, overwrites=overwrites, reason="Modmail ticket created")

    data["tickets"][str(user.id)] = ch.id
    save_data(data)
    # init logs
    session_logs[ch.id] = []
    return ch

# -------------------------
# DM Confirmation View (15s)
# -------------------------
class DMConfirmView(ui.View):
    def __init__(self, user: discord.User, orig_message: discord.Message, timeout: int = 15):
        super().__init__(timeout=timeout)
        self.user = user
        self.orig_message = orig_message
        self.created_channel = None

    async def _do_create(self, interaction: discord.Interaction):
        guild = bot.get_guild(PRIMARY_GUILD_ID)
        if not guild:
            await self.user.send("❌ Support unavailable right now. Try again later.")
            return None

        ch = await create_ticket_channel(guild, self.user)

        # header embed
        user_color = await get_user_embed_color(self.user)
        header = discord.Embed(title="📩 New Ticket", description=f"Ticket created by {self.user.mention} ({self.user.id})",
                               color=user_color, timestamp=datetime.now(timezone.utc))
        try:
            header.set_thumbnail(url=self.user.display_avatar.url)
        except Exception:
            pass
        acct_days = (datetime.now(timezone.utc) - self.user.created_at).days
        header.add_field(name="Account Age (days)", value=str(acct_days), inline=False)
        header.add_field(name="Created", value=now_ts(), inline=False)
        try:
            await ch.send(embed=header, view=TicketView(ticket_user_id=self.user.id))
        except Exception:
            await ch.send(embed=header)

        # forward original message as embed + attachments
        # prepare files
        files = []
        att_filenames = []
        for att in self.orig_message.attachments:
            try:
                f = await att.to_file()
                files.append(f)
                att_filenames.append(f.filename)
            except Exception:
                pass

        forward_emb = build_embed_for_forward(
            author_name=str(self.user),
            avatar_url=self.user.display_avatar.url,
            color=user_color,
            content=self.orig_message.content or "[attachment]",
            ts=now_ts(),
            attachment_filenames=att_filenames
        )
        # if there is at least one image-like attachment, set image to attachment://firstfilename
        if att_filenames:
            first = att_filenames[0]
            ext = first.split(".")[-1].lower() if "." in first else ""
            if ext in ("png","jpg","jpeg","gif","webp","bmp","svg"):
                forward_emb.set_image(url=f"attachment://{first}")

        # send
        try:
            if files:
                await ch.send(embed=forward_emb, files=files)
            else:
                await ch.send(embed=forward_emb)
        except Exception:
            pass

        # add to session logs
        entry = {
            "author_name": str(self.user),
            "author_id": self.user.id,
            "avatar_url": str(self.user.display_avatar.url),
            "color": color_to_hex(user_color),
            "content": self.orig_message.content or "",
            "attachments": [{"filename": f.filename, "url": getattr(f, "url", "")} for f in files],
            "ts": now_ts()
        }
        session_logs[ch.id] = [entry]

        # DM user confirmation
        try:
            await self.user.send(f"✅ Ticket created: {ch.mention if hasattr(ch, 'mention') else ch.name}. Staff will respond there.")
        except Exception:
            pass

        self.created_channel = ch
        return ch

    @ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("This confirmation is for the original user.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ch = await self._do_create(interaction)
        if ch:
            await interaction.followup.send("✅ Ticket created.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Could not create ticket.", ephemeral=True)
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❎")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("This confirmation is for the original user.", ephemeral=True)
        await interaction.response.send_message("❎ Ticket creation canceled.", ephemeral=True)
        self.stop()

    async def on_timeout(self):
        try:
            await self.user.send("⏳ Confirmation timed out after 15 seconds. Ticket not created. Send your message again to try.")
        except Exception:
            pass

# -------------------------
# Ticket view for staff (Mark solved / Close)
# -------------------------
class TicketView(ui.View):
    def __init__(self, ticket_user_id: int, timeout=None):
        super().__init__(timeout=timeout)
        self.ticket_user_id = ticket_user_id

    @ui.button(label="Mark Solved", style=discord.ButtonStyle.success, emoji="✅")
    async def mark_solved(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff(interaction.user) and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❎ You are not staff.", ephemeral=True)
        embed = discord.Embed(title="✅ Problem Marked Solved", description=f"Marked solved by {interaction.user.mention}",
                              color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Time", value=now_ts())
        await interaction.channel.send(embed=embed)
        # DM user if mapped
        ch_id = interaction.channel.id
        owner_uid = None
        for uid, cid in data.get("tickets", {}).items():
            if cid == ch_id:
                owner_uid = int(uid); break
        if owner_uid:
            try:
                u = await bot.fetch_user(owner_uid)
                await u.send("✅ Your ticket has been marked solved by staff. Reply here to re-open.")
            except Exception:
                pass
        await interaction.response.send_message("✅ Marked solved.", ephemeral=True)

    @ui.button(label="Close Ticket (delete)", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff(interaction.user) and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❎ You are not staff.", ephemeral=True)
        await interaction.response.send_message("✅ Closing ticket — transcript will be created and channel deleted in 5s.", ephemeral=True)
        ch = interaction.channel
        ch_id = ch.id
        # generate transcript if logs exist
        logs = session_logs.get(ch_id, [])
        try:
            path = generate_html_transcript(ch, logs)
            # post transcript file
            try:
                await ch.send("📁 Transcript:", file=discord.File(path))
            except Exception:
                pass
        except Exception:
            pass
        # remove mapping
        removed_uid = None
        for uid, cid in list(data.get("tickets", {}).items()):
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

# -------------------------
# on_message: forwarding, confirmation, keywords
# -------------------------
@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.author.bot:
        return

    # DM -> new or existing ticket
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
            # forward into existing ticket
            user_color = await get_user_embed_color(user)
            # prepare files
            files = []
            att_filenames = []
            for att in message.attachments:
                try:
                    f = await att.to_file()
                    files.append(f)
                    att_filenames.append(f.filename)
                except Exception:
                    pass
            emb = build_embed_for_forward(str(user), str(user.display_avatar.url), user_color, message.content or "[attachment]", now_ts(), att_filenames)
            if att_filenames:
                ext = att_filenames[0].split(".")[-1].lower() if "." in att_filenames[0] else ""
                if ext in ("png","jpg","jpeg","gif","webp","bmp","svg"):
                    emb.set_image(url=f"attachment://{att_filenames[0]}")
            try:
                if files:
                    await channel.send(embed=emb, files=files)
                else:
                    await channel.send(embed=emb)
            except Exception:
                pass
            # log
            entry = {
                "author_name": str(user),
                "author_id": user.id,
                "avatar_url": str(user.display_avatar.url),
                "color": color_to_hex(user_color),
                "content": message.content or "",
                "attachments": [{"filename": f.filename, "url": getattr(f, "url", "")} for f in files],
                "ts": now_ts()
            }
            session_logs.setdefault(channel.id, []).append(entry)
            return

        # No ticket yet -> ask for confirmation
        confirm_embed = discord.Embed(title="Confirm: Create Support Ticket?", description=message.content or "[attachment]",
                                      color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
        try:
            confirm_embed.set_thumbnail(url=user.display_avatar.url)
        except Exception:
            pass
        if message.attachments:
            att_names = "\n".join([f"- {att.filename}" for att in message.attachments])
            confirm_embed.add_field(name="Attachments", value=att_names, inline=False)
        confirm_embed.set_footer(text="Press ✅ to confirm or ❎ to cancel. This request times out in 15s.")
        # prepare files
        confirm_files = []
        for att in message.attachments:
            try:
                confirm_files.append(await att.to_file())
            except Exception:
                pass
        view = DMConfirmView(user=user, orig_message=message, timeout=15)
        try:
            if confirm_files:
                await user.send(embed=confirm_embed, view=view, files=confirm_files)
            else:
                await user.send(embed=confirm_embed, view=view)
        except Exception:
            pass
        return

    # In-guild: ticket channel handling (staff message forwarded to user)
    if message.guild and message.guild.id == PRIMARY_GUILD_ID:
        ch_id = message.channel.id
        tickets_map = data.get("tickets", {})
        if ch_id in list(tickets_map.values()):
            member = message.author
            # only staff/admin can interact in ticket (we still allow but skip forwarding if not staff)
            if not is_staff(member) and not member.guild_permissions.administrator:
                return

            content = message.content or ""
            solve_kw = (data.get("solve_keyword") or "solved").lower().strip()
            close_kw = (data.get("close_keyword") or "close").lower().strip()
            lc = content.strip().lower()
            matched_solve = (lc == solve_kw) or lc.startswith(solve_kw)
            matched_close = (lc == close_kw) or lc.startswith(close_kw)

            if matched_solve:
                embed = discord.Embed(title="✅ Problem Marked Solved", description=f"Marked solved by {member.mention}", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
                embed.add_field(name="Time", value=now_ts())
                await message.channel.send(embed=embed)
                try:
                    await message.delete()
                except Exception:
                    pass
                # DM user
                target_uid = None
                for uid, cid in tickets_map.items():
                    if cid == ch_id:
                        target_uid = int(uid); break
                if target_uid:
                    try:
                        u = await bot.fetch_user(target_uid)
                        await u.send("✅ Your ticket has been marked solved by staff. If it's still an issue, reply here to re-open.")
                    except Exception:
                        pass
                # log
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
                embed = discord.Embed(title="🗑️ Ticket Closed", description=f"Closed by {member.mention}", color=discord.Color.dark_gray(), timestamp=datetime.now(timezone.utc))
                embed.add_field(name="Time", value=now_ts())
                await message.channel.send(embed=embed)
                # transcript
                logs = session_logs.get(ch_id, [])
                try:
                    path = generate_html_transcript(message.channel, logs)
                    try:
                        await message.channel.send("📁 Transcript:", file=discord.File(path))
                    except Exception:
                        pass
                except Exception:
                    pass
                # remove mapping and delete channel
                removed_uid = None
                for uid, cid in list(tickets_map.items()):
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
                return

            # Normal staff message -> forward to user
            target_uid = None
            for uid, cid in tickets_map.items():
                if cid == ch_id:
                    target_uid = int(uid); break
            if target_uid:
                try:
                    u = await bot.fetch_user(target_uid)
                    # prepare files
                    files = []
                    att_filenames = []
                    for att in message.attachments:
                        try:
                            f = await att.to_file()
                            files.append(f)
                            att_filenames.append(f.filename)
                        except Exception:
                            pass
                    # embed with staff color
                    staff_color = member.color or discord.Color.default()
                    emb = build_embed_for_forward(str(member), str(member.display_avatar.url), staff_color, content or "[attachment]", now_ts(), att_filenames)
                    if att_filenames:
                        ext = att_filenames[0].split(".")[-1].lower() if "." in att_filenames[0] else ""
                        if ext in ("png","jpg","jpeg","gif","webp","bmp","svg"):
                            emb.set_image(url=f"attachment://{att_filenames[0]}")
                    # send to user
                    if files:
                        await u.send(embed=emb, files=files)
                    else:
                        await u.send(embed=emb)
                    try:
                        await message.add_reaction("✅")
                    except Exception:
                        pass
                    # log
                    session_logs.setdefault(ch_id, []).append({
                        "author_name": str(member),
                        "author_id": member.id,
                        "avatar_url": str(member.display_avatar.url),
                        "color": color_to_hex(member.color),
                        "content": content or "",
                        "attachments": [{"filename": f.filename, "url": getattr(f, "url", "")} for f in files],
                        "ts": now_ts()
                    })
                except Exception:
                    try:
                        await message.channel.send("❌ Could not DM the user (DMS may be closed).")
                    except Exception:
                        pass
                return

# -------------------------
# Commands (slash + prefix) - setup, settings, keywords, list, force_close, owner tools, commands list
# -------------------------
def mention_to_id(s: str):
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

# Setup
@bot.tree.command(name="setup", description="Set ticket category and staff role (admin only).")
async def setup_slash(interaction: discord.Interaction, category: discord.CategoryChannel, staff_role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    data["category_id"] = category.id
    data["staff_role_id"] = staff_role.id
    save_data(data)
    await interaction.response.send_message(f"✅ Category set to **{category.name}** and staff role set to **{staff_role.name}** — {now_ts()}", ephemeral=True)

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

# Settings
@bot.tree.command(name="settings", description="Show current modmail settings (admin only).")
async def settings_slash(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    guild = bot.get_guild(PRIMARY_GUILD_ID)
    category = guild.get_channel(data.get("category_id")) if data.get("category_id") else None
    staff_role = guild.get_role(data.get("staff_role_id")) if data.get("staff_role_id") else None
    embed = discord.Embed(title="⚙️ Modmail Settings", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Category", value=(f"{category.name} ({category.id})" if category else "❎ Not set"), inline=False)
    embed.add_field(name="Staff Role", value=(f"{staff_role.name} ({staff_role.id})" if staff_role else "❎ Not set"), inline=False)
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
    embed = discord.Embed(title="⚙️ Modmail Settings", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Category", value=(f"{category.name} ({category.id})" if category else "❎ Not set"), inline=False)
    embed.add_field(name="Staff Role", value=(f"{staff_role.name} ({staff_role.id})" if staff_role else "❎ Not set"), inline=False)
    embed.add_field(name="Solve Keyword", value=f"`{data.get('solve_keyword')}`", inline=True)
    embed.add_field(name="Close Keyword", value=f"`{data.get('close_keyword')}`", inline=True)
    embed.add_field(name="Open Tickets", value=str(len(data.get("tickets", {}))), inline=False)
    await ctx.send(embed=embed)

# Keywords
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

# Listing / force close
@bot.tree.command(name="list_tickets", description="List open tickets (admin only).")
async def list_tickets_slash(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    tickets = data.get("tickets", {})
    if not tickets:
        return await interaction.response.send_message("❎ No open tickets.", ephemeral=True)
    lines = [f"User `{uid}` → Channel ID `{cid}`" for uid, cid in tickets.items()]
    await interaction.response.send_message("Open tickets:\n" + "\n".join(lines), ephemeral=True)

@bot.command(name="list_tickets")
@commands.has_permissions(administrator=True)
async def list_tickets_prefix(ctx: commands.Context):
    tickets = data.get("tickets", {})
    if not tickets:
        return await ctx.send("❎ No open tickets.")
    lines = [f"User `{uid}` → Channel ID `{cid}`" for uid, cid in tickets.items()]
    await ctx.send("Open tickets:\n" + "\n".join(lines))

@bot.tree.command(name="force_close", description="Force close this ticket (staff/admin only).")
async def force_close_slash(interaction: discord.Interaction):
    ch = interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message("❎ This must be used in a ticket channel.", ephemeral=True)
    if not is_staff(interaction.user) and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Staff only.", ephemeral=True)
    await interaction.response.send_message("✅ Force closing and deleting channel in 4s...", ephemeral=True)
    ch_id = ch.id
    to_remove = None
    for uid, cid in list(data.get("tickets", {}).items()):
        if cid == ch_id:
            to_remove = uid; break
    if to_remove:
        data["tickets"].pop(to_remove, None)
        save_data(data)
    # transcript
    logs = session_logs.get(ch_id, [])
    try:
        path = generate_html_transcript(ch, logs)
        try:
            await ch.send("📁 Transcript:", file=discord.File(path))
        except Exception:
            pass
    except Exception:
        pass
    session_logs.pop(ch_id, None)
    await asyncio.sleep(4)
    try:
        await ch.delete(reason=f"Force-closed by {interaction.user}")
    except Exception:
        pass

@bot.command(name="force_close")
@commands.has_permissions(manage_channels=True)
async def force_close_prefix(ctx: commands.Context):
    ch = ctx.channel
    if not isinstance(ch, discord.TextChannel):
        return await ctx.send("❎ This must be used in a ticket channel.")
    if not is_staff(ctx.author) and not ctx.author.guild_permissions.administrator:
        return await ctx.send("❎ Staff only.")
    await ctx.send("✅ Force closing and deleting channel in 4s...")
    ch_id = ch.id
    to_remove = None
    for uid, cid in list(data.get("tickets", {}).items()):
        if cid == ch_id:
            to_remove = uid; break
    if to_remove:
        data["tickets"].pop(to_remove, None)
        save_data(data)
    logs = session_logs.get(ch_id, [])
    try:
        path = generate_html_transcript(ch, logs)
        try:
            await ch.send("📁 Transcript:", file=discord.File(path))
        except Exception:
            pass
    except Exception:
        pass
    session_logs.pop(ch_id, None)
    await asyncio.sleep(4)
    try:
        await ch.delete(reason=f"Force-closed by {ctx.author}")
    except Exception:
        pass

# Owner tools: refresh & restart
@bot.command(name="refresh")
async def refresh_prefix(ctx: commands.Context):
    if ctx.author.id != OWNER_ID:
        return await ctx.send("❎ Owner only.")
    try:
        guild_obj = discord.Object(id=PRIMARY_GUILD_ID)
        synced = await bot.tree.sync(guild=guild_obj)
        msg = f"✅ Refreshed {len(synced)} commands for guild {PRIMARY_GUILD_ID}"
        print(msg); await ctx.send(msg)
    except Exception as e:
        err = f"❌ Refresh failed: {e}"
        print(err); await ctx.send(err)

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

# Commands list help
@bot.command(name="commands")
async def commands_list(ctx: commands.Context):
    embed = discord.Embed(title="📚 Modmail Commands (prefix & slash)", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Setup", value="`!setup <category_id|#mention> <role_id|@mention>`\n`/setup category:<category> staff_role:<role>`\nAdmin only — sets ticket category and staff role.", inline=False)
    embed.add_field(name="Settings", value="`!settings` / `/settings` — shows current category/staff/keywords and ticket count.", inline=False)
    embed.add_field(name="Keywords", value="`!set_solve_keyword <word>` / `/set_solve_keyword` and `!set_close_keyword <word>` / `/set_close_keyword`", inline=False)
    embed.add_field(name="Ticket Flow", value="Users DM bot → confirm in 15s → ticket channel created. Staff messages in ticket forward to user. Attachments (images/gifs/videos) are forwarded.", inline=False)
    embed.add_field(name="Staff Tools", value="`!force_close` / `/force_close` — delete current ticket channel. `!list_tickets` / `/list_tickets` — list open tickets.", inline=False)
    embed.add_field(name="Owner Tools", value="`!refresh` / `/refresh` — resync slash commands. `!restart` / `/restart` — restart the bot (owner only).", inline=False)
    await ctx.send(embed=embed)

@bot.tree.command(name="commands", description="Show modmail commands and usage (prefix & slash).")
async def commands_slash(interaction: discord.Interaction):
    embed = discord.Embed(title="📚 Modmail Commands (prefix & slash)", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Setup", value="`!setup <category_id|#mention> <role_id|@mention>`\n`/setup category:<category> staff_role:<role>`\nAdmin only — sets ticket category and staff role.", inline=False)
    embed.add_field(name="Settings", value="`!settings` / `/settings` — shows current category/staff/keywords and ticket count.", inline=False)
    embed.add_field(name="Keywords", value="`!set_solve_keyword <word>` / `/set_solve_keyword` and `!set_close_keyword` / `/set_close_keyword`", inline=False)
    embed.add_field(name="Ticket Flow", value="Users DM bot → confirm in 15s → ticket channel created. Staff messages in ticket forward to user. Attachments (images/gifs/videos) are forwarded.", inline=False)
    embed.add_field(name="Staff Tools", value="`!force_close` / `/force_close` — delete current ticket channel. `!list_tickets` / `/list_tickets` — list open tickets.", inline=False)
    embed.add_field(name="Owner Tools", value="`!refresh` / `/refresh` — resync slash commands. `!restart` / `/restart` — restart the bot (owner only).", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -------------------------
# on_ready: sync & DM owner
# -------------------------
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=PRIMARY_GUILD_ID))
        print(f"✅ Synced {len(synced)} commands to guild {PRIMARY_GUILD_ID}")
    except Exception as e:
        print(f"⚠️ Guild sync failed: {e}. Attempting global sync...")
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
    except Exception as e:
        print(f"⚠️ Could not DM owner: {e}")

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    print("Starting modmail bot...")
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Bot failed to start: {e}")
        raise
