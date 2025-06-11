"""
Render.com specific configuration for the Discord bot
Complete implementation for handling cloud platform rate limiting
"""

import os
import asyncio
import random
import socket
import platform
import discord
from discord.ext import commands
import datetime


def is_render_platform():
    """Detect if running on Render platform"""
    return (
            os.getenv('RENDER') is not None or
            'render' in socket.gethostname().lower() or
            os.getenv('RENDER_SERVICE_ID') is not None or
            os.getenv('RENDER_SERVICE_NAME') is not None
    )


def is_cloud_platform():
    """Detect if running on any cloud platform"""
    hostname = socket.gethostname().lower()
    cloud_indicators = ['render', 'heroku', 'railway', 'fly', 'replit', 'glitch']

    return (
            any(cloud in hostname for cloud in cloud_indicators) or
            os.getenv('RENDER') is not None or
            os.getenv('DYNO') is not None or  # Heroku
            os.getenv('RAILWAY_ENVIRONMENT') is not None or  # Railway
            os.getenv('FLY_APP_NAME') is not None or  # Fly.io
            (platform.system() == 'Linux' and os.getenv('HOME', '').startswith('/app'))
    )


def configure_for_render(bot, rate_limiter=None):
    """Configure bot settings specifically for Render platform"""

    is_render = is_render_platform()
    is_cloud = is_cloud_platform()

    if not (is_render or is_cloud):
        print("💻 Local development environment detected")
        return False

    platform_name = "Render" if is_render else "Cloud Platform"
    print(f"🌐 {platform_name} detected - applying cloud optimizations...")

    # Override Discord.py's default settings for cloud platforms
    try:
        # Increase timeouts globally
        if hasattr(discord.http, 'HTTPClient'):
            original_init = discord.http.HTTPClient.__init__

            def patched_init(self, connector=None, *, proxy=None, proxy_auth=None, loop=None, unsync_clock=True):
                original_init(self, connector=connector, proxy=proxy, proxy_auth=proxy_auth, loop=loop,
                              unsync_clock=unsync_clock)
                # Apply cloud-friendly settings
                self.timeout = 60.0  # 60 second timeout
                self.max_retries = 2  # Reduce retries to prevent hammering

            discord.http.HTTPClient.__init__ = patched_init
            print("✅ Discord HTTP client patched for cloud platform")
    except Exception as e:
        print(f"⚠️ Could not patch HTTP client: {e}")

    # Configure rate limiter for cloud platforms
    if rate_limiter:
        print("🔧 Applying cloud-specific rate limiting...")

        # Ultra-conservative settings for cloud platforms
        rate_limiter.rate_limits = {
            'role_modification': {
                'requests_per_second': 1,  # 1 request per second max
                'burst_limit': 2,  # Max 2 burst requests
                'delay_between_requests': 3.0,  # 3 second delay between requests
                'backoff_multiplier': 3.0,  # Aggressive backoff
                'max_backoff': 120.0  # Up to 2 minute backoff
            },
            'member_fetch': {
                'requests_per_second': 3,  # 3 requests per second max
                'burst_limit': 5,  # Max 5 burst requests
                'delay_between_requests': 1.0,  # 1 second delay
                'backoff_multiplier': 2.5,
                'max_backoff': 60.0
            },
            'message_send': {
                'requests_per_second': 2,  # 2 messages per second max
                'burst_limit': 3,  # Max 3 burst messages
                'delay_between_requests': 2.0,  # 2 second delay
                'backoff_multiplier': 2.5,
                'max_backoff': 60.0
            },
            'guild_operations': {
                'requests_per_second': 0.5,  # 1 request every 2 seconds
                'burst_limit': 1,  # No burst for guild operations
                'delay_between_requests': 5.0,  # 5 second delay
                'backoff_multiplier': 4.0,
                'max_backoff': 180.0  # Up to 3 minute backoff
            }
        }

        # Enable more conservative jitter
        rate_limiter.use_jitter = True
        print("✅ Cloud-optimized rate limiting applied")

    return True


async def render_startup_sequence(bot):
    """Special startup sequence for cloud platforms to avoid rate limiting"""

    if not is_cloud_platform():
        return

    print("🚀 Starting cloud platform startup sequence...")

    # Extended initial delay for cloud platforms
    initial_delay = random.uniform(15.0, 30.0)
    print(f"⏳ Initial cloud startup delay: {initial_delay:.1f}s")
    await asyncio.sleep(initial_delay)

    # Test Discord API connectivity with timeout
    print("🔗 Testing Discord API connectivity...")
    try:
        # Create a simple HTTP client to test connectivity
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get('https://discord.com/api/v10/gateway') as response:
                if response.status == 200:
                    print("✅ Discord API connectivity confirmed")
                else:
                    print(f"⚠️ Discord API returned status {response.status}")
                    await asyncio.sleep(10.0)

    except asyncio.TimeoutError:
        print("⚠️ Discord API connectivity test timed out")
        await asyncio.sleep(30.0)
    except Exception as e:
        print(f"⚠️ Discord API connectivity test failed: {e}")
        await asyncio.sleep(20.0)

    print("✅ Cloud platform startup sequence complete")


