import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import requests
import msgpack
import json
from settings import WWM_UID, WWM_TOKEN, WWM_API_URL, WWM_CLUB_HOSTNUMS_URL, WWM_FULL_GUILD_URL, WWM_HOST, logger

def get_player_info(number_id, uid=None, token=None, api_url=None):
    # ======================
    # TWO STEP LOOKUP:
    # 1. First use old endpoint to get player PID from Number ID
    # 2. Then use new redis endpoint with PID to get FULL data including signatures
    # ======================
    UID = uid if uid else WWM_UID
    TOKEN = token if token else WWM_TOKEN
    NUMBER_ID = number_id

    HEADERS = {
        "Host": WWM_HOST,
        "Connection": "close",
        "h72-ms-uid": UID,
        "h72-ms-token": TOKEN,
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/octet-stream",
    }

    # ==================================
    # STEP 1: Get player PID using old endpoint
    # ==================================
    logger.info(f"Step 1: Resolving Number ID {NUMBER_ID} to PID")
    
    payload = {
        "uid": UID,
        "number_id": NUMBER_ID,
        "force_search": False
    }

    packed = msgpack.packb(payload)

    try:
        response = requests.post(WWM_API_URL, headers=HEADERS, data=packed, verify=True)
        
        if response.status_code != 200:
            logger.warning(f"PID lookup failed: {response.status_code}")
            return None

        data = msgpack.unpackb(response.content, raw=False, strict_map_key=False)
        
        if 'result' not in data or 'id' not in data['result']:
            logger.warning("Could not get player PID")
            return data
            
        player_pid = data['result']['id']
        logger.info(f"✅ Resolved PID: {player_pid}")

        # ==================================
        # STEP 2: Get FULL player data using new redis endpoint
        # ==================================
        logger.info(f"Step 2: Getting full player data for PID {player_pid}")

        REDIS_URL = "https://h72naxx2gb-ms-prod.easebar.com/flk/redis_player/get_players_info"
        
        payload = {
            "fields": [
                "base",
                "team",
                "head",
                "friend",
                "prison_prop",
                "space_data",
                "name_card",
                "club",
                "chat_room_sync",
                "disease",
                "kongfu",
                "ride",
                "mentor",
                "jieyuan_info",
                "jieyi",
                "jieyi_misc",
                "longmen",
                "gameplay_trail",
                "settings",
                "pvp_battle",
                "homeland"
            ],
            "hostnum2pids": {
                10595: [player_pid]
            },
            "uid": UID
        }

        packed = msgpack.packb(payload)
        
        response = requests.post(REDIS_URL, headers=HEADERS, data=packed, verify=True)
        
        logger.info(f"Redis request status: {response.status_code}")
        
        if response.status_code == 200:
            redis_data = msgpack.unpackb(response.content, raw=False, strict_map_key=False)
            
            if 'result' in redis_data and redis_data['result']:
                first_pid = next(iter(redis_data['result'].keys()))
                full_player_data = redis_data['result'][first_pid]
                
                logger.info("✅ Got full player data with signatures")
                
                # Return in same format as before
                return {
                    'code': 0,
                    'result': full_player_data
                }
        
        # Fallback: return original data if redis fails
        logger.info("Falling back to original data")
        return data

    except Exception as e:
        logger.error(f"Player lookup failed: {str(e)}")
        return None


