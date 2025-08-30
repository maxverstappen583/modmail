# main.py
import discord
from discord.ext import commands
from discord import app_commands, ui
import asyncio
import json
import os
from flask import Flask
from threading import Thread
import tempfile
import datetime

# ----------------- CONFIG -----------------
OWNER_ID = 1319292111325106296  # your user id
GUILD_ID = 1364371104755613837  # set to your guild id (int) to sync guild-only (recommended), or None for global
CONFIG_PATH = "config.json"
COOLDOWN_SECONDS = 300  # 5 minutes for /modmail
DM_CONFIRM_TIMEOUT = 15  # 15 seconds confirm in DM
# ------------------------------------------

# ---------- helper: load/save config ----------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        cfg = {"category_id": None, "staff_role_id": None, "log_channel_id": None, "tickets": {}}
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=4)
        return cfg
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)

config = load_config()

# ---------- Flask keep-alive (Render) ----------
app = Flask("")
@app.route("/")
def home():
    return "✅ ModMail bot running"
def run_flask():
    app.run(host="0.0.0.0", port=10000)
Thread(target=run_flask, daemon=True).start()

# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.dm_messages = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# store cooldowns for /modmail (user_id -> last_timestamp)
modmail_cooldowns = {}

# ---------- utility helpers ----------
def is_owner(user: discord.User) -> bool:
    return user.id == OWNER_ID

def get_guild():
    # if GUILD_ID set, use that guild, otherwise return first guild bot is in
    if GUILD_ID:
        return bot.get_guild(GUILD_ID)
    return bot.guilds[0] if bot.guilds else None

async def send_log_embed(guild: discord.Guild, embed: discord.Embed, file=None):
    log_id = config.get("log_channel_id")
    if not log_id:
        return
    ch = guild.get_channel(log_id)
    if not ch:
        return
    try:
        if file:
            await ch.send(embed=embed, file=file)
        else:
            await ch.send(embed=embed)
    except Exception:
        pass

def make_forward_embed(author: discord.abc.User, content: str, is_staff: bool):
    color = discord.Color.orange() if is_staff else discord.Color.blurple()
    e = discord.Embed(description=content or "‎", color=color, timestamp=datetime.datetime.utcnow())
    e.set_author(name=str(author), icon_url=author.display_avatar.url if hasattr(author, "display_avatar") else None)
    return e

# ---------- Confirm View for DMs ----------
class ConfirmCreateView(ui.View):
    def __init__(self, requester: discord.User, original_message: discord.Message, guild: discord.Guild):
        super().__init__(timeout=DM_CONFIRM_TIMEOUT)
        self.requester = requester
        self.original_message = original_message
        self.guild = guild
        self.result = None  # "confirm" / "cancel" / None

    @ui.button(label="Create Ticket", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.requester.id:
            return await interaction.response.send_message("This confirmation isn't for you.", ephemeral=True)
        self.result = "confirm"
        await interaction.response.send_message("✅ Creating your ticket...", ephemeral=True)
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.requester.id:
            return await interaction.response.send_message("This cancellation isn't for you.", ephemeral=True)
        self.result = "cancel"
        await interaction.response.send_message("❌ Ticket creation cancelled.", ephemeral=True)
        self.stop()

    async def on_timeout(self):
        # inform user on timeout
        try:
            await self.requester.send("⌛ Ticket creation timed out. Send another message if you still want help.")
        except Exception:
            pass

# ---------- Ticket Button View for panels ----------
class TicketPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🎟 Create Ticket", style=discord.ButtonStyle.green, custom_id="modmail:create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: ui.Button):
        # immediate ack
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        # check existing
        if str(user.id) in config.get("tickets", {}):
            ch_id = config["tickets"][str(user.id)]
            ch = interaction.guild.get_channel(ch_id)
            if ch:
                return await interaction.followup.send(f"⚠️ You already have a ticket: {ch.mention}", ephemeral=True)
        # create
        channel = await create_ticket_for_user(interaction.guild, user, opened_by_button=True)
        if not channel:
            return await interaction.followup.send("⚠️ Ticket system not configured properly (category/staff role).", ephemeral=True)
        try:
            await user.send(f"✅ Ticket created: {channel.mention} — you can reply here to talk with staff.")
        except:
            pass
        await interaction.followup.send(f"✅ Ticket created: {channel.mention}", ephemeral=True)

