# main.py
# Full ModMail (Style 2: DM <-> Server relay) with 15s confirm countdown on panel open.
# Put this file, .env, and requirements.txt together. Start with `python main.py`.

import os
import json
import asyncio
import datetime
from threading import Thread
from dotenv import load_dotenv
from flask import Flask

import discord
from discord.ext import commands
from discord import ui, ButtonStyle, app_commands

# ---------------- Load environment ----------------
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or ""
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
OTHER_GUILD_RESPONSE = os.getenv("OTHER_GUILD_RESPONSE", "Sorry, this bot only works in the official server.")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # optional

if OPENAI_API_KEY:
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        USE_OPENAI = True
    except Exception:
        USE_OPENAI = False
else:
    USE_OPENAI = False

FOOTER_TEXT = "@u4_straight1"
FOOTER_ICON = "https://i.postimg.cc/rp5b7Jkn/IMG-6152.jpg"

if not BOT_TOKEN or GUILD_ID == 0:
    raise SystemExit("ERROR: Set DISCORD_BOT_TOKEN and GUILD_ID in your .env before running.")

# ---------------- Flask keepalive (for hosting) ----------------
app = Flask("modmail_keepalive")

@app.route("/")
def _home():
    return "ModMail bot alive."

def _run_flask():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

Thread(target=_run_flask, daemon=True).start()

# ---------------- Settings persistence ----------------
SETTINGS_FILE = "modmail_settings.json"
DEFAULTS = {
    "staff_role": 0,
    "log_channel": 0,
    "ticket_category": 0,
    "cooldown": 60,
    "active_tickets": {},  # "user_id" (str) -> channel_id (int)
    "last_open": {}        # "user_id" -> iso timestamp
}

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, indent=2)
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    changed = False
    for k, v in DEFAULTS.items():
        if k not in data:
            data[k] = v
            changed = True
    if changed:
        save_settings(data)
    return data

def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

settings = load_settings()

# ---------------- Bot initialization ----------------
intents = discord.Intents.all()  # needed for DMs, member lookups, attachments, message_content
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------- Helpers ----------------
def guild_only_app():
    async def _check(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return True
        if interaction.guild.id != GUILD_ID:
            try:
                await interaction.response.send_message(OTHER_GUILD_RESPONSE, ephemeral=True)
            except Exception:
                pass
            return False
        return True
    return app_commands.check(_check)

def is_staff_member(member: discord.Member | discord.User) -> bool:
    try:
        sr = int(settings.get("staff_role") or 0)
    except Exception:
        sr = 0
    if isinstance(member, discord.Member):
        if sr and any(r.id == sr for r in member.roles):
            return True
        if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
            return True
    return False

def top_role_color(member: discord.Member | None) -> discord.Color:
    if not member:
        return discord.Color.greyple()
    for r in reversed(member.roles):
        if r.color.value != 0:
            return r.color
    return discord.Color.greyple()

def create_user_embed(user: discord.abc.User, content: str, member_obj=None, attachments=None):
    color = top_role_color(member_obj) if member_obj else discord.Color.greyple()
    embed = discord.Embed(description=content if content else "\u200b", color=color)
    embed.set_author(name=str(user), icon_url=user.display_avatar.url)
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    if attachments:
        for a in attachments:
            if a.content_type and a.content_type.startswith("image/"):
                embed.set_image(url=a.url)
                break
    return embed

async def ensure_category(guild: discord.Guild) -> discord.CategoryChannel:
    cat_id = int(settings.get("ticket_category") or 0)
    if cat_id:
        cat = discord.utils.get(guild.categories, id=cat_id)
        if cat:
            return cat
    cat = discord.utils.get(guild.categories, name="Tickets")
    if cat:
        return cat
    return await guild.create_category("Tickets")

async def create_ticket_channel_for_user(guild: discord.Guild, user: discord.User) -> discord.TextChannel | None:
    member = guild.get_member(user.id)
    if not member:
        try:
            await user.send("You must be a member of the server to open a ticket.")
        except Exception:
            pass
        return None

    cat = await ensure_category(guild)
    staff_role_id = int(settings.get("staff_role") or 0)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
        member: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
    }
    if staff_role_id:
        role = guild.get_role(staff_role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)

    ch = await guild.create_text_channel(name=f"ticket-{user.id}", category=cat, overwrites=overwrites)
    settings["active_tickets"][str(user.id)] = ch.id
    save_settings(settings)

    ping_text = guild.get_role(staff_role_id).mention if staff_role_id and guild.get_role(staff_role_id) else "@here"
    intro = discord.Embed(title="🎫 New Ticket", description=f"{user.mention} opened a ticket. Staff please assist.", color=discord.Color.red())
    intro.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    await ch.send(content=ping_text, embed=intro, view=TicketControlsView(opener_id=user.id))
    return ch

