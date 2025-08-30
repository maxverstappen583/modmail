# main.py
import discord
from discord.ext import commands
from discord import app_commands, ui
import asyncio
import json
import os
import tempfile
import datetime
from flask import Flask
from threading import Thread

# ----------------- CONFIG -----------------
OWNER_ID = 1319292111325106296,1380315427992768633,285323814991167489     # your user id (owner)
GUILD_ID = 1364371104755613837     # <<--- REPLACE this with your server ID (int)
TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # put your token in env var

CONFIG_FILE = "config.json"
DM_CONFIRM_TIMEOUT = 15            # seconds to confirm DM ticket creation
MODMAIL_COOLDOWN = 300             # 5 minutes cooldown for /modmail

# ----------------- FLASK KEEPALIVE -----------------
app = Flask(__name__)
@app.route("/")
def home():
    return "ModMail bot is alive"

def _run_flask():
    app.run(host="0.0.0.0", port=10000)

Thread(target=_run_flask, daemon=True).start()

# ----------------- STATE / CONFIG -----------------
def load_config():
    if not os.path.exists(CONFIG_FILE):
        base = {"category_id": None, "staff_role_id": None, "log_channel_id": None, "tickets": {}}
        with open(CONFIG_FILE, "w") as f:
            json.dump(base, f, indent=4)
        return base
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)

cfg = load_config()  # cfg keys: category_id, staff_role_id, log_channel_id, tickets { user_id: channel_id }

# ----------------- BOT SETUP -----------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# cooldown state for /modmail
_modmail_timestamps = {}  # user_id -> last_time

# ----------------- HELPERS -----------------
def is_owner_user(user: discord.abc.Snowflake) -> bool:
    return getattr(user, "id", None) == OWNER_ID

def staff_role_object(guild: discord.Guild):
    rid = cfg.get("staff_role_id")
    return guild.get_role(rid) if rid else None

def category_object(guild: discord.Guild):
    cid = cfg.get("category_id")
    return guild.get_channel(cid) if cid else None

def log_channel_object(guild: discord.Guild):
    lid = cfg.get("log_channel_id")
    return guild.get_channel(lid) if lid else None

def user_has_staff_role(member: discord.Member) -> bool:
    rid = cfg.get("staff_role_id")
    if rid is None:
        return False
    return any(r.id == rid for r in member.roles) or member.id == OWNER_ID

def make_user_embed_for_ticket(user: discord.User, content: str) -> discord.Embed:
    e = discord.Embed(description=content or "‎", color=discord.Color.blue(), timestamp=datetime.datetime.utcnow())
    e.set_author(name=str(user), icon_url=user.display_avatar.url if hasattr(user, "display_avatar") else None)
    return e

def make_staff_embed_for_user(staff: discord.Member, content: str) -> discord.Embed:
    e = discord.Embed(description=content or "‎", color=discord.Color.orange(), timestamp=datetime.datetime.utcnow())
    e.set_author(name=str(staff), icon_url=staff.display_avatar.url if hasattr(staff, "display_avatar") else None)
    return e

