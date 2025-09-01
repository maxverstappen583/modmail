# main.py
# Full modmail bot with:
# - Guild-only slash commands (instant sync)
# - /send_panel /refresh /settings /set_staff_role /set_log_channel /set_category /set_cooldown /close
# - Panel with red embed + "Open Ticket" button
# - One ticket per user
# - Staff role ping on create
# - DM <-> Ticket sync (text + images/videos)
# - Staff-only buttons: "Problem Solved ✅", "Not Solved ❎" -> then "Close Ticket 🔒"
# - Auto summary (optional OpenAI). Works without key.
# - Transcripts (.txt) to log channel and DM to user
# - Footer (image+text) on all embeds
# - Flask keep-alive
# - Settings persisted in modmail_settings.json

import os, json, asyncio, datetime
from threading import Thread

import discord
from discord.ext import commands
from discord import ui, ButtonStyle, app_commands

from flask import Flask
from dotenv import load_dotenv

# ---------- ENV ----------
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
OTHER_GUILD_RESPONSE = os.getenv(
    "OTHER_GUILD_RESPONSE",
    "Sorry, this bot only works in the official server."
)

# Optional OpenAI summary
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
USE_OPENAI = bool(OPENAI_API_KEY)
if USE_OPENAI:
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
    except Exception:
        USE_OPENAI = False  # if library missing, just disable summary

FOOTER_TEXT = "@u4_straight1"
FOOTER_ICON = "https://i.postimg.cc/rp5b7Jkn/IMG-6152.jpg"

# ---------- FLASK KEEP-ALIVE ----------
app = Flask(__name__)

@app.route("/")
def home():
    return "Modmail bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_flask, daemon=True).start()

# ---------- SETTINGS STORAGE ----------
SETTINGS_FILE = "modmail_settings.json"
DEFAULT_SETTINGS = {
    "staff_role": 0,            # int role id
    "log_channel": 0,           # int channel id
    "ticket_category": 0,       # int category id
    "cooldown": 60,             # seconds for re-opening
    "active_tickets": {},       # user_id(str) -> channel_id(int)
    "last_open": {}             # user_id(str) -> iso timestamp
}

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        save_settings(DEFAULT_SETTINGS.copy())
    with open(SETTINGS_FILE, "r") as f:
        data = json.load(f)
    # ensure keys
    changed = False
    for k, v in DEFAULT_SETTINGS.items():
        if k not in data:
            data[k] = v
            changed = True
    if changed:
        save_settings(data)
    return data

def save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

settings = load_settings()

# ---------- BOT ----------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- UTIL ----------
def guild_only_app():
    async def predicate(inter: discord.Interaction) -> bool:
        # Allow DMs to pass through (some commands not used in DMs anyway)
        if inter.guild and inter.guild.id != GUILD_ID:
            await inter.response.send_message(OTHER_GUILD_RESPONSE, ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

def is_staff(member: discord.Member) -> bool:
    sr_id = int(settings.get("staff_role") or 0)
    if sr_id == 0:
        # No staff role configured -> fallback to Manage Guild or Admin
        return member.guild_permissions.manage_guild or member.guild_permissions.administrator
    return any(r.id == sr_id for r in member.roles)

def top_role_color(member: discord.Member) -> discord.Color:
    for role in reversed(member.roles):
        if role.color.value != 0:
            return role.color
    return discord.Color.greyple()

def user_embed(user: discord.abc.User, content: str, *, member: discord.Member | None = None, attachments: list[discord.Attachment] | None = None) -> discord.Embed:
    color = top_role_color(member) if member else discord.Color.greyple()
    embed = discord.Embed(description=content if content else "\u200b", color=color)
    embed.set_author(name=str(user), icon_url=user.display_avatar.url)
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    # Preview first image in embed, still forward files separately
    if attachments:
        for a in attachments:
            if a.content_type and a.content_type.startswith("image/"):
                embed.set_image(url=a.url)
                break
    return embed

async def summarize_problem_from_channel(channel: discord.TextChannel, user_id: int) -> str:
    # gather last ~50 user messages
    collected = []
    async for msg in channel.history(limit=200, oldest_first=True):
        if msg.author.id == user_id and (msg.content or msg.attachments):
            line = msg.content if msg.content else ""
            if msg.attachments:
                att_list = " ".join([att.filename for att in msg.attachments])
                line = (line + " " + att_list).strip()
            collected.append(line)
    text = "\n".join(collected[-25:]).strip()
    if not text:
        return "No clear problem detected from the conversation."

    if USE_OPENAI:
        try:
            # Old API for broad compatibility
            resp = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "system", "content": "Summarize the user's issue in one short sentence."},
                          {"role": "user", "content": text}],
                max_tokens=60,
                temperature=0.2
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            pass
    # fallback simple heuristic
    return (text[:180] + "…") if len(text) > 180 else text

