# main.py
# Full ModMail + Ticket system — all commands from your screenshots included.
# Paste this file and run with Python 3.10+. Put your bot token into the TOKEN variable.
# Locked to your guild ID (so commands appear only there).

import discord
from discord.ext import commands
from discord import app_commands, ui
import asyncio
import json
import os
import tempfile
import datetime

# ========== CONFIG ==========
TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # <-- replace with your bot token
GUILD_ID = 1364371104755613837  # locked to your server
CONFIG_FILE = "config.json"
DM_COUNTDOWN = 15  # seconds countdown in DM before auto-create
FOOTER_ICON = "https://i.postimg.cc/rp5b7Jkn/IMG-6152.jpg"
FOOTER_TEXT = "@u4_straight1"
# ============================

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Ensure config file exists
if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"guilds": {}}, f, indent=4)

def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)

CFG = load_config()
if "guilds" not in CFG:
    CFG["guilds"] = {}
    save_config(CFG)

def ensure_guild_cfg(guild_id: int):
    gid = str(guild_id)
    if gid not in CFG["guilds"]:
        CFG["guilds"][gid] = {
            "category_id": None,
            "staff_role_id": None,
            "log_channel_id": None,
            "cooldown": 300,
            "tickets": {}  # user_id(str) -> channel_id(int)
        }
        save_config(CFG)
    return CFG["guilds"][gid]

# In-memory short cooldown tracking (non-persistent) to prevent spam while waiting
_last_open = {}  # (guild_id, user_id) -> timestamp

def in_cooldown(guild_id: int, user_id: int) -> int:
    key = (guild_id, user_id)
    now = asyncio.get_event_loop().time()
    last = _last_open.get(key, 0)
    cfg = ensure_guild_cfg(guild_id)
    cd = cfg.get("cooldown", 300)
    left = int(cd - (now - last))
    return max(0, left)

def start_cooldown(guild_id: int, user_id: int):
    _last_open[(guild_id, user_id)] = asyncio.get_event_loop().time()

# ---------- UTIL EMBEDS ----------
def set_footer(embed: discord.Embed):
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    return embed

def user_msg_embed(user: discord.User, content: str):
    e = discord.Embed(description=content or "‎", color=discord.Color.blurple(), timestamp=datetime.datetime.utcnow())
    e.set_author(name=f"📩 {user}", icon_url=getattr(user, "display_avatar", None).url if hasattr(user, "display_avatar") else None)
    set_footer(e)
    return e

def staff_msg_embed(member: discord.Member, content: str):
    e = discord.Embed(description=content or "‎", color=discord.Color.orange(), timestamp=datetime.datetime.utcnow())
    e.set_author(name=f"👤 {member}", icon_url=getattr(member, "display_avatar", None).url if hasattr(member, "display_avatar") else None)
    set_footer(e)
    return e

def log_embed(title: str, description: str):
    e = discord.Embed(title=title, description=description, color=discord.Color.dark_grey(), timestamp=datetime.datetime.utcnow())
    set_footer(e)
    return e

