"""
WWM API Constants and Field Definitions
Central place to store all API field names, constants, and common requests
"""

from settings import branch
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

if branch == "main":
    SCHOOL_EMOTES = {
        1: "<:WellOfHeaven:1508516903726088273>",
        2: "<:MaskedTroupe:1508517146794262749>",
        3: "<:RagingTides:1508517349412573284>",
        4: "<:SilverNeedle:1508517548142886942>",
        6: "<:MidnightBlades:1508517668729257984>",
        11: "<:NineMortalWays:1508517811004248134>",
        12: "<:VelvetShade:1508517976356425769>"
    }
else:
    SCHOOL_EMOTES = {
        1: "<:WellOfHeaven:1508518909345796218>",
        2: "<:MaskedTroupe:1508518907630063757>",
        3: "<:RagingTides:1508518905927438496>",
        4: "<:SilverNeedle:1508518903976956097>",
        6: "<:MidnightBlades:1508518902429257759>",
        11: "<:NineMortalWays:1508518900567117834>",
        12: "<:VelvetShade:1508518898188812369>"
    }


SCHOOL_RANKING = {
    "1_1": "Average Brother",
    "1_2": "Ironclad Buddy",
    "1_3": "Good Pal",
    "1_4": "Deputy Master",
    "1_5": "Hall Master",

    "2_1": "Understudy",
    "2_2": "Performer",
    "2_3": "Principal Artist",
    "2_4": "Stage Master",
    "2_5": "Stage Director",

    "3_1": "Solider",
    "3_2": "Squad Leader",
    "3_3": "Platoon Leader",
    "3_4": "Brigade Commander",
    "3_5": "Division General",

    "4_1": "Keeper",
    "4_2": "Physician",
    "4_3": "Chief Physician",
    "4_4": "Medical Scholar",
    "4_5": "Divine Healer",

    "6_1": "Novice Cultivator",
    "6_2": "Truth Walker",
    "6_3": "Sufferer",
    "6_4": "Chief Elder",

    "11_1": "Outer Prentice",
    "11_2": "Inner Disciple",
    "11_3": "Core Prentice",
    "11_4": "Faction Master",
    "11_5": "Clan Master",

    "12_1": "First Crimson Blossom",
    "12_2": "Twin Lotuses",
    "12_3": "Charming Trio",
    "12_4": "Four-Fragrance",
    "12_5": "Flower Messenger",
}

BOSS_NAMES = {
    1: "The Void King", 2: "Ye Wanshan", 3: "Lucky Seventeen",
    4: "Heartseeker", 5: "Snaker Doctor", 6: "Puppeteer",
    7: "Earth Fiend Deity", 8: "Yi Dao", 9: "Dao Lord",
    10: "Lion Dance", 11: "*BLANK*", 12: "*BLANK*",
    13: "Coffin Master", 14: "Zheng E", 15: "Drunk Martial Artist",
    16: "Ghost Master", 17: "Nameless General", 18: "Wolf Maiden",
    19: "*BLANK*", 20: "Grand Protector of Anxi", 21: "Moongazing Maiden",
    22: "Everdeer", 23: "*BLANK*", 24: "*BLANK*",
    25: "Sentinel Howlion", 26: "Pocketrupt Circus", 27: "Snowplum Requiem",
    28: "Veiled Lady", 29: "Moonlight Master"

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
    20900: "Gaunlet (?)"
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