async def ensure_category(guild: discord.Guild) -> discord.CategoryChannel:
    cat_id = int(settings.get("ticket_category") or 0)
    if cat_id:
        cat = discord.utils.get(guild.categories, id=cat_id)
        if cat:
            return cat
    # create default "Tickets" if not set
    cat = discord.utils.get(guild.categories, name="Tickets")
    if cat:
        return cat
    return await guild.create_category("Tickets")

async def create_ticket(guild: discord.Guild, opener: discord.User) -> discord.TextChannel:
    staff_role_id = int(settings.get("staff_role") or 0)
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None
    category = await ensure_category(guild)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
    }
    # allow opener and staff
    overwrites[opener] = discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)

    ch = await guild.create_text_channel(name=f"ticket-{opener.id}", category=category, overwrites=overwrites)

    # store active mapping
    settings["active_tickets"][str(opener.id)] = ch.id
    save_settings(settings)

    # ping staff and show controls
    ping_text = staff_role.mention if staff_role else "@here"
    intro = discord.Embed(
        title="🎫 New Ticket",
        description=f"{opener.mention} opened a ticket.\nPlease wait for staff to assist you.",
        color=discord.Color.red()
    )
    intro.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    await ch.send(content=ping_text, embed=intro, view=TicketControlsView(opener_id=opener.id))
    return ch