# ---------- TRANSCRIPT ----------
async def build_transcript(channel: discord.TextChannel) -> str:
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        who = f"{m.author} ({m.author.id})"
        content = m.content or ""
        lines.append(f"[{ts}] {who}: {content}")
        for a in m.attachments:
            lines.append(f"    [attachment] {a.url}")
    fd, path = tempfile.mkstemp(prefix=f"transcript_{channel.id}_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path

# ---------- LOG POSTING ----------
async def send_log(guild: discord.Guild, embed: discord.Embed, file_obj: discord.File = None):
    gcfg = ensure_guild_cfg(guild.id)
    lid = gcfg.get("log_channel_id")
    if not lid:
        return
    ch = guild.get_channel(lid)
    if not ch:
        return
    try:
        if file_obj:
            await ch.send(embed=embed, file=file_obj)
        else:
            await ch.send(embed=embed)
    except Exception:
        pass

# ---------- TICKET CREATION ----------
async def create_ticket(guild: discord.Guild, user: discord.User, opened_via: str, first_message: discord.Message = None):
    gcfg = ensure_guild_cfg(guild.id)
    cat_id = gcfg.get("category_id")
    staff_role_id = gcfg.get("staff_role_id")
    if not cat_id or not staff_role_id:
        return None, "Ticket system not configured (category or staff role missing)."

    category = guild.get_channel(cat_id)
    staff_role = guild.get_role(staff_role_id)
    if not isinstance(category, discord.CategoryChannel) or staff_role is None:
        return None, "Configured category or staff role not found."

    # Prevent duplicate ticket
    if str(user.id) in gcfg.get("tickets", {}):
        ch_id = gcfg["tickets"][str(user.id)]
        ch = guild.get_channel(ch_id)
        return ch, "You already have an open ticket."

    # Create channel hidden from everyone except staff & bot (user cannot see)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    }
    try:
        ch = await guild.create_text_channel(name=f"ticket-{user.name}", category=category, overwrites=overwrites, topic=str(user.id))
    except Exception as e:
        return None, f"Failed to create channel: {e}"

    # Save mapping
    gcfg.setdefault("tickets", {})[str(user.id)] = ch.id
    save_config(CFG)

    # Announce in ticket channel
    embed = discord.Embed(title="📩 New Ticket", description=f"Ticket opened by {user.mention}\nOpened via: {opened_via}", color=discord.Color.green(), timestamp=datetime.datetime.utcnow())
    set_footer(embed)
    await ch.send(embed=embed)

    # Forward first message (if present)
    if first_message:
        emb = user_msg_embed(user, first_message.content or "")
        files = []
        for a in first_message.attachments:
            try:
                files.append(await a.to_file())
            except:
                pass
        try:
            if files:
                await ch.send(embed=emb, files=files)
            else:
                await ch.send(embed=emb)
        except:
            pass

    # Log
    le = log_embed("Ticket Opened", f"User: {user} ({user.id})\nChannel: {ch.mention}\nOpened via: {opened_via}")
    await send_log(guild, le, None)
    # DM confirmation
    try:
        await user.send(embed=discord.Embed(title="✅ Ticket Created", description=f"Your ticket has been created in **{guild.name}**. Staff will reply here via DM.", color=discord.Color.green()).set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON))
    except:
        pass

    return ch, None

# ---------- CLOSE TICKET ----------
async def close_ticket(channel: discord.TextChannel, closed_by: discord.Member, reason: str = None):
    guild = channel.guild
    gcfg = ensure_guild_cfg(guild.id)
    # Find user id
    user_id = None
    if channel.topic and channel.topic.isdigit():
        user_id = channel.topic
    else:
        for uid, cid in gcfg.get("tickets", {}).items():
            if cid == channel.id:
                user_id = uid
                break

    # Transcript
    transcript_path = None
    try:
        transcript_path = await build_transcript(channel)
    except:
        transcript_path = None

    # Send log embed and transcript
    desc = f"Channel: {channel.name}\nClosed by: {closed_by} ({closed_by.id})"
    if reason:
        desc += f"\nReason: {reason}"
    le = log_embed("Ticket Closed", desc)
    file_obj = None
    try:
        if transcript_path:
            file_obj = discord.File(transcript_path, filename=os.path.basename(transcript_path))
    except:
        file_obj = None
    await send_log(guild, le, file_obj)

    # DM user
    if user_id:
        try:
            user = await bot.fetch_user(int(user_id))
            try:
                await user.send(f"✅ Your ticket in **{guild.name}** has been closed by staff.")
            except:
                pass
        except:
            pass
        # remove mapping
        gcfg.get("tickets", {}).pop(str(user_id), None)
        save_config(CFG)

    # delete channel
    try:
        await channel.delete()
    except:
        pass

    # cleanup transcript file
    if transcript_path:
        try:
            os.remove(transcript_path)
        except:
            pass

