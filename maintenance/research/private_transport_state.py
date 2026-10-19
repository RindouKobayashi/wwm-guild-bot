"""Seal narrowly selected RC4 state; never return or save plaintext state."""
import base64
import ctypes as C
import hashlib
import struct
from datetime import datetime, timezone
from property_registry_layout import pointer
from transport_rc4_state import RC4State


def _dpapi(data, decrypt=False):
    """Windows DPAPI user scope, UI forbidden, no machine-wide protection."""
    if not 0<len(data)<=65536:raise ValueError('Sealed evidence size bounds')
    class Blob(C.Structure):
        _fields_=[('size',C.c_uint32),('data',C.c_void_p)]
    crypt=C.WinDLL('crypt32',use_last_error=True)
    kernel=C.WinDLL('kernel32',use_last_error=True)
    crypt.CryptProtectData.argtypes=[C.POINTER(Blob),C.c_wchar_p,C.POINTER(Blob),
                                    C.c_void_p,C.c_void_p,C.c_uint32,C.POINTER(Blob)]
    crypt.CryptProtectData.restype=C.c_int
    crypt.CryptUnprotectData.argtypes=[C.POINTER(Blob),C.c_void_p,C.POINTER(Blob),
                                      C.c_void_p,C.c_void_p,C.c_uint32,C.POINTER(Blob)]
    crypt.CryptUnprotectData.restype=C.c_int
    kernel.LocalFree.argtypes=[C.c_void_p];kernel.LocalFree.restype=C.c_void_p
    source=C.create_string_buffer(data)
    entropy=C.create_string_buffer(b'WWM private transport evidence v1')
    incoming=Blob(len(data),C.cast(source,C.c_void_p))
    extra=Blob(len(entropy.raw)-1,C.cast(entropy,C.c_void_p));out=Blob()
    function=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    description=None if decrypt else 'WWM private transport state'
    if not function(C.byref(incoming),description,C.byref(extra),None,None,1,C.byref(out)):
        raise C.WinError(C.get_last_error())
    try:
        if not 0<out.size<=65536:raise ValueError('DPAPI output size bounds')
        return C.string_at(out.data,out.size)
    finally:kernel.LocalFree(out.data)


def protect_for_current_user(data):
    if len(data)!=1032:raise ValueError('Expected exactly one transport state')
    return _dpapi(data)


def unprotect_for_current_user(data):
    raw=_dpapi(data,True)
    RC4State.from_engine_bytes(raw)
    return raw


def seal_states(read, metadata, image_base, seal=protect_for_current_user):
    """Two owned state objects only; stability checked before/after sealing."""
    result={'format':'wwm-user-dpapi-rc4-v1','captured_from_utc':datetime.now(timezone.utc).isoformat(),
            'directions':{},'contains_key_equivalent_data':True}
    wrappers=metadata.get('directions',{})
    if set(wrappers)!={'send','receive'} or any(
            v.get('implementation')!='mode_zero_rc4' for v in wrappers.values()):
        raise ValueError('Requires two validated mode-zero RC4 directions')
    pointers=set()
    for direction in ('send','receive'):
        direction_started=datetime.now(timezone.utc).isoformat()
        wrapper=pointer(int(wrappers[direction]['wrapper'],16))
        wrapper_type=read(wrapper,8)
        if struct.unpack('<Q',wrapper_type)[0]!=image_base+0x5b5b358:
            raise ValueError('RC4 wrapper identity changed')
        owner=read(wrapper+8,8);cipher=pointer(struct.unpack('<Q',owner)[0])
        cipher_type=read(cipher,8)
        if struct.unpack('<Q',cipher_type)[0]!=image_base+0x5b57498:
            raise ValueError('RC4 cipher identity changed')
        if cipher in pointers:raise ValueError('Direction state objects alias')
        pointers.add(cipher)
        state=read(cipher+8,1032)
        RC4State.from_engine_bytes(state)
        if read(cipher+8,1032)!=state:raise ValueError('RC4 state changed during read')
        protected=seal(state)
        if (read(wrapper,8)!=wrapper_type or read(wrapper+8,8)!=owner
                or read(cipher,8)!=cipher_type or read(cipher+8,1032)!=state):
            raise ValueError('RC4 owner/state changed during sealing')
        result['directions'][direction]={'protected_state_base64':base64.b64encode(protected).decode('ascii'),
            'state_sha256':hashlib.sha256(state).hexdigest(),'state_bytes':1032,
            'captured_from_utc':direction_started,'captured_to_utc':datetime.now(timezone.utc).isoformat()}
    result['captured_to_utc']=datetime.now(timezone.utc).isoformat()
    return result
