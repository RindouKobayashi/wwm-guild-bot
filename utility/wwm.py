import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import requests
import msgpack
import json
from settings import WWM_UID, WWM_TOKEN, WWM_API_URL, WWM_CLUB_HOSTNUMS_URL, WWM_FULL_GUILD_URL, WWM_HOST, logger

def get_player_info(number_id, uid=None, token=None, api_url=None):
    # ======================
    # CONFIG (EDIT THESE)
    # ======================
    UID = uid if uid else WWM_UID
    TOKEN = token if token else WWM_TOKEN
    NUMBER_ID = number_id

    URL = api_url if api_url else WWM_API_URL

    HEADERS = {
        "Host": WWM_HOST,
        "Connection": "close",
        "h72-ms-uid": UID,
        "h72-ms-token": TOKEN,
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/octet-stream",
    }

    # ======================
    # BUILD MESSAGEPACK BODY
    # ======================
    payload = {
        "uid": UID,
        "number_id": NUMBER_ID,
        "force_search": False
    }

    packed = msgpack.packb(payload)

    try:
        # ======================
        # SEND REQUEST
        # ======================
        response = requests.post(URL, headers=HEADERS, data=packed, verify=True)
        
        logger.info(f"Player info request status: {response.status_code}")
        
        if response.status_code == 200:
            # ======================
            # DECODE RESPONSE
            # ======================
            try:
                data = msgpack.unpackb(response.content, raw=False, strict_map_key=False)
                logger.info("Successfully decoded player data")
                return data
                
            except Exception as e:
                logger.error(f"Failed to decode msgpack: {str(e)}")
                logger.debug(f"Raw content: {response.content}")
                return response.content
        else:
            logger.warning(f"Error response: {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"Player info request failed: {str(e)}")
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
    
    # Dynamic payload injection - replace club_id at exact byte position
    packed = b'\x84\xa7club_id\xb0' + club_id.encode('utf-8') + b'\xa3uid\xb0aflRzGCSslwu4Bsj\xaafield_info\x8a\xa9warehouse\x90\xa6applys\x90\xa8building\x90\xa7members\x90\xa8activity\x90\xafcustom_activity\x90\xa7targets\x90\xa4play\x90\xa4base\x90\xa5bonus\x90\xa7hostnum\xcd\'w'
    
    try:
        response = requests.post(
            URL,
            headers=headers,
            data=packed,
            verify=True,
            allow_redirects=False
        )
        
        logger.info(f"Full guild info request status: {response.status_code}")
        
        if response.status_code == 200:
            data = msgpack.unpackb(response.content, raw=False, strict_map_key=False)
            logger.info("Full guild data retrieved successfully")
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