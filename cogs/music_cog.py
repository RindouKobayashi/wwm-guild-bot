import discord
import settings
import asyncio
import os # Added
import io # Added
import re
from discord.ext import commands
from discord import app_commands
from settings import logger
import yt_dlp

class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_clients = {}  # Dictionary to keep track of voice clients per guild
        self.queues = {}  # Dictionary to keep track of song queues per guild
        self.now_playing = {}  # Dictionary to track currently playing song per guild
        self.vote_skips = {}  # Dictionary to track skip votes per guild
        self.active_selection_tasks = {}  # Dictionary to track active user selection tasks
        self.url_regex = re.compile(r'https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_\+.~#?&//=]*)')

    def _format_duration(self, seconds: int) -> str:
        """Convert seconds to MM:SS format"""
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes}:{seconds:02d}"

    def _is_admin(self, member: discord.Member) -> bool:
        """Check if member has administrator permissions"""
        return member.guild_permissions.administrator

    def _is_allowed_channel(self, channel_id: int) -> bool:
        """Check if command is being used in an allowed bot channel"""
        ALLOWED_CHANNELS = {414234388776353828, 1463479585567150194, 1482369748015513630}
        return channel_id in ALLOWED_CHANNELS

    async def _delete_temp_file(self, audio_file: str):
        """Async function to delete temp file with proper retries for Windows file locks"""
        max_retries = 10
        for attempt in range(max_retries):
            try:
                if os.path.exists(audio_file):
                    os.remove(audio_file)
                return
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.3)
                else:
                    logger.warning(f"Could not delete temp file {audio_file} after {max_retries} attempts: {e}")

    async def _play_song(self, guild_id: int, audio_file: str, song_title: str):
        """Internal function to handle actual song playback"""
        voice_client = self.voice_clients[guild_id]
        
        def after_playback(error):
            if error:
                logger.error(f"Playback error: {error}")
            # Schedule async cleanup and next song
            asyncio.run_coroutine_threadsafe(self._delete_temp_file(audio_file), self.bot.loop)
            asyncio.run_coroutine_threadsafe(self.play_next(guild_id), self.bot.loop)

        voice_client.play(discord.FFmpegPCMAudio(source=audio_file), after=after_playback)

    async def play_next(self, guild_id: int):
        """Play the next song in the queue"""
        if guild_id not in self.queues or len(self.queues[guild_id]) == 0:
            # No more songs, disconnect
            voice_client = self.voice_clients.get(guild_id)
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect()
                del self.voice_clients[guild_id]
            if guild_id in self.queues:
                del self.queues[guild_id]
            return

        # Get next song from queue
        song = self.queues[guild_id].pop(0)
        await self._play_song(guild_id, song['file'], song['title'])

    @app_commands.command(name="play", description="Play a song from YouTube")
    @app_commands.describe(query="YouTube URL or song name to search for")
    async def play(self, interaction: discord.Interaction, query: str):
        # Check if channel is allowed
        if not self._is_allowed_channel(interaction.channel_id):
            await interaction.response.send_message("❌ Music commands can only be used in bot channels!", ephemeral=True)
            return
            
        # Check if user is in a voice channel
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message("❌ You need to be in a voice channel to use this command!", ephemeral=True)
            return

        user_channel = interaction.user.voice.channel
        guild_id = interaction.guild.id
        
        # Check if bot is already in a different voice channel
        voice_client = self.voice_clients.get(guild_id)
        if voice_client is not None and voice_client.channel != user_channel:
            await interaction.response.send_message(f"❌ I'm already in {voice_client.channel.mention}! Join that channel or wait for me to finish.", ephemeral=True)
            return

        await interaction.response.defer()

        user_id = interaction.user.id
        # Cancel any previous active selection immediately for this user before proceeding
        if user_id in self.active_selection_tasks:
            self.active_selection_tasks[user_id].cancel()
            del self.active_selection_tasks[user_id]

        # Check if input is a URL or search query
        if self.url_regex.match(query):
            # Direct URL mode
            download_url = query
        else:
            # Search mode - get top 5 results
            await interaction.followup.send(f"🔍 Searching for **{query}**...")
            
            search_options = {
                'format': 'bestaudio/best',
                'quiet': True,
                'no_warnings': True,
                'extract_flat': 'in_playlist',
                'playlistend': 5,
            }
            
            with yt_dlp.YoutubeDL(search_options) as ytdlp:
                search_results = ytdlp.extract_info(f"ytsearch5:{query}", download=False)
            
            if not search_results or 'entries' not in search_results or len(search_results['entries']) == 0:
                await interaction.edit_original_response(content="❌ No results found for your search")
                return
            
            
            # Build selection menu
            results = search_results['entries']
            selection_text = "🎵 Found these results:\n"
            
            for idx, entry in enumerate(results, 1):
                selection_text += f"**{idx}.** {entry.get('title', 'Unknown Title')}\n"
            
            selection_text += "\nReply with a number 1-5 to select, or **6** to cancel (30 seconds timeout)"
            await interaction.edit_original_response(content=selection_text)
            
            # Wait for user selection
            def check(m):
                return m.author == interaction.user and m.channel == interaction.channel and m.content.isdigit() and 1 <= int(m.content) <= 6
            
            try:
                # Create and store task so we can cancel it later
                wait_task = asyncio.create_task(self.bot.wait_for('message', timeout=30.0, check=check))
                self.active_selection_tasks[user_id] = wait_task
                
                reply = await wait_task
            except asyncio.TimeoutError:
                if user_id in self.active_selection_tasks:
                    del self.active_selection_tasks[user_id]
                await interaction.edit_original_response(content="⏱️ Selection timed out")
                return
            except asyncio.CancelledError:
                # Selection was cancelled by new /play command
                await interaction.edit_original_response(content="❌ Selection cancelled (new search started)")
                return
            finally:
                if user_id in self.active_selection_tasks:
                    del self.active_selection_tasks[user_id]
            
            selection = int(reply.content)
            if selection == 6:
                await reply.delete()
                await interaction.edit_original_response(content="❌ Selection cancelled")
                return
            
            selected_idx = selection - 1
            download_url = results[selected_idx]['url']
            await reply.delete()
            await interaction.edit_original_response(content=f"✅ Selected: **{results[selected_idx]['title']}**")

        # Ensure data directory exists
        os.makedirs('data', exist_ok=True)
        
        # Download the audio from the YouTube video using yt-dlp
        ytdlp_options = {
            'format': 'bestaudio/best',
            'outtmpl': f'data/temp_audio_{guild_id}_%(epoch)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ytdlp_options) as ytdlp:
            info = ytdlp.extract_info(download_url, download=True)
            audio_file = ytdlp.prepare_filename(info)
            song_title = info.get('title', query)

        # Initialize queue if not exists
        if guild_id not in self.queues:
            self.queues[guild_id] = []

        # Connect to voice channel if not already connected
        if voice_client is None:
            voice_client = await user_channel.connect()
            self.voice_clients[guild_id] = voice_client

        # Create song entry
        song_entry = {
            'title': song_title,
            'file': audio_file,
            'requester': interaction.user.display_name,
            'requester_id': interaction.user.id,
            'duration': info.get('duration', 0),
            'url': download_url
        }

        # Initialize queue if not exists
        if guild_id not in self.queues:
            self.queues[guild_id] = []

        # Check if already playing
        if voice_client.is_playing() or voice_client.is_paused():
            # Add to queue
            self.queues[guild_id].append(song_entry)
            await interaction.edit_original_response(content=f"🎵 Added to queue: **{song_title}** (Position: {len(self.queues[guild_id])})")
            return

        # Play immediately if nothing is playing
        self.now_playing[guild_id] = song_entry
        await interaction.edit_original_response(content=f"🎵 Now playing: **{song_title}** [{self._format_duration(song_entry['duration'])}]")
        
        # Play the song
        await self._play_song(guild_id, audio_file, song_title)

    @app_commands.command(name="queue", description="View the current song queue")
    async def queue(self, interaction: discord.Interaction):
        if not self._is_allowed_channel(interaction.channel_id):
            await interaction.response.send_message("❌ Music commands can only be used in bot channels!", ephemeral=True)
            return
            
        guild_id = interaction.guild.id
        
        message = ""
        
        # Show currently playing
        if guild_id in self.now_playing:
            current = self.now_playing[guild_id]
            message += "🎶 **NOW PLAYING:**\n"
            message += f"▶️ {current['title']} `[{self._format_duration(current['duration'])}]` | requested by **{current['requester']}**\n\n"
        
        # Show queue
        if guild_id not in self.queues or len(self.queues[guild_id]) == 0:
            message += "📭 No songs in queue"
        else:
            queue_list = self.queues[guild_id]
            total_duration = sum(song['duration'] for song in queue_list)
            
            message += f"📋 **UP NEXT** ({len(queue_list)} songs | Total: {self._format_duration(total_duration)}):\n"
            message += "---\n"
            
            for idx, song in enumerate(queue_list, 1):
                message += f"**{idx}.** {song['title']} `[{self._format_duration(song['duration'])}]` | requested by **{song['requester']}**\n"
        
        await interaction.response.send_message(message)

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        if not self._is_allowed_channel(interaction.channel_id):
            await interaction.response.send_message("❌ Music commands can only be used in bot channels!", ephemeral=True)
            return
            
        guild_id = interaction.guild.id
        
        # Validation checks
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message("❌ You need to be in a voice channel!", ephemeral=True)
            return
            
        voice_client = self.voice_clients.get(guild_id)
        if voice_client is None or not voice_client.is_playing():
            await interaction.response.send_message("❌ Nothing is playing right now", ephemeral=True)
            return
            
        user_channel = interaction.user.voice.channel
        if voice_client.channel != user_channel:
            await interaction.response.send_message("❌ You need to be in the same voice channel as me!", ephemeral=True)
            return

        current_song = self.now_playing.get(guild_id)
        if not current_song:
            await interaction.response.send_message("❌ No song currently playing", ephemeral=True)
            return

        # Immediate skip for requester or admin
        if interaction.user.id == current_song['requester_id'] or self._is_admin(interaction.user):
            await interaction.response.send_message("✅ Skipping song...")
            voice_client.stop()
            return

        # Vote skip for others
        vc_members = len([m for m in user_channel.members if not m.bot])
        required_votes = (vc_members // 2) + 1

        # Initialize vote if not exists
        if guild_id not in self.vote_skips:
            self.vote_skips[guild_id] = set()

        if interaction.user.id in self.vote_skips[guild_id]:
            await interaction.response.send_message("✅ You already voted to skip!", ephemeral=True)
            return

        self.vote_skips[guild_id].add(interaction.user.id)
        vote_count = len(self.vote_skips[guild_id])

        if vote_count >= required_votes:
            await interaction.response.send_message(f"✅ Vote passed! {vote_count}/{required_votes} votes. Skipping song...")
            del self.vote_skips[guild_id]
            voice_client.stop()
        else:
            await interaction.response.send_message(f"🗳️ Skip vote: {vote_count}/{required_votes} needed. Use /skip again to vote.")

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        if not self._is_allowed_channel(interaction.channel_id):
            await interaction.response.send_message("❌ Music commands can only be used in bot channels!", ephemeral=True)
            return
            
        guild_id = interaction.guild.id
        voice_client = self.voice_clients.get(guild_id)
        
        if voice_client and voice_client.is_playing():
            voice_client.pause()
            await interaction.response.send_message("⏸️ Playback paused")
        else:
            await interaction.response.send_message("❌ Nothing is playing right now", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        if not self._is_allowed_channel(interaction.channel_id):
            await interaction.response.send_message("❌ Music commands can only be used in bot channels!", ephemeral=True)
            return
            
        guild_id = interaction.guild.id
        voice_client = self.voice_clients.get(guild_id)
        
        if voice_client and voice_client.is_paused():
            voice_client.resume()
            await interaction.response.send_message("▶️ Playback resumed")
        else:
            await interaction.response.send_message("❌ Nothing is paused right now", ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and clear queue (ADMIN ONLY)")
    async def stop(self, interaction: discord.Interaction):
        if not self._is_allowed_channel(interaction.channel_id):
            await interaction.response.send_message("❌ Music commands can only be used in bot channels!", ephemeral=True)
            return
            
        if not self._is_admin(interaction.user):
            await interaction.response.send_message("❌ Only administrators can use this command", ephemeral=True)
            return
            
        guild_id = interaction.guild.id
        voice_client = self.voice_clients.get(guild_id)
        
        if guild_id in self.queues:
            del self.queues[guild_id]
        if guild_id in self.now_playing:
            del self.now_playing[guild_id]
            
        if voice_client:
            voice_client.stop()
            # Cleanup temp files on stop
            try:
                # Cleanup currently playing file with retries
                if guild_id in self.now_playing:
                    audio_file = self.now_playing[guild_id]['file']
                    max_retries = 5
                    for attempt in range(max_retries):
                        try:
                            if os.path.exists(audio_file):
                                os.remove(audio_file)
                            break
                        except Exception:
                            if attempt < max_retries - 1:
                                asyncio.sleep(0.2)
                # Cleanup all queued files
                if guild_id in self.queues:
                    for song in self.queues[guild_id]:
                        if os.path.exists(song['file']):
                            try:
                                os.remove(song['file'])
                            except Exception:
                                pass
            except Exception as e:
                logger.warning(f"Could not cleanup temp files: {e}")
            await voice_client.disconnect()
            del self.voice_clients[guild_id]
            
        await interaction.response.send_message("⏹️ Playback stopped, queue cleared")

    @app_commands.command(name="clear", description="Clear the queue (ADMIN ONLY)")
    async def clear(self, interaction: discord.Interaction):
        if not self._is_allowed_channel(interaction.channel_id):
            await interaction.response.send_message("❌ Music commands can only be used in bot channels!", ephemeral=True)
            return
            
        if not self._is_admin(interaction.user):
            await interaction.response.send_message("❌ Only administrators can use this command", ephemeral=True)
            return
            
        guild_id = interaction.guild.id
        if guild_id in self.queues:
            del self.queues[guild_id]
        await interaction.response.send_message("🗑️ Queue cleared")
    
async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
