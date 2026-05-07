import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import requests
import msgpack
import json
from typing import Dict, Any, Optional, List
from settings import (
    WWM_UID, WWM_TOKEN, WWM_API_URL, WWM_CLUB_HOSTNUMS_URL,
    WWM_FULL_GUILD_URL, WWM_FASHION_PLAN_URL, WWM_HOST, logger
)

# -----------------------------------------------------------------------------
# Shared Constants
# -----------------------------------------------------------------------------
DEFAULT_HEADERS = {
    "Host": WWM_HOST,
    "Connection": "close",
    "h72-ms-uid": WWM_UID,
    "h72-ms-token": WWM_TOKEN,
    "Accept-Encoding": "gzip, deflate",
    "Content-Type": "application/octet-stream",
}

# Default requested fields (only useful fields - fast / minimal size)
DEFAULT_FIELDS = [
    "base", "team", "head", "name_card", "club",
    "kongfu", "ride", "mentor", "jieyuan_info", "jieyi",
    "jieyi_misc", "gameplay_trail", "pvp_battle", "attr"
]

# Complete list of ALL known fields (kept for reference / debugging)
ALL_KNOWN_FIELDS = [
    "base", "team", "head", "friend", "prison_prop", "space_data",
    "name_card", "club", "chat_room_sync", "disease", "kongfu",
    "ride", "mentor", "jieyuan_info", "jieyi", "jieyi_misc",
    "longmen", "gameplay_trail", "settings", "pvp_battle", "homeland",
    "attr"
]

# -----------------------------------------------------------------------------
# Base API Request Handler (all common logic in one place)
# -----------------------------------------------------------------------------
def _wwm_api_post(
    url: str,
    payload: Dict[str, Any],
    uid: Optional[str] = None,
    token: Optional[str] = None,
    timeout: int = 10,
    raw_payload: Optional[bytes] = None
) -> Optional[Dict[str, Any]]:
    """
    Internal base handler for all WWM MessagePack API requests
    Handles packing, headers, request sending, unpacking and logging automatically
    """
    headers = DEFAULT_HEADERS.copy()
    
    # Override credentials if provided
    if uid:
        headers["h72-ms-uid"] = uid
    if token:
        headers["h72-ms-token"] = token

    try:
        # Pack payload unless raw bytes are explicitly provided
        request_data = raw_payload if raw_payload is not None else msgpack.packb(payload)
        
        response = requests.post(
            url,
            headers=headers,
            data=request_data,
            timeout=timeout,
            verify=True,
            allow_redirects=False
        )

        logger.debug(f"API Request {url} status: {response.status_code}")

        if response.status_code == 200:
            return msgpack.unpackb(
                response.content,
                raw=False,
                strict_map_key=False
            )
        
        logger.warning(f"API Request failed {url}: HTTP {response.status_code}")
        return None

    except Exception as e:
        logger.error(f"API Request failed {url}: {str(e)}", exc_info=True)
        return None