async def render_safe_sync(bot):
    """Safely sync commands on cloud platforms with extreme precautions"""

    if not is_cloud_platform():
        print("💻 Using standard command sync for local development")
        return False

    print("🔄 Starting cloud-safe command synchronization...")

    try:
        # Global command sync with extended delay and retries
        print("📡 Syncing global commands...")

        global_sync_delay = random.uniform(10.0, 20.0)
        print(f"⏳ Pre-sync delay: {global_sync_delay:.1f}s")
        await asyncio.sleep(global_sync_delay)

        # Try global sync with timeout and retries
        for attempt in range(3):
            try:
                await asyncio.wait_for(bot.tree.sync(), timeout=60.0)
                print("✅ Global commands synced successfully")
                break
            except asyncio.TimeoutError:
                print(f"⏰ Global sync timeout (attempt {attempt + 1}/3)")
                if attempt < 2:
                    await asyncio.sleep(30.0)
                else:
                    print("❌ Global sync failed after 3 attempts")
                    return False
            except discord.HTTPException as e:
                if e.status == 429:
                    retry_after = getattr(e, 'retry_after', 60)
                    print(f"🚫 Global sync rate limited, waiting {retry_after + 30}s")
                    await asyncio.sleep(retry_after + 30)
                    if attempt < 2:
                        continue
                    else:
                        print("❌ Global sync failed due to persistent rate limiting")
                        return False
                else:
                    print(f"❌ Global sync HTTP error: {e}")
                    return False
            except Exception as e:
                print(f"❌ Global sync unexpected error: {e}")
                if attempt < 2:
                    await asyncio.sleep(20.0)
                else:
                    return False

        # Post-global-sync delay
        await asyncio.sleep(random.uniform(15.0, 25.0))

        # Guild command sync with ultra-conservative approach
        print("🏰 Starting guild command synchronization...")

        guilds = list(bot.guilds)
        total_guilds = len(guilds)
        print(f"📊 Syncing commands to {total_guilds} guild(s)")

        for i, guild in enumerate(guilds):
            try:
                # Massive delay between guild syncs for cloud platforms
                if i > 0:
                    delay = random.uniform(45.0, 90.0)  # 45-90 second delay between guilds!
                    print(f"⏳ Guild sync delay ({i}/{total_guilds}): {delay:.1f}s")
                    await asyncio.sleep(delay)

                print(f"🔄 Syncing commands to guild: {guild.name} ({i + 1}/{total_guilds})")

                # Try guild sync with extended timeout
                try:
                    await asyncio.wait_for(bot.tree.sync(guild=guild), timeout=90.0)
                    print(f"✅ Successfully synced to guild: {guild.name}")

                    # Post-sync delay
                    await asyncio.sleep(random.uniform(10.0, 20.0))

                except asyncio.TimeoutError:
                    print(f"⏰ Timeout syncing to guild: {guild.name} - skipping")
                    continue

                except discord.HTTPException as e:
                    if e.status == 429:
                        retry_after = getattr(e, 'retry_after', 90)
                        total_wait = retry_after + random.uniform(30.0, 60.0)
                        print(f"🚫 Rate limited syncing to {guild.name}, waiting {total_wait:.1f}s")
                        await asyncio.sleep(total_wait)

                        # Single retry attempt
                        try:
                            await asyncio.wait_for(bot.tree.sync(guild=guild), timeout=90.0)
                            print(f"✅ Retry successful for guild: {guild.name}")
                        except Exception as retry_error:
                            print(f"❌ Retry failed for guild {guild.name}: {retry_error}")
                    else:
                        print(f"❌ HTTP error syncing to guild {guild.name}: {e}")

                except Exception as e:
                    print(f"❌ Unexpected error syncing to guild {guild.name}: {e}")

            except Exception as outer_e:
                print(f"❌ Critical error processing guild {guild.name}: {outer_e}")
                continue

        print("✅ Cloud-safe command synchronization complete")
        return True

    except Exception as e:
        print(f"❌ Critical error in cloud-safe sync: {e}")
        return False