async def summarize_user_messages(channel: discord.TextChannel, user_id: int) -> str:
    collected = []
    async for m in channel.history(limit=300, oldest_first=True):
        if m.author.id == user_id and (m.content or m.attachments):
            text = m.content or ""
            if m.attachments:
                text += " " + " ".join(a.filename for a in m.attachments)
            collected.append(text)
    text = "\n".join(collected[-30:]).strip()
    if not text:
        return "No clear problem detected."
    if USE_OPENAI:
        try:
            resp = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role":"user","content":f"Summarize the user's issue in one sentence:\n\n{text}"}],
                max_tokens=80
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            pass
    return (text[:200] + "…") if len(text) > 200 else text

async def make_transcript_file(channel: discord.TextChannel) -> str:
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author = f"{m.author} ({m.author.id})"
        content = (m.content or "").replace("\n", "\\n")
        lines.append(f"[{ts}] {author}: {content}")
        if m.attachments:
            for a in m.attachments:
                lines.append(f"    [attachment] {a.filename} -> {a.url}")
    txt = "\n".join(lines) if lines else "No messages."
    fname = f"transcript-{channel.name}-{int(channel.id)}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(txt)
    return fname

async def close_ticket_flow(channel: discord.TextChannel, closed_by: discord.abc.User):
    opener_id = None
    for uid, cid in list(settings.get("active_tickets", {}).items()):
        if cid == channel.id:
            opener_id = int(uid)
            break
    fname = await make_transcript_file(channel)
    file = discord.File(fname)
    lc_id = int(settings.get("log_channel") or 0)
    if lc_id:
        try:
            log_ch = channel.guild.get_channel(lc_id)
            if log_ch:
                embed = discord.Embed(title="📑 Ticket Transcript", description=f"Transcript for {channel.name}\nClosed by: {getattr(closed_by,'mention',str(closed_by))}", color=discord.Color.red())
                embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
                await log_ch.send(embed=embed, file=file)
        except Exception:
            pass
    if opener_id:
        try:
            user = await bot.fetch_user(opener_id)
            if user:
                await user.send("📑 Here is your ticket transcript:", file=discord.File(fname))
        except Exception:
            pass
    if opener_id:
        settings["active_tickets"].pop(str(opener_id), None)
        save_settings(settings)
    try:
        os.remove(fname)
    except Exception:
        pass
    try:
        await channel.delete()
    except Exception:
        pass

# ---------------- Views ----------------
class PanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Open Ticket", style=ButtonStyle.danger, custom_id="panel_open_ticket")
    async def open_ticket_button(self, interaction: discord.Interaction, button: ui.Button):
        # Guard: only in configured guild
        if not interaction.guild or interaction.guild.id != GUILD_ID:
            return await interaction.response.send_message(OTHER_GUILD_RESPONSE, ephemeral=True)

        # Start the ephemeral 15s confirmation flow
        confirm_view = ConfirmView(opener_id=interaction.user.id, guild=interaction.guild)
        await interaction.response.send_message(f"Confirm opening a ticket — you have 15 seconds.", view=confirm_view, ephemeral=True)
        # original_response returns the ephemeral message object we can edit
        msg = await interaction.original_response()
        # assign the message object into the view and start countdown loop
        confirm_view.set_message(msg)
        asyncio.create_task(confirm_view.start_countdown())

class ConfirmView(ui.View):
    def __init__(self, opener_id: int, guild: discord.Guild, timeout: int = 20):
        super().__init__(timeout=None)
        self.opener_id = opener_id
        self.msg = None
        self.guild = guild
        self._done = asyncio.Event()
        self._confirmed = False
        self._cancelled = False
        self.seconds = 15

    def set_message(self, msg: discord.Message):
        self.msg = msg

    async def start_countdown(self):
        # update ephemeral message every second with remaining seconds
        try:
            for i in range(self.seconds, 0, -1):
                if self._done.is_set():
                    break
                try:
                    await self.msg.edit(content=f"Confirm opening a ticket — time remaining: **{i}s**", view=self)
                except Exception:
                    pass
                await asyncio.sleep(1)
            if not self._done.is_set():
                # time expired -> cancel
                self._cancelled = True
                try:
                    await self.msg.edit(content="⏰ Confirmation timed out. Ticket not created.", view=None)
                except Exception:
                    pass
                self._done.set()
        except Exception:
            pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # only allow the opener to confirm/cancel
        if interaction.user.id != self.opener_id:
            await interaction.response.send_message("Only the ticket opener can use these buttons.", ephemeral=True)
            return False
        return True

    @ui.button(label="✅ Confirm", style=ButtonStyle.success, custom_id="confirm_yes")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if self._done.is_set():
            return await interaction.response.send_message("Action already completed.", ephemeral=True)
        self._confirmed = True
        self._done.set()
        await interaction.response.defer(ephemeral=True)
        # create ticket channel
        ch = await create_ticket_channel_for_user(self.guild, interaction.user)
        if ch:
            try:
                await self.msg.edit(content=f"✅ Ticket created: {ch.mention}", view=None)
            except Exception:
                pass
            # reply ephemeral ack
            await interaction.followup.send(f"Your ticket was created: {ch.mention}", ephemeral=True)
        else:
            try:
                await self.msg.edit(content="❌ Could not create ticket. Make sure you're a guild member.", view=None)
            except Exception:
                pass
            await interaction.followup.send("❌ Could not create ticket.", ephemeral=True)

    @ui.button(label="❎ Cancel", style=ButtonStyle.secondary, custom_id="confirm_no")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if self._done.is_set():
            return await interaction.response.send_message("Action already completed.", ephemeral=True)
        self._cancelled = True
        self._done.set()
        try:
            await self.msg.edit(content="Cancelled. No ticket created.", view=None)
        except Exception:
            pass
        await interaction.response.send_message("Cancelled — no ticket created.", ephemeral=True)

