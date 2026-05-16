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

