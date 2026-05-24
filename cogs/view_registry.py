"""
Central registry for persistent discord.py views.
Cogs self-register their persistent views here, and setup_hook
calls register_all_views(bot) to register them all.

This avoids having to manually edit guildbot.py for every new
persistent view.
"""
_registry = []


def register(view_factory, *args, **kwargs):
    """Register a persistent view factory for bot startup.

    Args:
        view_factory: The View/LayoutView class to instantiate.
        *args, **kwargs: Passed straight into view_factory().
    """
    _registry.append((view_factory, args, kwargs))


def register_all_views(bot):
    """Call this from setup_hook to register all collected views."""
    from settings import logger

    for factory, args, kwargs in _registry:
        try:
            instance = factory(*args, **kwargs)
            bot.add_view(instance)
            logger.debug(f"Registered persistent view: {factory.__name__}")
        except Exception as e:
            logger.error(f"Failed to register view {factory.__name__}: {e}")

    if _registry:
        logger.info(f"Registered {len(_registry)} persistent view(s)")
    else:
        logger.info("No persistent views registered")