class TicketControlsView(ui.View):
    def __init__(self, opener_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @ui.button(label="Problem Solved ✅", style=ButtonStyle.success, custom_id="ticket_solved")
    async def solved(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff_member(interaction.user):
            return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
        opener_member = interaction.guild.get_member(self.opener_id)
        if opener_member:
            overwrites = interaction.channel.overwrites
            overwrites[opener_member] = discord.PermissionOverwrite(read_messages=True, send_messages=False, read_message_history=True)
            await interaction.channel.edit(overwrites=overwrites)
        summary = await summarize_user_messages(interaction.channel, self.opener_id)
        embed = discord.Embed(title="Ticket marked as solved", description=f"**Detected problem:**\n{summary}", color=discord.Color.red())
        embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
        await interaction.response.send_message(embed=embed, view=CloseView(self.opener_id))

    @ui.button(label="Not Solved ❎", style=ButtonStyle.secondary, custom_id="ticket_not_solved")
    async def not_solved(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff_member(interaction.user):
            return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
        opener_member = interaction.guild.get_member(self.opener_id)
        if opener_member:
            overwrites = interaction.channel.overwrites
            overwrites[opener_member] = discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)
            await interaction.channel.edit(overwrites=overwrites)
        await interaction.response.send_message("🔓 Ticket re-opened for the user to reply.", ephemeral=True)

class CloseView(ui.View):
    def __init__(self, opener_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @ui.button(label="Close Ticket 🔒", style=ButtonStyle.danger, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff_member(interaction.user):
            return await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await close_ticket_flow(interaction.channel, interaction.user)

# ---------------- Events ----------------
@bot.event
async def on_ready():
    print(f"✅ Bot logged in as {bot.user} (id: {bot.user.id})")
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"✅ Slash commands synced to guild {GUILD_ID}")
    except Exception as e:
        print("❌ Failed to sync slash commands:", e)
    bot.add_view(PanelView())  # persist panel button handlers across restarts
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="ModMail"))

@bot.event
async def on_typing(channel, user, when):
    if user.bot:
        return
    if isinstance(channel, discord.DMChannel):
        uid = str(user.id)
        active = settings.get("active_tickets", {})
        if uid in active:
            guild = bot.get_guild(GUILD_ID)
            ch_id = active[uid]
            ch = guild.get_channel(ch_id) if guild else None
            if ch:
                async with ch.typing():
                    await asyncio.sleep(2)

@bot.event
async def on_message(message: discord.Message):
    # ignore other guilds entirely
    if message.guild and message.guild.id != GUILD_ID:
        return
    if message.author.bot:
        return

    active = settings.get("active_tickets", {})
    guild = bot.get_guild(GUILD_ID)

    # DM from user -> forward (create ticket automatically if none)
    if isinstance(message.channel, discord.DMChannel):
        uid = str(message.author.id)
        member = guild.get_member(message.author.id) if guild else None
        if not member:
            try:
                await message.author.send("You must be a member of the server to open a ticket.")
            except Exception:
                pass
            return
        if uid in active:
            ch = guild.get_channel(active[uid])
            if ch is None:
                ch = await create_ticket_channel_for_user(guild, message.author)
        else:
            ch = await create_ticket_channel_for_user(guild, message.author)
        if not ch:
            return
        files = [await a.to_file() for a in message.attachments] if message.attachments else None
        embed = create_user_embed(message.author, message.content, member_obj=member, attachments=message.attachments)
        if files:
            await ch.send(embed=embed, files=files)
        else:
            await ch.send(embed=embed)

    # Staff message in ticket channel -> forward to user DM
    elif isinstance(message.channel, discord.TextChannel) and message.channel.name.startswith("ticket-"):
        opener_id = None
        for uid, cid in list(settings.get("active_tickets", {}).items()):
            if cid == message.channel.id:
                opener_id = int(uid)
                break
        if not opener_id:
            return
        try:
            user = await bot.fetch_user(opener_id)
        except Exception:
            user = bot.get_user(opener_id)
        if not user:
            return
        content = (f"**Staff:** {message.content}") if message.content else ""
        files = [await a.to_file() for a in message.attachments] if message.attachments else None
        try:
            if files:
                await user.send(content or "\u200b", files=files)
            else:
                await user.send(content or "\u200b")
        except Exception:
            await message.channel.send("⚠️ Could not DM the user. They may have DMs closed.")

    await bot.process_commands(message)

# ---------------- Slash commands ----------------
@bot.tree.command(name="send_panel", description="Send the ModMail panel to this channel")
@guild_only_app()
async def cmd_send_panel(interaction: discord.Interaction):
    if not is_staff_member(interaction.user):
        return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
    embed = discord.Embed(title="📮 Need Help?", description="Press **Open Ticket** to contact staff privately.", color=discord.Color.red())
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    await interaction.response.send_message(embed=embed, view=PanelView())

@bot.tree.command(name="refresh", description="Re-sync slash commands to the server")
@guild_only_app()
async def cmd_refresh(interaction: discord.Interaction):
    if not is_staff_member(interaction.user):
        return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        await interaction.response.send_message("✅ Commands refreshed.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to refresh: {e}", ephemeral=True)

@bot.tree.command(name="settings", description="Show current ModMail settings")
@guild_only_app()
async def cmd_settings(interaction: discord.Interaction):
    if not is_staff_member(interaction.user):
        return await interaction.response.send_message("Only staff can use this.", ephemeral=True)
    s = load_settings()
    staff_role = f"<@&{int(s.get('staff_role') or 0)}>" if int(s.get('staff_role') or 0) else "Not set"
    log_channel = f"<#{int(s.get('log_channel') or 0)}>" if int(s.get('log_channel') or 0) else "Not set"
    ticket_cat = f"{int(s.get('ticket_category') or 0) or 'Auto/\"Tickets\"'}"
    cooldown = int(s.get('cooldown') or 0)
    active_count = len(s.get("active_tickets", {}))
    desc = (
        f"**Staff role:** {staff_role}\n"
        f"**Log channel:** {log_channel}\n"
        f"**Ticket category:** {ticket_cat}\n"
        f"**Cooldown:** {cooldown}s\n"
        f"**Active tickets:** {active_count}\n"
        f"**Guild ID:** {GUILD_ID}"
    )
    embed = discord.Embed(title="⚙️ ModMail Settings", description=desc, color=discord.Color.blurple())
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="set_staff_role", description="Set the staff role (admin only)")
@guild_only_app()
@app_commands.describe(role="Role to assign as staff")
async def cmd_set_staff_role(interaction: discord.Interaction, role: discord.Role):
    if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
    settings["staff_role"] = role.id
    save_settings(settings)
    await interaction.response.send_message(f"✅ Staff role set to {role.mention}", ephemeral=True)

