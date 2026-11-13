"""Who may link to which Jellyfin user, and the line in the admin channel when
someone does. A link only steers the bot (progress, stats, DMs, and which Seerr
user /request asks as), but linked to an admin a member's requests would skip
approval, and linked to a friend they would see that friend's history."""
import discord

import config


def claim_problem(name: str, mine: str | None, linked: set[str], linkable: list[str],
                  owner: bool) -> str | None:
    """Why this member can't take the Jellyfin user name, or None if they can.
    linked is every claimed name in lower case; mine is the member's own link.
    The owner may link anyone to anything."""
    if owner:
        return None
    if name.lower() not in {n.lower() for n in linkable}:
        return f"**{name}** can only be linked by the server owner."
    if name.lower() in linked and (mine or "").lower() != name.lower():
        return f"**{name}** is linked to someone else already. If that's you, ask the server owner."
    return None


def notice(member: discord.abc.User, name: str, previous: str | None, how: str,
           by: discord.abc.User | None = None) -> str:
    text = f"🔗 {member.mention} linked as **{name}**"
    if previous and previous.lower() != name.lower():
        text += f" (was **{previous}**)"
    text += f", {how}"
    if by is not None and by.id != member.id:
        text += f", by {by.mention}"
    return text


async def announce(client, member, name: str, previous: str | None, how: str, by=None):
    """Tell the owner in the admin channel, so a wrong link gets noticed. Quiet
    without an admin channel; never stops the link itself."""
    if not config.ADMIN_CHANNEL_ID or (previous or "").lower() == name.lower():
        return
    channel = client.get_channel(config.ADMIN_CHANNEL_ID)
    if channel is None:
        return
    try:
        await channel.send(notice(member, name, previous, how, by),
                           allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        print(f"Could not post a link notice: {e}")