# -----------------------------------------------------------------------------
# Public API Functions
# -----------------------------------------------------------------------------
def get_player_info(number_id: str, uid: Optional[str] = None, token: Optional[str] = None, api_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Get full player info by Number ID (two step lookup)
    1. Resolve Number ID to PID
    2. Fetch full player data from Redis endpoint
    """
    logger.info(f"Resolving Number ID {number_id} to PID")
    
    # Step 1: Resolve Number ID to PID
    pid_result = _wwm_api_post(
        api_url if api_url else WWM_API_URL,
        {
            "uid": uid if uid else WWM_UID,
            "number_id": number_id,
            "force_search": False
        },
        uid=uid,
        token=token
    )

    if not pid_result or 'result' not in pid_result or 'id' not in pid_result['result']:
        logger.warning("Could not resolve Number ID to PID")
        return pid_result

    player_pid = pid_result['result']['id']
    logger.info(f"✅ Resolved PID: {player_pid}")

    # Step 2: Get full player data
    logger.info(f"Getting full player data for PID {player_pid}")
    
    redis_data = _wwm_api_post(
        WWM_CLUB_HOSTNUMS_URL,
        {
            "fields": DEFAULT_FIELDS,
            "hostnum2pids": {
                10595: [player_pid]
            },
            "uid": uid if uid else WWM_UID
        },
        uid=uid,
        token=token
    )

    if redis_data and 'result' in redis_data and redis_data['result']:
        first_pid = next(iter(redis_data['result'].keys()))
        full_player_data = redis_data['result'][first_pid]
        logger.info("✅ Got full player data with signatures")
        
        return {
            'code': 0,
            'result': full_player_data
        }

    # Fallback to original data if redis request fails
    logger.info("Falling back to original player data")
    return pid_result


def get_full_guild_info(club_id: int, hostnum: int = 10103) -> Optional[Dict[str, Any]]:
    """Get complete guild information including all fields"""
    logger.info(f"Getting full guild info for club_id: {club_id}")
    
    return _wwm_api_post(
        WWM_FULL_GUILD_URL,
        {
            "club_id": club_id,
            "uid": WWM_UID,
            "field_info": {
                "warehouse": [],
                "applys": [],
                "building": [],
                "members": [],
                "activity": [],
                "custom_activity": [],
                "targets": [],
                "play": [],
                "base": [],
                "bonus": []
            },
            "hostnum": hostnum
        }
    )


def get_club_hostnums(player_pid: str) -> Optional[Dict[str, Any]]:
    """Get club hostnum associations for a player"""
    logger.info(f"Getting club hostnums for PID: {player_pid}")
    
    return _wwm_api_post(
        WWM_CLUB_HOSTNUMS_URL,
        {
            "fields": ["club"],
            "hostnum2pids": {
                10595: [player_pid]
            },
            "uid": WWM_UID
        }
    )


def get_bulk_players_info(pid_list: List[str], fields: Optional[List[str]] = None, hostnum: int = 10595) -> Optional[Dict[str, Any]]:
    """Bulk fetch multiple players info in one API call"""
    if fields is None:
        fields = ["base"]
    
    logger.info(f"Bulk fetching {len(pid_list)} players")
    
    return _wwm_api_post(
        WWM_CLUB_HOSTNUMS_URL,
        {
            "fields": fields,
            "hostnum2pids": {
                hostnum: pid_list
            },
            "uid": WWM_UID
        }
    )


def get_fashion_plan(player_pid: str, hostnum: int = 40, uid: Optional[str] = None, token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Get player fashion plan including cover image"""
    logger.info(f"Getting fashion plan for PID: {player_pid}")
    
    final_uid = uid if uid else WWM_UID
    
    # Special raw byte payload required for this specific endpoint
    raw_payload = b'\x83\xa3uid\xb0' + final_uid.encode('utf-8') + b'\xa3pid\xb0' + player_pid.encode('utf-8') + b'\xa7hostnum\xcd(\xa3'
    logger.debug(f"Fashion plan raw payload bytes: {raw_payload.hex()}")

    return _wwm_api_post(
        WWM_FASHION_PLAN_URL,
        {},
        uid=final_uid,
        token=token,
        raw_payload=raw_payload
    )


def get_club_chat(club_id: str, hostnum: int = 10103) -> Optional[Dict[str, Any]]:
    """Fetch latest club chat messages"""
    logger.debug(f"Fetching club chat for club_id: {club_id} (hostnum: {hostnum})")
    
    api_url = "https://h72naxx2gb-ms-prod.easebar.com/flk/club_service/get_club_info"
    
    payload = {
        "club_id": club_id,
        "hostnum": hostnum,
        "field_info": {
            "chat": []
        },
        "uid": WWM_UID
    }
    
    return _wwm_api_post(api_url, payload)


def get_full_player_and_club(number_id: str) -> Dict[str, Any]:
    """Complete player + guild lookup workflow"""
    print(f"\n🔍 Looking up player with number_id: {number_id}")
    
    # Step 1: Get player info
    player_data = get_player_info(number_id)
    player = player_data.get('result', {}) if player_data else {}
    player_pid = player.get('id')
    
    print(f"\n✅ Found player: {player.get('name')}")
    print(f"✅ Player PID: {player_pid}")
    print("\n" + "="*60)

    club_data = None
    full_guild_data = None

    if player_pid:
        # Step 2: Get club info for player
        print(f"\n🏰 Getting club info for this player...")
        club_data = get_club_hostnums(player_pid)
        
        print("\n" + "="*60)

        # Step 3: Get full guild data
        if club_data:
            result_data = club_data.get('result', {})
            club_player_data = result_data.get(player_pid, {})
            club_id = club_player_data.get('club', {}).get('club_id')
            
            if club_id:
                print(f"\n🏯 Getting full guild data for club_id: {club_id}")
                full_guild_data = get_full_guild_info(club_id)

    # Combine all data
    combined_data = {
        "number_id": number_id,
        "player_data": player_data,
        "club_hostnum_data": club_data,
        "full_guild_data": full_guild_data
    }

    # Save to JSON file
    filename = f"{number_id}.json"
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(combined_data, f, indent=4, default=str, ensure_ascii=False)
    
    print(f"\n✅ All data combined and saved to: {filename}")
    
    return combined_data


if __name__ == "__main__":
    # CONFIG: Just change this number_id to lookup any player
    TARGET_NUMBER_ID = "4036668451"
    
    combined_data = get_full_player_and_club(TARGET_NUMBER_ID)
    
    print("\n✅ Done.")