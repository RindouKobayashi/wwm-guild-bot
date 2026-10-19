"""Offline RC4 state handling for captured transport evidence; no process access.

Never use this legacy cipher to protect new data. This module models saved
engine behavior; it does not load or execute captured machine code.
"""
import struct


class RC4State:
    def __init__(self, x, y, permutation):
        if not 0<=x<=255 or not 0<=y<=255:
            raise ValueError('RC4 index bounds')
        if len(permutation)!=256 or sorted(permutation)!=list(range(256)):
            raise ValueError('RC4 permutation bounds/uniqueness')
        self.x=x;self.y=y;self.s=list(permutation)

    @classmethod
    def from_engine_bytes(cls, raw):
        if len(raw)!=1032:raise ValueError('Expected exactly 1032 state bytes')
        values=struct.unpack('<258I',raw)
        return cls(values[0],values[1],values[2:])

    @classmethod
    def from_key(cls, key):
        if not 1<=len(key)<=256:raise ValueError('RC4 key size bounds')
        s=list(range(256));j=0
        for i in range(256):
            j=(j+s[i]+key[i%len(key)])&255
            s[i],s[j]=s[j],s[i]
        return cls(0,0,s)

    def to_engine_bytes(self):
        return struct.pack('<258I',self.x,self.y,*self.s)

    def apply(self,data):
        result=bytearray(len(data));s=self.s;x=self.x;y=self.y
        for pos,value in enumerate(data):
            x=(x+1)&255;y=(y+s[x])&255
            s[x],s[y]=s[y],s[x]
            result[pos]=value^s[(s[x]+s[y])&255]
        self.x=x;self.y=y
        return bytes(result)

    def rewind(self,count):
        if not isinstance(count,int) or not 0<=count<=64*1024*1024:
            raise ValueError('Offline rewind budget exceeded')
        s=self.s;x=self.x;y=self.y
        for _ in range(count):
            old_y=(y-s[y])&255
            s[x],s[y]=s[y],s[x]
            x=(x-1)&255;y=old_y
        self.x=x;self.y=y