async def make_transcript(channel: discord.TextChannel) -> str:
    # returns filename
    lines = []
    async for msg in channel.history(limit=None, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        base = f"[{ts}] {msg.author} ({msg.author.id}):"
        content = (msg.content or "").replace("\n", "\\n")
        if content:
            lines.append(f"{base} {content}")
        else:
            lines.append(f"{base}")
        if msg.attachments:
            for a in msg.attachments:
                lines.append(f"    [attachment] {a.filename} -> {a.url}")
    text = "\n".join(lines) if lines else "No messages."
    fname = f"transcript-{channel.name}-{int(channel.id)}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(text)
    return fname

async def close_ticket_flow(channel: discord.TextChannel, closed_by: discord.abc.User):
    # find mapped user
    user_id = None
    for uid, cid in list(settings["active_tickets"].items()):
        if cid == channel.id:
            user_id = int(uid)
            break

    # build and send transcript
    fname = await make_transcript(channel)
    file = discord.File(fname)

    # send to log channel
    log_channel_id = int(settings.get("log_channel") or 0)
    if log_channel_id:
        log_ch = channel.guild.get_channel(log_channel_id)
        if log_ch:
            embed = discord.Embed(
                title="📑 Ticket Closed",
                description=f"Channel: {channel.mention}\nClosed by: {closed_by.mention}",
                color=discord.Color.red()
            )
            embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
            await log_ch.send(embed=embed, file=file)

    # DM transcript to the ticket user (best effort)
    if user_id:
        try:
            user = await bot.fetch_user(user_id)
            await user.send("📑 Here is the transcript of your ticket.", file=discord.File(fname))
        except Exception:
            pass

    # cleanup file and mapping
    try: os.remove(fname)
    except Exception: pass

    if user_id:
        settings["active_tickets"].pop(str(user_id), None)
        save_settings(settings)

    await channel.delete()

# ---------- VIEWS / BUTTONS ----------
class PanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Open Ticket", style=ButtonStyle.success, custom_id="open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if not interaction.guild or interaction.guild.id != GUILD_ID:
            return await interaction.response.send_message(OTHER_GUILD_RESPONSE, ephemeral=True)

        # One-ticket + cooldown
        s = load_settings()
        active = s.get("active_tickets", {})
        last_open = s.get("last_open", {})
        uid = str(interaction.user.id)

        if uid in active:
            ch = interaction.guild.get_channel(active[uid])
            if ch:
                return await interaction.response.send_message(f"❌ You already have a ticket: {ch.mention}", ephemeral=True)
            else:
                active.pop(uid, None)
                s["active_tickets"] = active
                save_settings(s)

        cd = int(s.get("cooldown") or 0)
        if cd and uid in last_open:
            try:
                last_ts = datetime.datetime.fromisoformat(last_open[uid])
                delta = (datetime.datetime.utcnow() - last_ts).total_seconds()
                if delta < cd:
                    return await interaction.response.send_message(
                        f"⏳ Please wait {int(cd - delta)}s before opening another ticket.",
                        ephemeral=True
                    )
            except Exception:
                pass

        ch = await create_ticket(interaction.guild, interaction.user)
        s["last_open"][uid] = datetime.datetime.utcnow().isoformat()
        save_settings(s)
        await interaction.response.send_message(f"✅ Ticket created: {ch.mention}", ephemeral=True)

class TicketControlsView(ui.View):
    def __init__(self, opener_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @ui.button(label="Problem Solved ✅", style=ButtonStyle.success, custom_id="ticket_solved")
    async def solved(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can use this.", ephemeral=True)

        # lock user from talking
        overwrites = interaction.channel.overwrites
        opener = interaction.guild.get_member(self.opener_id)
        if opener:
            overwrites[opener] = discord.PermissionOverwrite(read_messages=True, send_messages=False, read_message_history=True)
            await interaction.channel.edit(overwrites=overwrites)

        # auto-summarize problem
        summary = await summarize_problem_from_channel(interaction.channel, self.opener_id)
        embed = discord.Embed(
            title="✅ Marked as Solved",
            description=f"**Detected problem:**\n{summary}",
            color=discord.Color.red()
        )
        embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
        await interaction.response.send_message(embed=embed, view=CloseView(self.opener_id))

    @ui.button(label="Not Solved ❎", style=ButtonStyle.secondary, custom_id="ticket_not_solved")
    async def not_solved(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can use this.", ephemeral=True)

        # re-open user permissions
        opener = interaction.guild.get_member(self.opener_id)
        if opener:
            overwrites = interaction.channel.overwrites
            overwrites[opener] = discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)
            await interaction.channel.edit(overwrites=overwrites)

        await interaction.response.send_message("🔓 Ticket re-opened for the user to reply.", ephemeral=True)

class CloseView(ui.View):
    def __init__(self, opener_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @ui.button(label="Close Ticket 🔒", style=ButtonStyle.danger, custom_id="close_ticket")
    async def close_btn(self, interaction: discord.Interaction, button: ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await close_ticket_flow(interaction.channel, interaction.user)

# ---------- EVENTS ----------
@bot.event
async def on_ready():
    print(f"🤖 Logged in as {bot.user}")
    # sync guild-only (instant)
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"✅ Slash commands synced to guild {GUILD_ID}")
    except Exception as e:
        print("❌ Failed to sync commands:", e)
    # persistent panel button
    bot.add_view(PanelView())
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="ModMail"))

@bot.event
async def on_message(message: discord.Message):
    # never process other guilds
    if message.guild and message.guild.id != GUILD_ID:
        return

    # ignore bots
    if message.author.bot:
        return

    s = load_settings()
    active = s.get("active_tickets", {})
    guild = bot.get_guild(GUILD_ID)

    # USER DM -> TICKET
    if isinstance(message.channel, discord.DMChannel):
        # find or create
        uid = str(message.author.id)
        if uid in active:
            ticket_ch = guild.get_channel(active[uid])
            if ticket_ch is None:
                # stale id -> create new
                ticket_ch = await create_ticket(guild, message.author)
        else:
            ticket_ch = await create_ticket(guild, message.author)

        member = guild.get_member(message.author.id)
        embed = user_embed(message.author, message.content, member=member, attachments=message.attachments)
        files = [await a.to_file() for a in message.attachments] if message.attachments else None
        await ticket_ch.send(embed=embed, files=files)

    # STAFF MESSAGE IN TICKET -> DM USER
    elif message.channel and message.channel.name.startswith("ticket-"):
        # find mapped user
        target_user_id = None
        for uid, cid in active.items():
            if cid == message.channel.id:
                target_user_id = int(uid)
                break
        if target_user_id:
            try:
                target = await bot.fetch_user(target_user_id)
            except Exception:
                target = bot.get_user(target_user_id)
            if target:
                content = message.content if message.content else ""
                files = [await a.to_file() for a in message.attachments] if message.attachments else None
                # prepend label
                if content:
                    content = f"**Staff:** {content}"
                await target.send(content or "\u200b", files=files)

    await bot.process_commands(message)

# ---------- SLASH COMMANDS ----------
@bot.tree.command(name="send_panel", description="Send the ModMail panel here")
@guild_only_app()
async def send_panel(inter: discord.Interaction):
    if not is_staff(inter.user):
        return await inter.response.send_message("Only staff can use this.", ephemeral=True)
    embed = discord.Embed(
        title="📮 Need Help?",
        description="Press **Open Ticket** to contact staff privately.",
        color=discord.Color.red()
    )
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    await inter.response.send_message(embed=embed, view=PanelView())

@bot.tree.command(name="refresh", description="Re-sync slash commands to this server")
@guild_only_app()
async def refresh(inter: discord.Interaction):
    if not is_staff(inter.user):
        return await inter.response.send_message("Only staff can use this.", ephemeral=True)
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    await inter.response.send_message("✅ Commands refreshed.", ephemeral=True)

@bot.tree.command(name="settings", description="Show current ModMail settings")
@guild_only_app()
async def settings_cmd(inter: discord.Interaction):
    if not is_staff(inter.user):
        return await inter.response.send_message("Only staff can use this.", ephemeral=True)
    s = load_settings()
    def fmt_role(rid): return f"<@&{rid}>" if rid else "Not set"
    def fmt_ch(cid): return f"<#{cid}>" if cid else "Not set"
    desc = (
        f"**Staff Role:** {fmt_role(int(s.get('staff_role') or 0))}\n"
        f"**Log Channel:** {fmt_ch(int(s.get('log_channel') or 0))}\n"
        f"**Ticket Category:** {int(s.get('ticket_category') or 0) or 'Auto/\"Tickets\"'}\n"
        f"**Cooldown:** {int(s.get('cooldown') or 0)}s\n"
        f"**Active Tickets:** {len(s.get('active_tickets', {}))}\n"
        f"**Guild Lock:** {GUILD_ID}"
    )
    embed = discord.Embed(title="⚙️ ModMail Settings", description=desc, color=discord.Color.blurple())
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    await inter.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="set_staff_role", description="Set the staff role")
@guild_only_app()
@app_commands.describe(role="Select the staff role")
async def set_staff_role(inter: discord.Interaction, role: discord.Role):
    if not inter.user.guild_permissions.manage_guild and not inter.user.guild_permissions.administrator:
        return await inter.response.send_message("You need Manage Server permission.", ephemeral=True)
    settings["staff_role"] = role.id
    save_settings(settings)
    await inter.response.send_message(f"✅ Staff role set to {role.mention}", ephemeral=True)

@bot.tree.command(name="set_log_channel", description="Set the log channel for transcripts")
@guild_only_app()
@app_commands.describe(channel="Select a text channel")
async def set_log_channel(inter: discord.Interaction, channel: discord.TextChannel):
    if not inter.user.guild_permissions.manage_guild and not inter.user.guild_permissions.administrator:
        return await inter.response.send_message("You need Manage Server permission.", ephemeral=True)
    settings["log_channel"] = channel.id
    save_settings(settings)
    await inter.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="set_category", description="Set the category where tickets are created")
