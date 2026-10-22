"""Shared command checks and the slash-command error handler."""
import discord
from discord import app_commands


# --- Owner-only commands ----------------------------------------------------
# For the server owner (or the bot application's owner) only. default_permissions
# hides them from everyone without Administrator in the command list; the check
# refuses anyone else who calls them anyway.
async def is_owner(interaction: discord.Interaction) -> bool:
    if interaction.guild and interaction.user.id == interaction.guild.owner_id:
        return True
    return await interaction.client.is_owner(interaction.user)


def owner_only():
    def decorate(func):
        func = app_commands.check(is_owner)(func)
        return app_commands.default_permissions(administrator=True)(func)
    return decorate


async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        message = "That command is for the server owner only."
    else:
        print(f"Command error in /{interaction.command.name if interaction.command else '?'}: {error}")
        message = "Something went wrong running that command."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
