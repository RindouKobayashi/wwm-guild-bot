import discord
from discord import app_commands
from discord.ext import commands
import logging

from utility.wwm import get_player_info, get_club_hostnums
from settings import WWM_UID, WWM_TOKEN, WWM_API_URL, logger, CLUB_ID


class WWMCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("WWM Cog loaded")

    player_group = app_commands.Group(
        name="player",
        description="WWM Player search commands"
    )

    @player_group.command(
        name="search",
        description="Search for a WWM player by their Number ID"
    )
    @app_commands.describe(
        number_id="The player's 10-digit Number ID"
    )
    async def player_search(self, interaction: discord.Interaction, number_id: str):
        await interaction.response.defer(thinking=True)

        # Validate input
        if not number_id.isdigit() or len(number_id) != 10:
            embed = discord.Embed(
                title="❌ Invalid Number ID",
                description="Number ID must be exactly 10 digits long",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        # Check if config is set
        if not WWM_UID or not WWM_TOKEN:
            embed = discord.Embed(
                title="❌ API Not Configured",
                description="WWM API credentials are not set up properly. Please contact bot owner.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            # Get player info using utility function
            raw_data = get_player_info(number_id, uid=WWM_UID, token=WWM_TOKEN, api_url=WWM_API_URL)
            
            if not raw_data:
                embed = discord.Embed(
                    title="❌ Player not found",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            # Debug: log full response structure
            logger.info(f"API Response received: {str(raw_data)}")

            # Build response embed
            embed = discord.Embed(
                title="👤 Player Profile",
                color=discord.Color.og_blurple()
            )

            # Handle different API response structures
            if isinstance(raw_data, dict):
                # Check if data is nested inside result field
                if 'result' in raw_data:
                    data = raw_data.get('result', {})
                else:
                    data = raw_data
                
                base_data = data.get('base', {})
                
                # If base is list, take first item
                if isinstance(base_data, list) and len(base_data) > 0:
                    base_data = base_data[0]
                    
                # Fallback: check if base is directly on root
                if not base_data and 'nickname' in data:
                    base_data = data
                
                # Main player info
                nickname = base_data.get('nickname', data.get('nickname', 'Unknown'))
                embed.description = f"**{nickname}**"
                
                embed.add_field(
                    name="📛 Nickname",
                    value=f"`{nickname}`",
                    inline=True
                )
                
                embed.add_field(
                    name="🏆 Level",
                    value=f"`{base_data.get('level', 0)}`",
                    inline=True
                )
                
                embed.add_field(
                    name="🆔 Number ID",
                    value=f"`{base_data.get('number_id', number_id)}`",
                    inline=True
                )
                
                embed.add_field(
                    name="⚔️ Martial Mastery",
                    value=f"`{round(base_data.get('max_xiuwei_kungfu', 0), 1)}`",
                    inline=True
                )
                
                embed.add_field(
                    name="🌍 Region",
                    value=f"`{base_data.get('oversea_tag', 'N/A')}`",
                    inline=True
                )
                
                embed.add_field(
                    name="⌛ Total Online Time",
                    value=f"`{round(base_data.get('online_time', 0) / 3600, 1)} hours`",
                    inline=True
                )
                
                # Get club info using utility function
                player_pid = data.get('id')
                if player_pid:
                    try:
                        club_data = get_club_hostnums(player_pid)
                        
                        if club_data:
                            result_data = club_data.get('result', {})
                            player_club_data = result_data.get(player_pid, {})
                            club_info = player_club_data.get('club', {})
                            player_club_id = club_info.get('club_id')
                            
                            # Check if member of our guild
                            if player_club_id == CLUB_ID:
                                member_status = "✅ **Guild Member**"
                                embed.color = discord.Color.green()
                            else:
                                member_status = "❌ Not Guild Member"
                            
                            embed.add_field(
                                name="👥 Member Status",
                                value=member_status,
                                inline=False
                            )
                            
                    except Exception as club_err:
                        logger.warning(f"Failed to get club info: {str(club_err)}")
                
            else:
                embed.description = f"```\n{str(raw_data)}\n```"

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"API Request failed: {str(e)}")
            embed = discord.Embed(
                title="❌ API Error",
                description=f"Failed to connect to WWM API: `{str(e)}`",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Player search failed: {str(e)}", exc_info=True)
            embed = discord.Embed(
                title="❌ Search Failed",
                description=f"An error occurred while searching: `{str(e)}`",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(WWMCog(bot))