def get_full_guild_info(club_id):
    URL = WWM_FULL_GUILD_URL
    
    headers = {
        'Host': WWM_HOST,
        'Connection': 'close',
        'h72-ms-uid': WWM_UID,
        'h72-ms-token': WWM_TOKEN,
        'Accept-Encoding': 'gzip, deflate',
        'Content-Type': 'application/octet-stream'
    }
    
    # Correct full payload with all fields requested
    payload = {
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
        "hostnum": 10103
    }
    
    packed = msgpack.packb(payload)
    
    try:
        response = requests.post(
            URL,
            headers=headers,
            data=packed,
            verify=True,
            allow_redirects=False
        )
        
        logger.debug(f"Full guild info request status: {response.status_code}")
        
        if response.status_code == 200:
            data = msgpack.unpackb(response.content, raw=False, strict_map_key=False)
            logger.debug("Full guild data retrieved successfully")
            return data
        else:
            logger.warning(f"Guild info error: {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"Full guild info request failed: {str(e)}")
        return None


def get_club_hostnums(player_pid):
    # Correct API endpoint - redis_player not club_service
    URL = WWM_CLUB_HOSTNUMS_URL
    
    HEADERS = {
        "Host": WWM_HOST,
        "Connection": "close",
        "h72-ms-uid": WWM_UID,
        "h72-ms-token": WWM_TOKEN,
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/octet-stream",
    }

    # Dynamically build payload with actual player PID from first request
    payload = {
        "fields": ["club"],
        "hostnum2pids": {
            10595: [player_pid]
        },
        "uid": "aflxRmCSslwu4HAo"
    }
    
    packed = msgpack.packb(payload)

    try:
        response = requests.post(URL, headers=HEADERS, data=packed, verify=True)
        
        logger.info(f"Club hostnum lookup status: {response.status_code}")
        
        if response.status_code == 200:
            data = msgpack.unpackb(response.content, raw=False, strict_map_key=False)
            logger.info("Club hostnum data retrieved successfully")
            return data
        else:
            logger.warning(f"Club hostnum error: {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"Club hostnum request failed: {str(e)}")
        return None


def get_bulk_players_info(pid_list, fields=None, hostnum=10595):
    """Bulk fetch multiple players info in one API call"""
    if fields is None:
        fields = ["base"]
    
    URL = "https://h72naxx2gb-ms-prod.easebar.com/flk/redis_player/get_players_info"
    
    HEADERS = {
        "Host": WWM_HOST,
        "Connection": "close",
        "h72-ms-uid": WWM_UID,
        "h72-ms-token": WWM_TOKEN,
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/octet-stream",
    }
    
    payload = {
        "fields": fields,
        "hostnum2pids": {
            hostnum: pid_list
        },
        "uid": WWM_UID
    }
    
    try:
        packed = msgpack.packb(payload)
        response = requests.post(URL, headers=HEADERS, data=packed, timeout=10, verify=True)
        
        if response.status_code == 200:
            return msgpack.unpackb(response.content, raw=False, strict_map_key=False)
        
        logger.warning(f"Bulk player info failed: {response.status_code}")
        return None
        
    except Exception as e:
        logger.error(f"Bulk player info request failed: {str(e)}")
        return None


def get_fashion_plan(player_pid, hostnum=40, uid=None, token=None):
    """Get player fashion plan including cover image"""
    from settings import WWM_FASHION_PLAN_URL
    
    URL = WWM_FASHION_PLAN_URL
    UID = uid if uid else WWM_UID
    TOKEN = token if token else WWM_TOKEN
    
    HEADERS = {
        "Host": WWM_HOST,
        "Connection": "close",
        "h72-ms-uid": UID,
        "h72-ms-token": TOKEN,
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/octet-stream",
    }

    # EXACT payload including trailing \xa3 byte required by server
    # Server rejects msgpack auto-packed data, must use exact byte sequence
    packed = b'\x83\xa3uid\xb0' + UID.encode('utf-8') + b'\xa3pid\xb0' + player_pid.encode('utf-8') + b'\xa7hostnum\xcd(\xa3'
    logger.debug(f"Fashion plan raw payload bytes: {packed.hex()}")

    try:
        response = requests.post(URL, headers=HEADERS, data=packed, verify=True)
        
        logger.info(f"Fashion plan request status: {response.status_code}")
        
        if response.status_code == 200:
            data = msgpack.unpackb(response.content, raw=False, strict_map_key=False)
            logger.info("Fashion plan data retrieved successfully")
            logger.debug(f"Full fashion plan response: {json.dumps(data, indent=2, default=str)}")
            return data
        else:
            logger.warning(f"Fashion plan error: {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"Fashion plan request failed: {str(e)}")
        return None


def get_full_player_and_club(number_id):
    print(f"\n🔍 Looking up player with number_id: {number_id}")
    
    # Step 1: Get player info first
    player_data = get_player_info(number_id)
    
    player = player_data.get('result', {})
    player_pid = player.get('id')
    
    print(f"\n✅ Found player: {player.get('name')}")
    print(f"✅ Player PID: {player_pid}")
    
    print("\n" + "="*60)
    
    # Step 2: Use player PID to get club info
    print(f"\n🏰 Getting club info for this player...")
    club_data = get_club_hostnums(player_pid)
    
    print("\n" + "="*60)
    
    # Step 3: Extract club_id from club response - data is under 'result' key
    result_data = club_data.get('result', {})
    club_player_data = result_data.get(player_pid, {})
    club_id = club_player_data.get('club', {}).get('club_id')
    
    print(f"\n🏯 Getting full guild data for club_id: {club_id}")
    full_guild_data = get_full_guild_info(club_id) if club_id else None
    
    # Combine all data into single object
    combined_data = {
        "number_id": number_id,
        "player_data": player_data,
        "club_hostnum_data": club_data,
        "full_guild_data": full_guild_data
    }
    
    # Save single combined JSON file named after number_id
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