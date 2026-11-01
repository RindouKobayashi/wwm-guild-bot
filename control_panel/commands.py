"""Public discord.py command hooks; names/outcomes only, never arguments or users."""
import time
import uuid
from discord import app_commands
from .telemetry import enabled, emit, parent_id

def begin(name, kind):
    data = {'id':uuid.uuid4().hex, 'name':name[:100], 'type':kind, 'started':time.time()}
    return data, parent_id.set(data['id'])

def finish(data, outcome, error=None):
    emit('command',dict(data, outcome=outcome, duration_ms=round((time.time()-data['started'])*1000,2),
                        exception=type(error).__name__ if error else None))

class ObservedTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        accepted = await super().interaction_check(interaction)
        if accepted and enabled():
            # Command data contains sensitive options; read only the fixed name.
            name = str((interaction.data or {}).get('name','unknown'))
            data, _ = begin(name,'application')
            interaction.extras['panel_command'] = data
        return accepted

    async def on_error(self, interaction, error):
        data = interaction.extras.pop('panel_command',None)
        if data:
            finish(data,'error',error)
        await super().on_error(interaction,error)

def install(bot):
    @bot.before_invoke
    async def start(context):
        if enabled():
            context.panel_command, context.panel_context_token = begin(context.command.qualified_name,'prefix')
    @bot.after_invoke
    async def complete(context):
        data = getattr(context,'panel_command',None)
        if data:
            finish(data,'error' if context.command_failed else 'success')
            parent_id.reset(context.panel_context_token)
    @bot.listen('on_app_command_completion')
    async def application_complete(interaction, command):
        data = interaction.extras.pop('panel_command',None)
        if data:
            finish(data,'success')
