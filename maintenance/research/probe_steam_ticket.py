"""Bounded standalone Steam ticket feasibility probe; never logs ticket values.

Default: inspect the installed DLL without executing it. --request-ticket loads
the verified Valve DLL into THIS process, uses Steam's normal client API, and
cancels the ticket on exit. No WWM process access or SDK/game-server requests.
"""
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import time

HERE = Path(__file__).resolve().parent
DEFAULT_DLL = Path('C:/Program Files (x86)/Steam/steamapps/common/Where Winds Meet/Engine/Binaries/Win64r/steam_api64.dll')
VERIFIED_SHA256 = 'e082bf5c9f881c822b1540a76b74f9d15e18019a73ebd206a559595badcb7f65'
APP_ID = 3564740
CALLBACK_ID = 168


class CallbackMsg(C.Structure):
    _fields_ = [('user', C.c_int32), ('callback', C.c_int32),
                ('data', C.c_void_p), ('size', C.c_int32)]


def ticket_summary(payload, expected_handle):
    """Validate only the documented callback; export no ticket/identity bytes."""
    if len(payload) != 2572:
        raise ValueError('Unexpected ticket callback size')
    handle, result, length = struct.unpack_from('<Iii', payload)
    if handle != expected_handle:
        raise ValueError('Ticket callback handle mismatch')
    if result != 1:
        return {'status': 'steam_ticket_failed', 'steam_result': result}
    if not 1 <= length <= 2560:
        raise ValueError('Invalid ticket length')
    return {'status': 'steam_ticket_received', 'ticket_bytes': length,
            'ticket_values_saved': False}


def inspect_dll(path):
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != VERIFIED_SHA256:
        raise ValueError('Steam DLL changed: inspect the new build before executing it')
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if data[:2] != b'MZ' or data[pe:pe + 4] != b'PE\0\0' or struct.unpack_from('<H', data, pe + 4)[0] != 0x8664:
        raise ValueError('Expected verified x64 PE DLL')
    app = (path.parent / 'steam_appid.txt').read_text().strip('\0\r\n \t')
    if app != str(APP_ID):
        raise ValueError('Installed app context does not match inspected game')
    return {'dll_sha256': digest, 'app_id': APP_ID, 'status': 'steam_ticket_preflight_ok'}


def bind(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype = result
    fn.argtypes = list(args)
    return fn


def request_ticket(path, timeout=15, expected_steam_id=None, consume_ticket=None):
    if os.name != 'nt' or C.sizeof(C.c_void_p) != 8:
        raise ValueError('Requires 64-bit Windows Python and the logged-in Steam user')
    if not 1 <= timeout <= 30:
        raise ValueError('Timeout must be 1..30 seconds')
    inspect_dll(path)
    # Private working directory prevents Steam diagnostic files landing in game.
    root = HERE / 'output' / 'steam-ticket-probe'
    root.mkdir(parents=True, exist_ok=True)
    old_cwd = Path.cwd()
    previous = {key: os.environ.get(key) for key in ('SteamAppId', 'SteamGameId')}
    with tempfile.TemporaryDirectory(prefix='context-', dir=root) as context:
        try:
            Path(context, 'steam_appid.txt').write_text(str(APP_ID))
            os.chdir(context)
            os.environ['SteamAppId'] = os.environ['SteamGameId'] = str(APP_ID)
            dll = C.CDLL(str(path.resolve()), winmode=0x1100)
            init = bind(dll, 'SteamAPI_InitSafe', C.c_bool)
            shutdown = bind(dll, 'SteamAPI_Shutdown', None)
            if not init():
                return {'status': 'steam_init_failed', 'action': 'Open Steam and sign in as this Windows user; the game is not required for this probe.'}
            handle = 0
            user = None
            cancel = None
            try:
                user = bind(dll, 'SteamAPI_SteamUser_v023', C.c_void_p)()
                utils = bind(dll, 'SteamAPI_SteamUtils_v010', C.c_void_p)()
                if not user or not utils:
                    raise ValueError('Steam interfaces unavailable')
                app = bind(dll, 'SteamAPI_ISteamUtils_GetAppID', C.c_uint32, C.c_void_p)(utils)
                if app != APP_ID:
                    raise ValueError('Steam initialized under a different app')
                if not bind(dll, 'SteamAPI_ISteamUser_BLoggedOn', C.c_bool, C.c_void_p)(user):
                    return {'status': 'steam_not_logged_on'}
                if expected_steam_id is not None:
                    account = bind(dll, 'SteamAPI_ISteamUser_GetSteamID', C.c_uint64, C.c_void_p)(user)
                    if str(account) != expected_steam_id:
                        return {'status': 'steam_account_mismatch', 'request_sent': False}
                bind(dll, 'SteamAPI_ManualDispatch_Init', None)()
                pipe = bind(dll, 'SteamAPI_GetHSteamPipe', C.c_int32)()
                if not pipe:
                    raise ValueError('Steam callback pipe unavailable')
                frame = bind(dll, 'SteamAPI_ManualDispatch_RunFrame', None, C.c_int32)
                next_callback = bind(dll, 'SteamAPI_ManualDispatch_GetNextCallback', C.c_bool, C.c_int32, C.POINTER(CallbackMsg))
                free = bind(dll, 'SteamAPI_ManualDispatch_FreeLastCallback', None, C.c_int32)
                cancel = bind(dll, 'SteamAPI_ISteamUser_CancelAuthTicket', None, C.c_void_p, C.c_uint32)
                handle = bind(dll, 'SteamAPI_ISteamUser_GetAuthTicketForWebApi', C.c_uint32, C.c_void_p, C.c_char_p)(user, None)
                if not handle:
                    return {'status': 'steam_ticket_request_rejected'}
                deadline = time.monotonic() + timeout
                count = 0
                while time.monotonic() < deadline and count < 4096:
                    frame(pipe)
                    msg = CallbackMsg()
                    while count < 4096 and time.monotonic() < deadline and next_callback(pipe, C.byref(msg)):
                        count += 1
                        try:
                            if msg.callback == CALLBACK_ID:
                                if not msg.data or msg.size != 2572:
                                    raise ValueError('Unexpected callback pointer/size')
                                payload = C.string_at(msg.data, msg.size)
                                result = ticket_summary(payload, handle)
                                if result['status'] == 'steam_ticket_received' and consume_ticket:
                                    result['consumer_result'] = consume_ticket(payload[12:12 + result['ticket_bytes']])
                                return result
                        finally:
                            free(pipe)
                    time.sleep(0.025)
                return {'status': 'steam_ticket_callback_budget_reached', 'callbacks_checked': count}
            finally:
                try:
                    if handle and cancel and user:
                        cancel(user, handle)
                finally:
                    shutdown()
        finally:
            os.chdir(old_cwd)
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-ticket', action='store_true')
    parser.add_argument('--dll', type=Path, default=DEFAULT_DLL)
    args = parser.parse_args()
    report = inspect_dll(args.dll)
    if args.request_ticket:
        report.update(request_ticket(args.dll.resolve()))
    report.update(game_process_access=False, sdk_login_requests=False,
                  ticket_values_saved=False, ticket_cancelled_on_probe_exit=bool(args.request_ticket and report['status'] == 'steam_ticket_received'))
    output = HERE / 'output' / 'steam-ticket-probe'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'latest.redacted.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report['status'] in ('steam_ticket_preflight_ok', 'steam_ticket_received') else 2


if __name__ == '__main__':
    raise SystemExit(main())