# ---------- DM -> Ticket flow (countdown with reactions) ----------
async def dm_countdown_then_create(user: discord.User, guild: discord.Guild, initial_message: discord.Message = None, source: str = "DM"):
    # Send confirmation embed with reactions and a countdown that updates per second.
    try:
        embed = discord.Embed(title="🎫 Create Ticket", description=f"React ✅ to create now, ❎ to cancel.\nThis will auto-create in **{DM_COUNTDOWN}** seconds.", color=discord.Color.blue())
        set_footer(embed)
        dm = await user.create_dm()
        confirm_msg = await dm.send(embed=embed)
        await confirm_msg.add_reaction("✅")
        await confirm_msg.add_reaction("❎")
    except Exception:
        return False, "Unable to DM user."

    # Wait loop per second to update the embed and check for reaction
    def check_react(reaction, reactor):
        try:
            return reactor.id == user.id and reaction.message.id == confirm_msg.id and str(reaction.emoji) in ["✅", "❎"]
        except:
            return False

    for remaining in range(DM_COUNTDOWN, 0, -1):
        # edit embed countdown
        try:
            embed.description = f"React ✅ to create now, ❎ to cancel.\nThis will auto-create in **{remaining}** seconds."
            await confirm_msg.edit(embed=embed)
        except:
            pass

        try:
            reaction, reactor = await bot.wait_for("reaction_add", timeout=1.0, check=check_react)
            if str(reaction.emoji) == "✅":
                ch, err = await create_ticket(guild, user, opened_via=source, first_message=initial_message)
                if err:
                    try: await dm.send(f"⚠️ {err}")
                    except: pass
                    return False, err
                start_cooldown(guild.id, user.id)
                return True, ch
            elif str(reaction.emoji) == "❎":
                try: await dm.send("❌ Ticket creation cancelled.")
                except: pass
                return False, "cancelled"
        except asyncio.TimeoutError:
            continue

    # Countdown finished — auto create
    ch, err = await create_ticket(guild, user, opened_via=source, first_message=initial_message)
    if err:
        try: await dm.send(f"⚠️ {err}")
        except: pass
        return False, err
    start_cooldown(guild.id, user.id)
    return True, ch

# ---------- PANEL VIEW ----------
class PanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="create_ticket_button")
    async def create_ticket_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        # Check guild configured
        gcfg = ensure_guild_cfg(interaction.guild.id)
        if not gcfg.get("category_id") or not gcfg.get("staff_role_id"):
            return await interaction.followup.send("⚠️ Ticket system not configured on this server. Admin: use /set_category and /set_staffrole.", ephemeral=True)

        left = in_cooldown(interaction.guild.id, interaction.user.id)
        if left > 0:
            return await interaction.followup.send(f"⏳ Please wait {left}s before opening another ticket.", ephemeral=True)

        # Start DM confirmation/countdown
        try:
            await interaction.followup.send("📩 Check your DMs — starting the confirmation.", ephemeral=True)
            await dm_countdown_then_create(interaction.user, interaction.guild, source="panel")
        except discord.Forbidden:
            await interaction.followup.send("❗ I can't DM you. Enable DMs and try again.", ephemeral=True)

# ---------- SLASH COMMANDS (guild only) ----------
# helper admin check
def admin_member_check(member: discord.Member) -> bool:
    return member.guild_permissions.administrator