@bot.tree.command(name="set_log_channel", description="Set channel for transcripts (admin only)")
@guild_only_app()
@app_commands.describe(channel="Text channel to send transcripts to")
async def cmd_set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
    settings["log_channel"] = channel.id
    save_settings(settings)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="set_category", description="Set ticket category (admin only)")
@guild_only_app()
@app_commands.describe(category="Category to create tickets in")
async def cmd_set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
    settings["ticket_category"] = category.id
    save_settings(settings)
    await interaction.response.send_message(f"✅ Ticket category set to **{category.name}**", ephemeral=True)

@bot.tree.command(name="set_cooldown", description="Set cooldown between opens in seconds (admin only)")
@guild_only_app()
@app_commands.describe(seconds="Seconds, 0 to disable")
async def cmd_set_cooldown(interaction: discord.Interaction, seconds: int):
    if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
    if seconds < 0:
        seconds = 0
    settings["cooldown"] = seconds
    save_settings(settings)
    await interaction.response.send_message(f"✅ Cooldown set to {seconds}s", ephemeral=True)

@bot.tree.command(name="close", description="Close this ticket and save transcript")
@guild_only_app()
async def cmd_close(interaction: discord.Interaction):
    if not is_staff_member(interaction.user):
        return await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
    if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel) or not interaction.channel.name.startswith("ticket-"):
        return await interaction.response.send_message("This command must be used inside a ticket channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await close_ticket_flow(interaction.channel, interaction.user)

# ---------------- Run ----------------
bot.run(BOT_TOKEN)
