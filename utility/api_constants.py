"""
WWM API Constants and Field Definitions
Central place to store all API field names, constants, and common requests
"""

# ==============================================
# BULK PLAYER API FIELDS
# ==============================================
BULK_FIELDS = {
    # Base player info
    "base": [
        "nickname",
        "level",
        "number_id",
        "is_online",
        "last_online_ts",
        "online_time",
        "oversea_tag",
    ],
    
    # Club / Guild membership info
    "club": [
        "club_id",
        "hostnum",
        "post",
        "join_time",
        "liveness",
        "week_liveness",
        "total_liveness",
        "contribution",
    ],
    
    # Player attributes / stats
    "attr": [
        "XIUWEI_KUNGFU",
        "XIUWEI_TRADE3",
        "XIUWEI_TRADE4",
        "XIUWEI_EXPLORE",
        "STR",
        "CON",
        "BAS",
        "CRI",
        "AGI",
    ],
    
    # Name card / profile
    "name_card": [
        "sign",
        "title",
        "avatar_frame",
    ],
    
    # Gameplay stats
    "gameplay_trail": [
        "played",
        "pk_match_info",
    ]
}

# Commonly used combined field sets
BULK_PRESETS = {
    # For guild monitor - online count + weekly points
    "guild_monitor": ["base", "club"],
    
    # For full player profile
    "full_profile": ["base", "club", "attr", "name_card", "gameplay_trail"],
    
    # For verification only
    "verification": ["base", "club"],
    
    # For online status only
    "online_only": ["base"],
}


# ==============================================
# GUILD API FIELDS
# ==============================================
GUILD_FIELDS = {
    "base": [
        "name",
        "level",
        "fund",
        "fame",
        "week_fame",
        "member_num",
        "apprentice_num",
    ],
    
    "members": [
        "members",
        "member_num",
        "apprentice_num",
    ],
    
    "activity": [
        "week_liveness",
        "total_liveness",
    ],
    
    "play": [
        "pk_match_info",
        "battle_score",
    ],
    
    "buildings": [
        "building_list",
    ],
    
    "applys": [
        "apply_dict",
    ],
    
    "gonggao_info": [
        "msg",
        "update_time",
    ]
}


# ==============================================
# API ENDPOINTS
# ==============================================
API_ENDPOINTS = {
    "get_player": "/player/get",
    "get_bulk_players": "/player/bulk/get",
    "get_guild": "/club/get",
    "get_club_hostnums": "/club/hostnums",
    "get_fashion_plan": "/fashion/plan/get",
    "get_club_chat": "/club/chat/get",
    "find_people_by_nickname": "/find_people/by_nickname",
}


# ==============================================
# CLUB CHAT API FIELDS
# ==============================================
CLUB_CHAT_FIELDS = [
    "chat"
]

# ==============================================
# SCHOOL / SECT MAPPING
# ==============================================
# In-game Sects (base.school is a numeric ID).
SCHOOL_NAMES = {
    1: "Well of Heaven",
    2: "Masked Troupe",
    3: "Raging Tides",
    4: "Silver Needle",
    6: "Midnight Blades",
    11: "Nine Mortal Ways",
    12: "Velvet Shade",
    100: "Sectless",
}

CLUB_CHAT_MESSAGE_FIELDS = {
    "from_pid",
    "nickname",
    "level",
    "msg",
    "msg_id",
    "channel",
    "ts",
    "head_id",
    "head_back_color",
    "hostnum",
    "body_type",
    "is_prisoner",
    "is_chuyan",
    "ext"
}

# ==============================================
# KUNGFU / WEAPON MAPPING
# ==============================================
KONGFU_WEAPON_MAP = {
    10101: "Strategic Sword",
    10102: "Nameless Sword",
    10201: "Heavenquaker Spear",
    10202: "Nameless Spear",
    10301: "Panacea Fan",
    10302: "Inkwell Fan",
    20103: "Stormbreaker Spear",
    20401: "Thundercry Blade",
    20402: "Phalanxbane Blade",
    20501: "Infernal Twinblades",
    20601: "Vernal Umbrella",
    20602: "Soulshade Umbrella",
    20603: "Everspring Umbrella",
    20701: "Mortal Rope Dart",
    20702: "Unfettered Rope Dart",
    20801: "Snowparting Blade",
}

HEALER_WEAPONS = {10301, 20602}
TANK_WEAPONS = {20103, 20401}


def classify_kongfu_role(weapon_ids: list) -> str:
    """Classify player role based on their equipped kongfu/weapon IDs."""
    healer_count = sum(1 for w in weapon_ids if w in HEALER_WEAPONS)
    tank_count = sum(1 for w in weapon_ids if w in TANK_WEAPONS)
    dps_count = len(weapon_ids) - healer_count - tank_count
    
    if healer_count == 2:
        return "Healer"
    elif tank_count == 2:
        return "Tank"
    elif healer_count == 1 and tank_count == 0 and dps_count <= 1:
        return "Healer Hybrid"
    elif tank_count == 1 and healer_count == 0 and dps_count <= 1:
        return "Tank Hybrid"
    elif healer_count == 1 and tank_count == 1:
        return "Healer/Tank Hybrid"
    else:
        return "DPS"


def format_kongfu_display(weapon_ids: list) -> str:
    """Format weapon names and role classification into a display string."""
    weapon_names = [KONGFU_WEAPON_MAP.get(w, f"Unknown ({w})") for w in weapon_ids]
    role = classify_kongfu_role(weapon_ids)
    role_emoji = {
        "Healer": "💚",
        "Healer Hybrid": "💚",
        "Tank": "🛡️",
        "Tank Hybrid": "🛡️",
        "Healer/Tank Hybrid": "💚🛡️",
        "DPS": "⚔️",
    }.get(role, "⚔️")
    
    weapons_str = ", ".join(weapon_names)
    return f"{weapons_str} | {role_emoji} {role}"


def get_kongfu_ids_from_player(player_data: dict) -> list:
    """Extract kongfu weapon IDs from player data's kongfu section.
    Structure is: {"kongfu": {"kongfu_main": 10101, "kongfu_sub": 10202}}
    """
    kongfu_data = player_data.get('kongfu', {})
    if not kongfu_data:
        return []
    
    weapon_ids = []
    main_id = kongfu_data.get('kongfu_main')
    sub_id = kongfu_data.get('kongfu_sub')
    if main_id:
        weapon_ids.append(main_id)
    if sub_id:
        weapon_ids.append(sub_id)
    
    return weapon_ids

