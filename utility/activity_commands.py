"""Keep the Portal-managed Activity entry point during discord.py command sync."""

async def sync_with_activity(bot):
    # discord.py 2.7 does not model type-4 commands. A normal bulk sync would
    # omit the Portal's Launch command. Read first and preserve its write fields.
    remote=await bot.http.get_global_commands(bot.application_id)
    entries=[command for command in remote if command.get('type')==4]
    if not entries:
        await bot.tree.sync()
        return
    tree=bot.tree
    commands=tree._get_all_commands(guild=None)
    payload=[await command.get_translated_payload(tree,tree.translator) for command in commands] if tree.translator else [command.to_dict(tree) for command in commands]
    fields={'id','name','name_localizations','description','description_localizations','type',
            'handler','default_member_permissions','dm_permission','integration_types','contexts','nsfw'}
    payload.extend({key:value for key,value in command.items() if key in fields} for command in entries)
    await bot.http.bulk_upsert_global_commands(bot.application_id,payload=payload)
