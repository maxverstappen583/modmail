# modmail_bot.py
import os
import sys
import json
import asyncio
import discord
from discord.ext import commands
from discord import ui
from flask import Flask
from threading import Thread
from datetime import datetime, timezone

# Optional OpenAI support
try:
    import openai
except Exception:
    openai = None

# load dotenv if present (optional)
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

if not TOKEN:
    print("❌ ERROR: No Discord token found. Please set DISCORD_TOKEN (or DISCORD_BOT_TOKEN) in environment.")
    sys.exit(1)

if OPENAI_API_KEY and openai:
    openai.api_key = OPENAI_API_KEY

# Helpful log: which env var was used
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
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

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
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v
    return d

def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=4)

data = load_data()

# -------------------------
# Helpers
# -------------------------
def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def is_staff(member: discord.Member):
    role_id = data.get("staff_role_id")
    if not role_id:
        return False
    return any(r.id == role_id for r in member.roles)

def mention_to_id(s: str):
    """Convert mention like <#id> or <@&id> or plain id to int, else None"""
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

# -------------------------
# Bot setup
# -------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------
# Ticket view (buttons)
# -------------------------
class TicketView(ui.View):
    def __init__(self, ticket_user_id: int, timeout=None):
        super().__init__(timeout=timeout)
        self.ticket_user_id = ticket_user_id

    @ui.button(label="Mark Solved", style=discord.ButtonStyle.success, emoji="✅")
    async def mark_solved(self, interaction: discord.Interaction, button: ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("This works only inside the server.", ephemeral=True)
        if not is_staff(interaction.user) and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❎ You are not staff.", ephemeral=True)

        # Notify channel and DM user
        embed = discord.Embed(
            title="✅ Problem Marked Solved",
            description=f"Marked solved by {interaction.user.mention}",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Time", value=now_ts())
        await interaction.channel.send(embed=embed)
        # DM the user linked to this ticket (if mapping exists)
        ch_id = interaction.channel.id
        user_id = None
        for uid, cid in data.get("tickets", {}).items():
            if cid == ch_id:
                user_id = int(uid)
                break
        if user_id:
            try:
                user = await bot.fetch_user(user_id)
                await user.send("✅ Your ticket has been marked **solved** by staff. If it's still an issue, reply here to re-open.")
            except Exception:
                pass
        await interaction.response.send_message("✅ Marked solved.", ephemeral=True)

    @ui.button(label="Close Ticket (delete)", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("This works only inside the server.", ephemeral=True)
        if not is_staff(interaction.user) and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❎ You are not staff.", ephemeral=True)

        await interaction.response.send_message("✅ Closing ticket — channel will be deleted in 5 seconds.", ephemeral=True)
        final_embed = discord.Embed(
            title="🗑️ Ticket Closed",
            description=f"Closed by {interaction.user.mention}",
            color=discord.Color.dark_gray(),
            timestamp=datetime.now(timezone.utc)
        )
        final_embed.add_field(name="Time", value=now_ts())
        await interaction.channel.send(embed=final_embed)

        ch_id = interaction.channel.id
        removed_uid = None
        for uid, cid in list(data.get("tickets", {}).items()):
            if cid == ch_id:
                removed_uid = uid
                break
        if removed_uid:
            data["tickets"].pop(removed_uid, None)
            save_data(data)
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except Exception:
            pass

# -------------------------
# Create ticket channel helper
# -------------------------
async def create_ticket_channel(guild: discord.Guild, user: discord.User):
    cat = guild.get_channel(data.get("category_id")) if data.get("category_id") else None
    staff_role = guild.get_role(data.get("staff_role_id")) if data.get("staff_role_id") else None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
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
    return ch

# -------------------------
# Message handling (DM -> ticket, staff -> user)
# -------------------------
@bot.event
async def on_message(message: discord.Message):
    # ensure prefix commands work
    await bot.process_commands(message)

    if message.author.bot:
        return

    # 1) DM -> create/forward to ticket channel
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
        if not channel:
            channel = await create_ticket_channel(guild, user)
            embed = discord.Embed(
                title="📩 New Ticket",
                description=f"Ticket created by {user.mention} ({user.id})",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Account Age (days)", value=str(acct_days), inline=False)
            embed.add_field(name="Created", value=now_ts(), inline=False)
            try:
                await channel.send(embed=embed, view=TicketView(ticket_user_id=user.id))
            except Exception:
                await channel.send(embed=embed)
            try:
                await user.send(f"✅ Your ticket has been created in **{guild.name}**. Staff will respond there.")
            except Exception:
                pass

        # forward content and attachments to the channel
        forward = discord.Embed(
            title=f"📨 Message from {user}",
            description=message.content or "[attachment]",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        forward.set_footer(text=f"User ID: {user.id} • {now_ts()}")
        files = []
        for att in message.attachments:
            try:
                files.append(await att.to_file())
            except Exception:
                pass
        try:
            if files:
                await channel.send(embed=forward, files=files)
            else:
                await channel.send(embed=forward)
        except Exception:
            pass
        return

    # 2) In-guild ticket channels: staff messages forwarded to user and keyword handling
    if message.guild and message.guild.id == PRIMARY_GUILD_ID:
        ch_id = message.channel.id
        tickets_map = data.get("tickets", {})
        # if this channel is one of our ticket channels
        if ch_id in list(tickets_map.values()):
            # ensure author is staff or admin to forward messages
            member = message.author
            if not is_staff(member) and not member.guild_permissions.administrator:
                return

            content = message.content or ""
            # keyword handling
            solve_kw = (data.get("solve_keyword") or "solved").lower().strip()
            close_kw = (data.get("close_keyword") or "close").lower().strip()
            lc = content.strip().lower()
            matched_solve = (lc == solve_kw) or lc.startswith(solve_kw)
            matched_close = (lc == close_kw) or lc.startswith(close_kw)
            if matched_solve:
                embed = discord.Embed(
                    title="✅ Problem Marked Solved",
                    description=f"Marked solved by {member.mention}",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
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
                        target_uid = int(uid)
                        break
                if target_uid:
                    try:
                        user = await bot.fetch_user(target_uid)
                        await user.send("✅ Your ticket has been marked solved by staff. If it's still an issue, reply here to re-open.")
                    except Exception:
                        pass
                return

            if matched_close:
                embed = discord.Embed(
                    title="🗑️ Ticket Closed",
                    description=f"Closed by {member.mention}",
                    color=discord.Color.dark_gray(),
                    timestamp=datetime.now(timezone.utc)
                )
                embed.add_field(name="Time", value=now_ts())
                await message.channel.send(embed=embed)
                # remove mapping and delete channel
                removed_uid = None
                for uid, cid in list(tickets_map.items()):
                    if cid == ch_id:
                        removed_uid = uid
                        break
                if removed_uid:
                    data["tickets"].pop(removed_uid, None)
                    save_data(data)
                await asyncio.sleep(3)
                try:
                    await message.channel.delete(reason=f"Closed by keyword by {member}")
                except Exception:
                    pass
                return

            # If not a keyword, forward the staff message to the user (including attachments)
            target_uid = None
            for uid, cid in tickets_map.items():
                if cid == ch_id:
                    target_uid = int(uid)
                    break
            if target_uid:
                try:
                    user = await bot.fetch_user(target_uid)
                    # build embed
                    staff_forward = discord.Embed(
                        title=f"Reply from staff ({member.display_name})",
                        description=content or "[attachment]",
                        color=discord.Color.blue(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    staff_forward.set_footer(text=f"Staff: {member} • {now_ts()}")
                    files = []
                    for att in message.attachments:
                        try:
                            files.append(await att.to_file())
                        except Exception:
                            pass
                    if files:
                        await user.send(embed=staff_forward, files=files)
                    else:
                        await user.send(embed=staff_forward)
                    # optional: confirm to staff
                    try:
                        await message.add_reaction("✅")
                    except Exception:
                        pass
                except Exception:
                    # Could not DM user (disabled DMs or blocked) — inform staff
                    try:
                        await message.channel.send(f"❌ Could not DM the user (DMS may be closed).")
                    except Exception:
                        pass
                return

# -------------------------
# Slash commands (guild-only) + prefix equivalents
# -------------------------
# Setup: set category and staff role
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
    # category_arg can be id or mention like <#id>; staff_role_arg can be id or mention like <@&id>
    cat_id = mention_to_id(category_arg) or None
    role_id = mention_to_id(staff_role_arg) or None
    if not cat_id or not role_id:
        return await ctx.send("❌ Invalid args. Use `!setup <category_id|#mention> <role_id|@mention>`")
    data["category_id"] = int(cat_id)
    data["staff_role_id"] = int(role_id)
    save_data(data)
    await ctx.send(f"✅ Category and staff role set. Category ID: `{cat_id}`, Role ID: `{role_id}`")

# Settings: show current settings
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

# Set solve keyword
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

# Set close keyword
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

# List tickets
@bot.tree.command(name="list_tickets", description="List open tickets (admin only).")
async def list_tickets_slash(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❎ Admins only.", ephemeral=True)
    tickets = data.get("tickets", {})
    if not tickets:
        return await interaction.response.send_message("❎ No open tickets.", ephemeral=True)
    lines = []
    for uid, cid in tickets.items():
        lines.append(f"User `{uid}` → Channel ID `{cid}`")
    await interaction.response.send_message("Open tickets:\n" + "\n".join(lines), ephemeral=True)

@bot.command(name="list_tickets")
@commands.has_permissions(administrator=True)
async def list_tickets_prefix(ctx: commands.Context):
    tickets = data.get("tickets", {})
    if not tickets:
        return await ctx.send("❎ No open tickets.")
    lines = []
    for uid, cid in tickets.items():
        lines.append(f"User `{uid}` → Channel ID `{cid}`")
    await ctx.send("Open tickets:\n" + "\n".join(lines))

# Force close (in-channel)
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
            to_remove = uid
            break
    if to_remove:
        data["tickets"].pop(to_remove, None)
        save_data(data)
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
            to_remove = uid
            break
    if to_remove:
        data["tickets"].pop(to_remove, None)
        save_data(data)
    await asyncio.sleep(4)
    try:
        await ch.delete(reason=f"Force-closed by {ctx.author}")
    except Exception:
        pass

# -------------------------
# Owner-only: refresh & restart (prefix + slash)
# -------------------------
@bot.command(name="refresh")
async def refresh_prefix(ctx: commands.Context):
    if ctx.author.id != OWNER_ID:
        return await ctx.send("❎ Owner only.")
    try:
        guild_obj = discord.Object(id=PRIMARY_GUILD_ID)
        synced = await bot.tree.sync(guild=guild_obj)
        msg = f"✅ Refreshed {len(synced)} commands for guild {PRIMARY_GUILD_ID}"
        print(msg)
        await ctx.send(msg)
    except Exception as e:
        err = f"❌ Refresh failed: {e}"
        print(err)
        await ctx.send(err)

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

# -------------------------
# on_ready: sync to guild & DM owner
# -------------------------
@bot.event
async def on_ready():
    # sync only to your guild (instant)
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
    # DM owner where possible
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