class RenderErrorHandler:
    """Enhanced error handler specifically designed for cloud platforms"""

    @staticmethod
    async def handle_rate_limit(interaction, operation_name="operation"):
        """Handle rate limiting errors with cloud platform awareness"""
        platform_name = "Render" if is_render_platform() else "cloud platform"

        try:
            error_message = (
                f"⚠️ **Cloud Platform Rate Limiting Detected**\n\n"
                f"The bot is experiencing rate limiting on {platform_name}. "
                f"Your {operation_name} may have completed successfully, but the response was delayed.\n\n"
                f"**What to do:**\n"
                f"• Wait 3-5 minutes before trying again\n"
                f"• Check if your action completed (use `/status` or `/rank`)\n"
                f"• This is normal for cloud-hosted bots during high usage"
            )

            if interaction.response.is_done():
                await interaction.followup.send(error_message, ephemeral=True)
            else:
                await interaction.response.send_message(error_message, ephemeral=True)

        except Exception as e:
            print(f"🚫 Completely rate limited - could not send error message for {operation_name}: {e}")

    @staticmethod
    async def handle_timeout(interaction, operation_name="operation"):
        """Handle timeout errors with cloud platform context"""
        platform_name = "Render" if is_render_platform() else "cloud platform"

        try:
            error_message = (
                f"⏰ **{operation_name.title()} Timed Out**\n\n"
                f"The request timed out due to {platform_name} limitations. "
                f"This is common on cloud platforms.\n\n"
                f"**Your {operation_name} may have completed successfully!**\n\n"
                f"Please check manually:\n"
                f"• Use `/status` to check queue status\n"
                f"• Use `/rank` to check your stats\n"
                f"• Wait a few minutes before retrying"
            )

            if interaction.response.is_done():
                await interaction.followup.send(error_message, ephemeral=True)
            else:
                await interaction.response.send_message(error_message, ephemeral=True)

        except Exception as e:
            print(f"⏰ Timeout occurred and could not send error message for {operation_name}: {e}")

    @staticmethod
    async def handle_general_error(interaction, error, operation_name="operation"):
        """Handle general errors with cloud platform context"""
        platform_name = "Render" if is_render_platform() else "cloud platform"

        try:
            if isinstance(error, discord.HTTPException):
                if error.status == 429:
                    await RenderErrorHandler.handle_rate_limit(interaction, operation_name)
                    return
                elif error.status == 503:
                    error_message = f"🔧 Discord API is temporarily unavailable. Please try your {operation_name} again in a few minutes."
                elif error.status == 502:
                    error_message = f"🌐 {platform_name} is experiencing connectivity issues. Your {operation_name} may have completed successfully."
                else:
                    error_message = f"❌ API Error {error.status}: {str(error)}"
            elif isinstance(error, asyncio.TimeoutError):
                await RenderErrorHandler.handle_timeout(interaction, operation_name)
                return
            else:
                error_message = f"❌ Unexpected error during {operation_name}: {str(error)}"

            if interaction.response.is_done():
                await interaction.followup.send(error_message, ephemeral=True)
            else:
                await interaction.response.send_message(error_message, ephemeral=True)

        except Exception as handler_error:
            print(f"❌ Error handler failed for {operation_name}: {handler_error}")


async def cloud_safe_defer(interaction, ephemeral=False, max_retries=3):
    """Safely defer an interaction on cloud platforms"""
    for attempt in range(max_retries):
        try:
            await interaction.response.defer(ephemeral=ephemeral)
            return True
        except discord.HTTPException as e:
            if e.status == 429 and attempt < max_retries - 1:
                retry_after = getattr(e, 'retry_after', 5)
                if is_cloud_platform():
                    retry_after *= 2  # Double wait time on cloud
                await asyncio.sleep(retry_after)
                continue
            else:
                print(f"❌ Failed to defer interaction: {e}")
                return False
        except Exception as e:
            print(f"❌ Unexpected error deferring interaction: {e}")
            return False

    return False


async def cloud_safe_followup(interaction, content=None, embed=None, ephemeral=False, max_retries=3):
    """Safely send a followup message on cloud platforms"""
    for attempt in range(max_retries):
        try:
            if is_cloud_platform():
                # Add delay before sending on cloud platforms
                await asyncio.sleep(random.uniform(0.5, 2.0))

            return await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)

        except discord.HTTPException as e:
            if e.status == 429 and attempt < max_retries - 1:
                retry_after = getattr(e, 'retry_after', 10)
                if is_cloud_platform():
                    retry_after *= 2
                jitter = random.uniform(0, retry_after * 0.3)
                total_wait = retry_after + jitter
                print(f"⚠️ Followup rate limited, waiting {total_wait:.1f}s")
                await asyncio.sleep(total_wait)
                continue
            else:
                raise
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) * random.uniform(1.0, 3.0)
                if is_cloud_platform():
                    wait_time *= 2
                await asyncio.sleep(wait_time)
                continue
            else:
                raise

    raise Exception(f"Failed to send followup after {max_retries} attempts")


# Utility functions for checking platform
def get_platform_info():
    """Get detailed platform information for debugging"""
    return {
        'is_render': is_render_platform(),
        'is_cloud': is_cloud_platform(),
        'hostname': socket.gethostname(),
        'platform': platform.system(),
        'environment_vars': {
            'RENDER': os.getenv('RENDER'),
            'RENDER_SERVICE_ID': os.getenv('RENDER_SERVICE_ID'),
            'DYNO': os.getenv('DYNO'),
            'RAILWAY_ENVIRONMENT': os.getenv('RAILWAY_ENVIRONMENT'),
            'FLY_APP_NAME': os.getenv('FLY_APP_NAME'),
        }
    }


# Export all functions and classes
__all__ = [
    'configure_for_render',
    'render_startup_sequence',
    'render_safe_sync',
    'RenderErrorHandler',
    'cloud_safe_defer',
    'cloud_safe_followup',
    'is_render_platform',
    'is_cloud_platform',
    'get_platform_info'
]