# ---------- Core: create ticket ----------
async def create_ticket_for_user(guild: discord.Guild, user: discord.User, opened_by_button=False):
    cat_id = config.get("category_id")
    staff_role_id = config.get("staff_role_id")
    if not cat_id or not staff_role_id:
        return None
    category = guild.get_channel(cat_id)
    staff_role = guild.get_role(staff_role_id)
    if not category or not staff_role:
        return None

    # make channel name safe
    safe_name = f"ticket-{user.name}".replace(" ", "-")[:90]
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    }
    ch = await guild.create_text_channel(name=safe_name, category=category, overwrites=overwrites, topic=str(user.id))
    # save mapping
    config.setdefault("tickets", {})[str(user.id)] = ch.id
    save_config(config)

    # announce
    await ch.send(f"{staff_role.mention} 📬 New ticket opened for {user.mention}")
    # log
    embed = discord.Embed(title="Ticket Opened", description=f"Ticket for {user} opened", color=discord.Color.green(), timestamp=datetime.datetime.utcnow())
    embed.add_field(name="Channel", value=ch.mention, inline=True)
    embed.add_field(name="Opened by", value=str(user), inline=True)
    await send_log_embed(guild, embed)
    return ch

# ---------- transcript helper ----------
async def create_transcript(channel: discord.TextChannel) -> str:
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author = f"{m.author} ({m.author.id})"
        content = m.content
        lines.append(f"[{ts}] {author}: {content}")
        for a in m.attachments:
            lines.append(f"    [attachment] {a.url}")
    text = "\n".join(lines)
    # write to temp file
    fd, path = tempfile.mkstemp(prefix=f"transcript_{channel.id}_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path

# ---------- COMMANDS: setup (owner-only) ----------
def owner_check(inter: discord.Interaction):
    return inter.user.id == OWNER_ID

def owner_only():
    return app_commands.check(lambda inter: owner_check(inter))

@bot.tree.command(name="set_category", description="Owner: set the ticket category")
@owner_only()
async def set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    await interaction.response.defer(ephemeral=True)
    config["category_id"] = category.id
    save_config(config)
    await interaction.followup.send(f"✅ Ticket category set to **{category.name}**", ephemeral=True)

@bot.tree.command(name="set_staffrole", description="Owner: set the staff role")
@owner_only()
async def set_staffrole(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    config["staff_role_id"] = role.id
    save_config(config)
    await interaction.followup.send(f"✅ Staff role set to {role.mention}", ephemeral=True)

@bot.tree.command(name="set_logchannel", description="Owner: set the log channel")
@owner_only()
async def set_logchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    config["log_channel_id"] = channel.id
    save_config(config)
    await interaction.followup.send(f"✅ Log channel set to {channel.mention}", ephemeral=True)

# ---------- send panel command ----------
@bot.tree.command(name="send_panel", description="Owner: send the ticket creation panel here")
@owner_only()
async def send_panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(title="🎟 Support / Create Ticket", description="Click the button below to open a private ticket with staff.", color=discord.Color.blue())
    view = TicketPanelView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.followup.send("✅ Ticket panel sent.", ephemeral=True)

# ---------- /modmail command with cooldown ----------
@bot.tree.command(name="modmail", description="Open a ModMail ticket (5 min cooldown)")
async def modmail_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # cooldown
    now = asyncio.get_event_loop().time()
    last = modmail_cooldowns.get(interaction.user.id, 0)
    if now - last < COOLDOWN_SECONDS:
        remaining = int(COOLDOWN_SECONDS - (now - last))
        return await interaction.followup.send(f"⏳ You must wait {remaining}s before creating another ticket.", ephemeral=True)
    modmail_cooldowns[interaction.user.id] = now

    # check config
    guild = interaction.guild or get_guild()
    if not guild:
        return await interaction.followup.send("⚠️ Bot not in any guild.", ephemeral=True)

    if not config.get("category_id") or not config.get("staff_role_id"):
        return await interaction.followup.send("⚠️ Ticket system not configured. Owner must run /set_category and /set_staffrole.", ephemeral=True)

    # if user already has ticket
    if str(interaction.user.id) in config.get("tickets", {}):
        ch_id = config["tickets"][str(interaction.user.id)]
        ch = guild.get_channel(ch_id)
        if ch:
            return await interaction.followup.send(f"⚠️ You already have an open ticket: {ch.mention}", ephemeral=True)

    # create ticket
    ch = await create_ticket_for_user(guild, interaction.user)
    if not ch:
        return await interaction.followup.send("⚠️ Failed to create ticket (check config).", ephemeral=True)

    # notify user
    try:
        await interaction.user.send(f"✅ Ticket created: {ch.mention} — reply here to chat with staff.")
    except:
        pass

    await interaction.followup.send(f"✅ Ticket created: {ch.mention}", ephemeral=True)

# ---------- /close command ----------
@bot.tree.command(name="close", description="Close this ticket (staff or owner)")
async def close_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    # check if current channel is a ticket
    if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.followup.send("⚠️ This command must be used in a ticket channel.", ephemeral=True)

    channel = interaction.channel
    # check mapping
    user_id = None
    for uid, cid in config.get("tickets", {}).items():
        if cid == channel.id:
            user_id = uid
            break
    if not user_id:
        return await interaction.followup.send("⚠️ This is not a managed ticket channel.", ephemeral=True)

    # permission: owner or staff role
    member = interaction.user
    staff_role_id = config.get("staff_role_id")
    if not (is_owner(member) or (staff_role_id and discord.utils.get(member.roles, id=staff_role_id))):
        return await interaction.followup.send("❌ Only staff or the owner can close tickets.", ephemeral=True)

    # make transcript
    try:
        transcript_path = await create_transcript(channel)
        file = discord.File(transcript_path, filename=os.path.basename(transcript_path))
    except Exception as e:
        file = None

    # send log
    guild = interaction.guild or get_guild()
    embed = discord.Embed(title="Ticket Closed", color=discord.Color.red(), timestamp=datetime.datetime.utcnow())
    embed.add_field(name="Channel", value=channel.name, inline=True)
    try:
        user = await bot.fetch_user(int(user_id))
        embed.add_field(name="User", value=str(user), inline=True)
    except:
        embed.add_field(name="User", value=user_id, inline=True)
    embed.add_field(name="Closed by", value=str(interaction.user), inline=True)

    await send_log_embed(guild, embed, file=file)

    # cleanup mapping
    config["tickets"].pop(user_id, None)
    save_config(config)

    # notify user
    try:
        u = await bot.fetch_user(int(user_id))
        await u.send("✅ Your ticket has been closed by staff. A transcript has been saved to logs.")
    except:
        pass

    # delete file after sending
    if file:
        try:
            os.remove(transcript_path)
        except:
            pass

    # delete channel
    await interaction.followup.send("🗂️ Ticket closed and logged.", ephemeral=True)
    await channel.delete()

# ---------- /refresh command (owner-only) ----------
@bot.tree.command(name="refresh", description="Owner: refresh slash commands (sync)")
@owner_only()
async def refresh_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        # sync globally and (optionally) to guild for instant update
        if GUILD_ID:
            await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        await bot.tree.sync()
        await interaction.followup.send("✅ Commands synced (global + guild).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Sync failed: {e}", ephemeral=True)

# ---------- MESSAGE ROUTING ----------
@bot.event
async def on_message(message: discord.Message):
    # ignore bot
    if message.author.bot:
        return

    # 1) If DM: handle DM -> ticket flow
    if isinstance(message.channel, discord.DMChannel):
        user = message.author
        # if user already has ticket, forward directly
        if str(user.id) in config.get("tickets", {}):
            ch_id = config["tickets"][str(user.id)]
            guild = get_guild()
            if not guild:
                return
            ch = guild.get_channel(ch_id)
            if ch:
                # embed + attachments
                emb = make_forward_embed(user, message.content or "", is_staff=False)
                files = []
                for a in message.attachments:
                    try:
                        files.append(await a.to_file())
                    except:
                        pass
                try:
                    await ch.send(embed=emb, files=files)
                except:
                    pass
            return

        # otherwise ask for confirm via buttons
        guild = get_guild()
        if not guild:
            return await user.send("⚠️ Bot not in any server to open a ticket.")
        # Build view and send confirm DM with original content preview
        preview = make_forward_embed(user, message.content or "", is_staff=False)
        try:
            view = ConfirmCreateView(user, message, guild)
            await user.send(embed=preview, view=view)
        except Exception:
            # if cannot send DM, we can't continue
            return

        # wait for view to stop (user action or timeout)
        await view.wait()
        if view.result != "confirm":
            # user canceled or timed out
            return

        # create ticket and forward original message
        ch = await create_ticket_for_user(guild, user)
        if not ch:
            try:
                await user.send("⚠️ Ticket creation failed (owner might not have configured category/staff role).")
            except:
                pass
            return
        # forward original message (with attachments)
        emb = make_forward_embed(user, message.content or "", is_staff=False)
        files = []
        for a in message.attachments:
            try:
                files.append(await a.to_file())
            except:
                pass
        try:
            await ch.send(embed=emb, files=files)
        except:
            pass
        # notify user
        try:
            await user.send(f"✅ Ticket created: {ch.mention} — staff will reply here or in this DM.")
        except:
            pass
        return

    # 2) If message in guild: check if in ticket channel (forward to user's DM)
    if message.guild:
        # check if channel is in tickets mapping
        for uid, cid in config.get("tickets", {}).items():
            if cid == message.channel.id:
                # only forward staff messages (avoid echoing user's own messages if they have send permission)
                member = message.author
                # staff check
                staff_role_id = config.get("staff_role_id")
                is_staff = (member.id == OWNER_ID) or (staff_role_id and any(r.id == staff_role_id for r in member.roles))
                if not is_staff:
                    # don't forward non-staff messages to user (prevent loops)
                    return await bot.process_commands(message)
                # forward embed with attachments
                try:
                    user = await bot.fetch_user(int(uid))
                    emb = make_forward_embed(member, message.content or "", is_staff=True)
                    files = []
                    for a in message.attachments:
                        try:
                            files.append(await a.to_file())
                        except:
                            pass
                    try:
                        if files:
                            await user.send(embed=emb, files=files)
                        else:
                            await user.send(embed=emb)
                    except:
                        await message.channel.send("⚠️ Could not DM the user.")
                    # log the forward
                    guild = message.guild
                    log_embed = discord.Embed(title="Message forwarded to user", color=discord.Color.light_grey(), timestamp=datetime.datetime.utcnow())
                    log_embed.add_field(name="From", value=str(member), inline=True)
                    log_embed.add_field(name="To (user)", value=uid, inline=True)
                    log_embed.add_field(name="Channel", value=message.channel.mention, inline=True)
                    await send_log_embed(guild, log_embed)
                except Exception:
                    pass
                return

    # process other commands (important)
    await bot.process_commands(message)

# ---------- start ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    # try to sync (guild sync if GUILD_ID set for instant availability)
    try:
        if GUILD_ID:
            await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        await bot.tree.sync()
    except Exception as e:
        print("Slash sync error:", e)

# run bot
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    print("ERROR: set DISCORD_BOT_TOKEN env var")
else:
    bot.run(TOKEN)
