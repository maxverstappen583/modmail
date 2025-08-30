import discord
from discord.ext import commands
from discord import app_commands
import os
import threading
from flask import Flask

# --- Flask keep-alive ---
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=10000)

threading.Thread(target=run_flask).start()

# --- Discord Bot ---
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.members = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- CONFIG (replace GUILD_ID with your server) ---
GUILD_ID = 1364371104755613837 # your server ID
OWNER_ID = 1319292111325106296 # only you can configure
ticket_category_id = None
staff_role_id = None
# --------------------------------------------------

open_tickets = {}  # user_id : channel_id


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"⚠️ Sync failed: {e}")


# /modmail command
@bot.tree.command(name="modmail", description="Open a ModMail ticket", guild=discord.Object(id=GUILD_ID))
async def modmail(interaction: discord.Interaction):
    global ticket_category_id, staff_role_id

    user = interaction.user
    guild = interaction.guild

    if guild.id != GUILD_ID:
        await interaction.response.send_message("⚠️ This command only works in the main server.", ephemeral=True)
        return

    if ticket_category_id is None:
        await interaction.response.send_message("⚠️ Ticket system not set up. Please ask the owner to set it up.", ephemeral=True)
        return

    if user.id in open_tickets:
        await interaction.response.send_message("⚠️ You already have an open ticket. Check your DMs!", ephemeral=True)
        return

    category = guild.get_channel(ticket_category_id)
    if not category or not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message("⚠️ Ticket category not valid. Please ask the owner to reset it.", ephemeral=True)
        return

    ticket_channel = await category.create_text_channel(
        name=f"ticket-{user.name}",
        topic=f"Ticket for {user} ({user.id})"
    )

    open_tickets[user.id] = ticket_channel.id

    staff_role = guild.get_role(staff_role_id) if staff_role_id else None
    ping = staff_role.mention if staff_role else "@here"

    await ticket_channel.send(f"{ping} 📬 New ticket opened by {user.mention}")
    await interaction.response.send_message("✅ Ticket created! Please check your DMs.", ephemeral=True)

    try:
        await user.send("📩 Your ticket has been opened! You can reply here to talk with staff.")
    except:
        await interaction.followup.send("⚠️ Could not DM you. Please enable DMs from server members.", ephemeral=True)


# Relay messages DM ↔ ticket
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # User DM → ticket channel
    if isinstance(message.channel, discord.DMChannel):
        if message.author.id in open_tickets:
            guild = bot.get_guild(GUILD_ID)
            channel = guild.get_channel(open_tickets[message.author.id])
            if channel:
                await channel.send(f"📩 **{message.author}**: {message.content}")
        return

    # Staff → user DM
    if message.guild and message.guild.id == GUILD_ID:
        if message.channel.id in open_tickets.values():
            user_id = next((uid for uid, cid in open_tickets.items() if cid == message.channel.id), None)
            if user_id:
                user = await bot.fetch_user(user_id)
                try:
                    await user.send(f"💬 **Staff**: {message.content}")
                except:
                    await message.channel.send("⚠️ Could not DM the user.")

    await bot.process_commands(message)


# /close command
@bot.tree.command(name="close", description="Close a ModMail ticket", guild=discord.Object(id=GUILD_ID))
async def close(interaction: discord.Interaction):
    channel = interaction.channel
    if channel.id in open_tickets.values():
        user_id = next((uid for uid, cid in open_tickets.items() if cid == channel.id), None)
        if user_id:
            open_tickets.pop(user_id, None)
            user = await bot.fetch_user(user_id)
            try:
                await user.send("✅ Your ticket has been closed by staff.")
            except:
                pass
        await channel.delete()
    else:
        await interaction.response.send_message("⚠️ This is not a ticket channel.", ephemeral=True)


# /set_category command (only you)
@bot.tree.command(name="set_category", description="Set the category for ModMail tickets", guild=discord.Object(id=GUILD_ID))
async def set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    global ticket_category_id
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ You are not authorized to use this command.", ephemeral=True)
        return

    ticket_category_id = category.id
    await interaction.response.send_message(f"✅ Ticket category set to **{category.name}**", ephemeral=True)


# /set_staffrole command (only you)
@bot.tree.command(name="set_staffrole", description="Set the staff role to ping for tickets", guild=discord.Object(id=GUILD_ID))
async def set_staffrole(interaction: discord.Interaction, role: discord.Role):
    global staff_role_id
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ You are not authorized to use this command.", ephemeral=True)
        return

    staff_role_id = role.id
    await interaction.response.send_message(f"✅ Staff role set to **{role.name}**", ephemeral=True)


bot.run(os.getenv("DISCORD_BOT_TOKEN"))