@tree.command(name="set_category", description="Set the ticket category (Admin required)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(category="Category where tickets are created")
async def cmd_set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    if not admin_member_check(interaction.user):
        return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg = ensure_guild_cfg(interaction.guild.id)
    cfg["category_id"] = category.id
    save_config(CFG)
    await interaction.followup.send(f"✅ Ticket category set to {category.mention}", ephemeral=True)

@tree.command(name="set_logchannel", description="Set the log channel for tickets (Admin required)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(channel="Channel to post ticket logs")
async def cmd_set_logchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not admin_member_check(interaction.user):
        return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg = ensure_guild_cfg(interaction.guild.id)
    cfg["log_channel_id"] = channel.id
    save_config(CFG)
    await interaction.followup.send(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(name="set_staffrole", description="Set the staff role (Admin required)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(role="Role that will be staff")
async def cmd_set_staffrole(interaction: discord.Interaction, role: discord.Role):
    if not admin_member_check(interaction.user):
        return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg = ensure_guild_cfg(interaction.guild.id)
    cfg["staff_role_id"] = role.id
    save_config(CFG)
    await interaction.followup.send(f"✅ Staff role set to {role.mention}", ephemeral=True)

@tree.command(name="set_cooldown", description="Set per-guild ticket creation cooldown in seconds (Admin)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(seconds="Cooldown in seconds")
async def cmd_set_cooldown(interaction: discord.Interaction, seconds: int):
    if not admin_member_check(interaction.user):
        return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg = ensure_guild_cfg(interaction.guild.id)
    cfg["cooldown"] = max(0, int(seconds))
    save_config(CFG)
    await interaction.followup.send(f"✅ Cooldown set to {cfg['cooldown']} seconds", ephemeral=True)

@tree.command(name="settings", description="View ModMail settings (Admin/Staff)", guild=discord.Object(id=GUILD_ID))
async def cmd_settings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cfg = ensure_guild_cfg(interaction.guild.id)
    cat = interaction.guild.get_channel(cfg.get("category_id")) if cfg.get("category_id") else None
    sr = interaction.guild.get_role(cfg.get("staff_role_id")) if cfg.get("staff_role_id") else None
    log = interaction.guild.get_channel(cfg.get("log_channel_id")) if cfg.get("log_channel_id") else None
    e = discord.Embed(title="ModMail Settings", color=discord.Color.blurple())
    e.add_field(name="Category", value=cat.name if cat else "❌ Not set", inline=False)
    e.add_field(name="Staff Role", value=sr.mention if sr else "❌ Not set", inline=False)
    e.add_field(name="Log Channel", value=log.mention if log else "❌ Not set", inline=False)
    e.add_field(name="Cooldown", value=f"{cfg.get('cooldown', 300)}s", inline=False)
    set_footer(e)
    await interaction.followup.send(embed=e, ephemeral=True)

@tree.command(name="send_panel", description="Send a Create Ticket panel (Admin only)", guild=discord.Object(id=GUILD_ID))
async def cmd_send_panel(interaction: discord.Interaction):
    if not admin_member_check(interaction.user):
        return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg = ensure_guild_cfg(interaction.guild.id)
    if not cfg.get("category_id") or not cfg.get("staff_role_id"):
        return await interaction.followup.send("⚠️ Configure category & staff role first.", ephemeral=True)
    e = discord.Embed(title="📩 Support Panel", description="Click the button below to open a private ModMail ticket. You will chat via DM only.", color=discord.Color.green())
    set_footer(e)
    view = PanelView()
    # send panel
    await interaction.channel.send(embed=e, view=view)
    await interaction.followup.send("✅ Panel sent.", ephemeral=True)

@tree.command(name="modmail", description="Open a ModMail ticket (starts DM countdown)", guild=discord.Object(id=GUILD_ID))
async def cmd_modmail(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cfg = ensure_guild_cfg(interaction.guild.id)
    if not cfg.get("category_id") or not cfg.get("staff_role_id"):
        return await interaction.followup.send("⚠️ Ticket system not configured.", ephemeral=True)
    left = in_cooldown(interaction.guild.id, interaction.user.id)
    if left > 0:
        return await interaction.followup.send(f"⏳ Please wait {left}s before opening another ticket.", ephemeral=True)
    await interaction.followup.send("📩 Check your DMs — starting the ticket countdown.", ephemeral=True)
    try:
        await dm_countdown_then_create(interaction.user, interaction.guild, source="slash")
    except discord.Forbidden:
        await interaction.followup.send("❗ I can't DM you. Enable DMs and try again.", ephemeral=True)

@tree.command(name="close", description="Close this ticket (staff/admin)", guild=discord.Object(id=GUILD_ID))
async def cmd_close(interaction: discord.Interaction, reason: str = None):
    await interaction.response.defer(ephemeral=True)
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    cfg_g = ensure_guild_cfg(interaction.guild.id)
    staff_role = interaction.guild.get_role(cfg_g.get("staff_role_id")) if cfg_g.get("staff_role_id") else None
    if not (member and (member.guild_permissions.administrator or (staff_role and staff_role in member.roles))):
        return await interaction.followup.send("❌ You don't have permission to close tickets.", ephemeral=True)
    if str(interaction.channel.id) not in map(str, cfg_g.get("tickets", {}).values()):
        return await interaction.followup.send("⚠️ This is not a ModMail ticket channel.", ephemeral=True)
    await interaction.followup.send("🗂 Closing ticket and saving transcript...", ephemeral=True)
    await close_ticket(interaction.channel, interaction.user, reason)

@tree.command(name="refresh", description="Admin: refresh commands for this guild (clears old commands & re-sync)", guild=discord.Object(id=GUILD_ID))
async def cmd_refresh(interaction: discord.Interaction):
    if not admin_member_check(interaction.user):
        return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        # clear guild commands then sync current ones
        tree.clear_commands(guild=discord.Object(id=GUILD_ID))
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        await interaction.followup.send("✅ Guild commands refreshed (old guild commands cleared).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Refresh failed: {e}", ephemeral=True)

# Owner helper to fully clear global & guild commands and re-sync (owner only)
@bot.command(name="_owner_clear_and_refresh", hidden=True)
@commands.is_owner()
async def _owner_clear_and_refresh(ctx: commands.Context):
    await ctx.send("⏳ Clearing global & guild application commands...")
    try:
        await tree.sync(guild=None)
        await tree.clear_commands(guild=None)
    except Exception as e:
        print("clear global failed:", e)
    for g in bot.guilds:
        try:
            await tree.sync(guild=discord.Object(id=g.id))
            await tree.clear_commands(guild=discord.Object(id=g.id))
        except Exception as e:
            print(f"clear guild {g.id} failed:", e)
    await tree.sync()
    await ctx.send("✅ Cleared and re-synced commands. Old sticky commands should be gone.")

# ---------- MESSAGE FORWARDING ----------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # DMs from user -> forward into ticket (if exists) or start countdown and create ticket
    if isinstance(message.channel, discord.DMChannel):
        # if user already has a ticket in configured guild, forward
        forwarded = False
        for gid, gdata in CFG.get("guilds", {}).items():
            if str(message.author.id) in gdata.get("tickets", {}):
                g = bot.get_guild(int(gid))
                if not g: continue
                ch = g.get_channel(gdata["tickets"][str(message.author.id)])
                if not ch: continue
                emb = user_msg_embed(message.author, message.content or "")
                files = []
                for a in message.attachments:
                    try: files.append(await a.to_file())
                    except: pass
                try:
                    if files: await ch.send(embed=emb, files=files)
                    else:     await ch.send(embed=emb)
                except:
                    pass
                forwarded = True
                break
        if forwarded:
            return

        # pick the configured guild (locked to GUILD_ID)
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            try: await message.channel.send("⚠️ Ticket system unavailable.")
            except: pass
            return
        gcfg = ensure_guild_cfg(guild.id)
        if not gcfg.get("category_id") or not gcfg.get("staff_role_id"):
            try: await message.channel.send("⚠️ Ticket system not configured. Admin must run setup commands.")
            except: pass
            return

        left = in_cooldown(guild.id, message.author.id)
        if left > 0:
            try: await message.channel.send(f"⏳ Please wait {left}s before opening another ticket.")
            except: pass
            return

        # start DM countdown/create
        try:
            await dm_countdown_then_create(message.author, guild, initial_message=message, source="DM")
        except Exception:
            try: await message.channel.send("❗ Failed to start ticket process.")
            except: pass
        return

    # Guild channel message -> staff replies in ticket channel forward to user
    if message.guild:
        gcfg = ensure_guild_cfg(message.guild.id)
        tickets = gcfg.get("tickets", {})
        for uid, cid in list(tickets.items()):
            if cid == message.channel.id:
                member = message.author if isinstance(message.author, discord.Member) else None
                if not member:
                    return
                staff_role_id = gcfg.get("staff_role_id")
                role_obj = message.guild.get_role(staff_role_id) if staff_role_id else None
                # only staff/admin messages forward; ignore other messages
                if not (member.guild_permissions.administrator or (role_obj and role_obj in member.roles)):
                    return
                try:
                    user = await bot.fetch_user(int(uid))
                except:
                    return
                emb = staff_msg_embed(member, message.content or "")
                files = []
                for a in message.attachments:
                    try: files.append(await a.to_file())
                    except: pass
                try:
                    if files: await user.send(embed=emb, files=files)
                    else:     await user.send(embed=emb)
                except:
                    try: await message.channel.send("⚠️ Could not DM the user (their DMs may be closed).")
                    except: pass
                # log
                le = log_embed("Staff → User", f"{member} → {user} in {message.channel.mention}")
                await send_log(message.guild, le, None)
                return

    await bot.process_commands(message)

# ---------- READY ----------
@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user} (id: {bot.user.id})")
    # sync commands to configured guild
    try:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        print("Commands synced to guild.")
    except Exception as e:
        print("Sync failed:", e)

# ---------- RUN ----------
if __name__ == "__main__":
    if not TOKEN or TOKEN == "YOUR_BOT_TOKEN":
        print("ERROR: Put your bot token in the TOKEN variable at top of main.py")
    else:
        bot.run(TOKEN)