@guild_only_app()
@app_commands.describe(category="Select a category")
async def set_category(inter: discord.Interaction, category: discord.CategoryChannel):
    if not inter.user.guild_permissions.manage_guild and not inter.user.guild_permissions.administrator:
        return await inter.response.send_message("You need Manage Server permission.", ephemeral=True)
    settings["ticket_category"] = category.id
    save_settings(settings)
    await inter.response.send_message(f"✅ Ticket category set to **{category.name}**", ephemeral=True)

@bot.tree.command(name="set_cooldown", description="Set cooldown (seconds) between opening tickets")
@guild_only_app()
@app_commands.describe(seconds="Seconds (0 to disable)")
async def set_cooldown(inter: discord.Interaction, seconds: int):
    if not inter.user.guild_permissions.manage_guild and not inter.user.guild_permissions.administrator:
        return await inter.response.send_message("You need Manage Server permission.", ephemeral=True)
    if seconds < 0: seconds = 0
    settings["cooldown"] = seconds
    save_settings(settings)
    await inter.response.send_message(f"✅ Cooldown set to **{seconds}s**", ephemeral=True)

@bot.tree.command(name="close", description="Close this ticket and save transcript")
@guild_only_app()
async def close_cmd(inter: discord.Interaction):
    if not is_staff(inter.user):
        return await inter.response.send_message("Only staff can close tickets.", ephemeral=True)
    if not inter.channel or not inter.channel.name.startswith("ticket-"):
        return await inter.response.send_message("This is not a ticket channel.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    await close_ticket_flow(inter.channel, inter.user)

# ---------- RUN ----------
if not BOT_TOKEN or not GUILD_ID:
    raise SystemExit("Set DISCORD_BOT_TOKEN and GUILD_ID in .env")

bot.run(BOT_TOKEN)