async def create_transcript(channel: discord.TextChannel) -> str:
    """Return path to a transcript .txt file for the channel (history oldest->newest)."""
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author = f"{m.author} ({m.author.id})"
        content = m.content or ""
        lines.append(f"[{ts}] {author}: {content}")
        for a in m.attachments:
            lines.append(f"    [attachment] {a.url}")
    text = "\n".join(lines)
    fd, path = tempfile.mkstemp(prefix=f"transcript_{channel.id}_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path

async def send_log_embed(guild: discord.Guild, embed: discord.Embed, file_path: str = None):
    ch = log_channel_object(guild)
    if not ch:
        return
    try:
        if file_path:
            await ch.send(embed=embed, file=discord.File(file_path))
        else:
            await ch.send(embed=embed)
    except Exception as e:
        print("Failed to send log:", e)

# ----------------- UI / Views -----------------
class DMConfirmView(ui.View):
    def __init__(self, user: discord.User, original_message: discord.Message):
        super().__init__(timeout=DM_CONFIRM_TIMEOUT)
        self.requester = user
        self.original_message = original_message
        self.result = None  # "confirm" or "cancel" or None

    @ui.button(label="Create Ticket", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.requester.id:
            return await interaction.response.send_message("This confirmation isn't for you.", ephemeral=True)
        self.result = "confirm"
        await interaction.response.send_message("✅ Creating ticket...", ephemeral=True)
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.requester.id:
            return await interaction.response.send_message("This cancellation isn't for you.", ephemeral=True)
        self.result = "cancel"
        await interaction.response.send_message("❌ Cancelled.", ephemeral=True)
        self.stop()

    async def on_timeout(self):
        # notify user on timeout
        try:
            await self.requester.send("⌛ Ticket creation timed out. Send another message to try again.")
        except:
            pass

class PanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🎟 Create Ticket", style=discord.ButtonStyle.green, custom_id="panel_create_ticket")
    async def create_ticket_button(self, interaction: discord.Interaction, button: ui.Button):
        # create ticket flow (reuse create_ticket_for_user)
        await interaction.response.defer(ephemeral=True)
        await create_ticket_for_user(interaction.user, interaction.guild, opened_via="button")
        await interaction.followup.send("✅ If configured, your ticket has been created. Check DMs.", ephemeral=True)

# ----------------- CORE: create ticket -----------------
async def create_ticket_for_user(user: discord.User, guild: discord.Guild, opened_via: str = "command", first_message: discord.Message = None):
    """Creates a ticket channel for user in guild. Returns channel or None and error message."""
    if guild.id != GUILD_ID:
        return None, "This bot works only in the configured server."

    cat = category_object(guild)
    staff_role = staff_role_object(guild)
    if not cat or not staff_role:
        return None, "Ticket system not configured (category or staff role missing)."

    # Prevent duplicate ticket if exists in cfg
    if str(user.id) in cfg.get("tickets", {}):
        ch_id = cfg["tickets"][str(user.id)]
        ch = guild.get_channel(ch_id)
        return ch, "You already have an open ticket."

    # Create channel
    safe_name = f"ticket-{user.name}".replace(" ", "-")[:90]
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    channel = await cat.create_text_channel(name=safe_name, overwrites=overwrites, topic=str(user.id))

    # Save mapping
    cfg.setdefault("tickets", {})[str(user.id)] = channel.id
    save_config(cfg)

    # Announce in channel and log
    await channel.send(f"{staff_role.mention} 📬 New ticket opened by {user.mention} (via {opened_via})")
    embed = discord.Embed(title="Ticket Opened", color=discord.Color.green(), timestamp=datetime.datetime.utcnow())
    embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
    embed.add_field(name="Channel", value=channel.mention, inline=False)
    embed.add_field(name="Opened via", value=opened_via, inline=True)
    await send_log_embed(guild, embed)

    # Forward first message if provided
    if first_message:
        try:
            # build embed + attachments
            emb = make_user_embed_for_ticket(user, first_message.content or "")
            files = []
            for a in first_message.attachments:
                try:
                    files.append(await a.to_file())
                except Exception:
                    pass
            if files:
                await channel.send(embed=emb, files=files)
            else:
                await channel.send(embed=emb)
        except Exception as e:
            print("Error forwarding first message:", e)

    # DM user that ticket created
    try:
        await user.send(f"✅ Your ticket has been opened: {channel.mention}. Staff will respond here or in DMs.")
    except Exception:
        pass

    return channel, None

# ----------------- TRANSCRIPT + CLOSE -----------------
async def close_ticket(channel: discord.TextChannel, closed_by: discord.Member):
    # find user id from topic or cfg mapping
    user_id = None
    # first check topic
    try:
        if channel.topic and channel.topic.isdigit():
            user_id = channel.topic
    except:
        user_id = None

    # fallback to cfg
    if not user_id:
        for uid, cid in cfg.get("tickets", {}).items():
            if cid == channel.id:
                user_id = uid
                break

    # create transcript and send to log
    guild = channel.guild
    transcript_path = None
    try:
        transcript_path = await create_transcript(channel)
    except Exception as e:
        print("Transcript creation failed:", e)

    embed = discord.Embed(title="Ticket Closed", color=discord.Color.red(), timestamp=datetime.datetime.utcnow())
    embed.add_field(name="Channel", value=channel.name, inline=True)
    embed.add_field(name="Closed by", value=str(closed_by), inline=True)
    if user_id:
        try:
            user = await bot.fetch_user(int(user_id))
            embed.add_field(name="User", value=f"{user} ({user_id})", inline=True)
            # DM user
            try:
                await user.send("✅ Your ticket has been closed by staff. A transcript was posted to logs.")
            except:
                pass
        except Exception:
            embed.add_field(name="User ID", value=user_id, inline=True)

    # send to log
    await send_log_embed(guild, embed, file_path=transcript_path if transcript_path else None)

    # cleanup mapping
    if user_id and str(user_id) in cfg.get("tickets", {}):
        cfg["tickets"].pop(str(user_id), None)
        save_config(cfg)

    # delete channel
    try:
        await channel.delete()
    except Exception:
        pass

    # remove transcript file
    if transcript_path:
        try:
            os.remove(transcript_path)
        except:
            pass

# ----------------- COMMANDS -----------------
# owner-only check for app_commands
def owner_only():
    def predicate(interaction: discord.Interaction):
        return interaction.user.id == OWNER_ID
    return app_commands.check(predicate)

@tree.command(name="set_category", description="Owner: set ticket category", guild=discord.Object(id=GUILD_ID))
@owner_only()
async def cmd_set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    await interaction.response.defer(thinking=True, ephemeral=True)
    cfg["category_id"] = category.id
    save_config(cfg)
    await interaction.followup.send(f"✅ Category set to **{category.name}**", ephemeral=True)

@tree.command(name="set_staff_role", description="Owner: set staff role", guild=discord.Object(id=GUILD_ID))
@owner_only()
async def cmd_set_staff_role(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(thinking=True, ephemeral=True)
    cfg["staff_role_id"] = role.id
    save_config(cfg)
    await interaction.followup.send(f"✅ Staff role set to {role.mention}", ephemeral=True)

@tree.command(name="set_log_channel", description="Owner: set log channel", guild=discord.Object(id=GUILD_ID))
@owner_only()
async def cmd_set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(thinking=True, ephemeral=True)
    cfg["log_channel_id"] = channel.id
    save_config(cfg)
    await interaction.followup.send(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(name="settings", description="Show current modmail settings", guild=discord.Object(id=GUILD_ID))
async def cmd_settings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    g = interaction.guild
    cat = g.get_channel(cfg.get("category_id")) if cfg.get("category_id") else None
    staff = g.get_role(cfg.get("staff_role_id")) if cfg.get("staff_role_id") else None
    log = g.get_channel(cfg.get("log_channel_id")) if cfg.get("log_channel_id") else None

    e = discord.Embed(title="ModMail Settings", color=discord.Color.blurple())
    e.add_field(name="Category", value=cat.name if cat else "❌ Not set", inline=False)
    e.add_field(name="Staff Role", value=staff.name if staff else "❌ Not set", inline=False)
    e.add_field(name="Log Channel", value=log.mention if log else "❌ Not set", inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)

@tree.command(name="send_panel", description="Send the Create Ticket panel (Staff+Owner)", guild=discord.Object(id=GUILD_ID))
async def cmd_send_panel(interaction: discord.Interaction):
    # only staff or owner can send panel
    if not (is_owner_user(interaction.user) or user_has_staff_role(interaction.user)):
        return await interaction.response.send_message("❌ You don't have permission to send the panel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(title="🎟 Need Help?", description="Click the button below to open a private ticket with staff.", color=discord.Color.green())
    view = PanelView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.followup.send("✅ Panel posted.", ephemeral=True)

@tree.command(name="modmail", description="Open a ModMail ticket (5m cooldown)", guild=discord.Object(id=GUILD_ID))
async def cmd_modmail(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    # cooldown
    now = asyncio.get_event_loop().time()
    last = _modmail_timestamps.get(interaction.user.id, 0)
    if now - last < MODMAIL_COOLDOWN:
        remain = int(MODMAIL_COOLDOWN - (now - last))
        return await interaction.followup.send(f"⏳ You must wait {remain}s before creating another ticket.", ephemeral=True)
    _modmail_timestamps[interaction.user.id] = now

    ch, err = await create_ticket_for_user(interaction.user, interaction.guild, opened_via="slash")
    if err:
        return await interaction.followup.send(err, ephemeral=True)
    await interaction.followup.send(f"✅ Ticket created: {ch.mention}", ephemeral=True)

@tree.command(name="close", description="Close this ticket (staff/owner)", guild=discord.Object(id=GUILD_ID))
async def cmd_close(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    # only staff or owner
    if not (is_owner_user(interaction.user) or user_has_staff_role(interaction.user)):
        return await interaction.followup.send("❌ You don't have permission to close tickets.", ephemeral=True)

    # validate channel
    if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.followup.send("⚠️ This command must be used in a ticket channel.", ephemeral=True)

    # ensure it's a managed ticket
    if str(interaction.channel.id) not in map(str, cfg.get("tickets", {}).values()):
        return await interaction.followup.send("❌ This is not a managed ticket channel.", ephemeral=True)

    # proceed to close
    await interaction.followup.send("🗂 Closing ticket and saving transcript...", ephemeral=True)
    await close_ticket(interaction.channel, interaction.user)

@tree.command(name="refresh", description="Refresh slash commands (owner only)", guild=discord.Object(id=GUILD_ID))
@owner_only()
async def cmd_refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        await bot.tree.sync()
        await interaction.followup.send("✅ Commands synced (guild + global).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Sync failed: {e}", ephemeral=True)

# ----------------- EVENTS: DMs & Ticket message forwarding -----------------
@bot.event
async def on_message(message: discord.Message):
    # ignore bots
    if message.author.bot:
        return

    # If DM to bot
    if isinstance(message.channel, discord.DMChannel):
        user = message.author
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            try:
                await user.send("⚠️ Bot is not connected to the configured server.")
            except:
                pass
            return

        # If user already has ticket, forward directly
        if str(user.id) in cfg.get("tickets", {}):
            ch_id = cfg["tickets"].get(str(user.id))
            ch = guild.get_channel(ch_id)
            if ch:
                emb = make_user_embed_for_ticket(user, message.content or "")
                files = []
                for a in message.attachments:
                    try:
                        files.append(await a.to_file())
                    except:
                        pass
                try:
                    if files:
                        await ch.send(embed=emb, files=files)
                    else:
                        await ch.send(embed=emb)
                except Exception:
                    pass
            return

        # Otherwise ask for confirmation with buttons
        try:
            preview = make_user_embed_for_ticket(user, message.content or "")
            view = DMConfirmView(user, message)
            try:
                await user.send("Do you want to create a support ticket? (15s)", embed=preview, view=view)
            except Exception:
                # can't DM user, abort
                return
            # wait until view stops (user clicked or timeout)
            await view.wait()
            if view.result != "confirm":
                try:
                    await user.send("❌ Ticket creation cancelled.")
                except:
                    pass
                return
            # create ticket and forward original message
            ch, err = await create_ticket_for_user(user, guild, opened_via="DM", first_message=message)
            if err:
                try:
                    await user.send(f"⚠️ {err}")
                except:
                    pass
                return
        except Exception as e:
            print("Error handling DM:", e)
            return

        return

    # If message in a guild (possible ticket reply)
    if message.guild and message.guild.id == GUILD_ID:
        # If message occurs in a ticket channel managed by cfg, forward to user
        for uid, cid in list(cfg.get("tickets", {}).items()):
            if cid == message.channel.id:
                # only forward if author is staff or owner (to avoid echoing user messages)
                try:
                    member = message.author
                    if not (is_owner_user(member) or user_has_staff_role(member)):
                        # not staff, don't forward
                        break
                except:
                    break

                # forward to user
                try:
                    user = await bot.fetch_user(int(uid))
                    emb = make_staff_embed_for_user(message.author, message.content or "")
                    files = []
                    for a in message.attachments:
                        try:
                            files.append(await a.to_file())
                        except:
                            pass
                    if files:
                        await user.send(embed=emb, files=files)
                    else:
                        await user.send(embed=emb)
                    # log forwarding
                    log_embed = discord.Embed(title="Message forwarded to user", color=discord.Color.light_grey(), timestamp=datetime.datetime.utcnow())
                    log_embed.add_field(name="From (staff)", value=str(message.author), inline=True)
                    log_embed.add_field(name="To (user)", value=uid, inline=True)
                    log_embed.add_field(name="Channel", value=message.channel.mention, inline=True)
                    await send_log_embed(message.guild, log_embed)
                except Exception:
                    try:
                        await message.channel.send("⚠️ Could not DM the user.")
                    except:
                        pass
                break

    # finally process commands as usual
    await bot.process_commands(message)

# ----------------- READY -----------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id: {bot.user.id})")
    # sync commands for the guild only (faster)
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    except Exception as e:
        print("Sync error:", e)

# ----------------- RUN -----------------
if TOKEN is None:
    print("ERROR: set DISCORD_BOT_TOKEN environment variable")
else:
    bot.run(TOKEN)
