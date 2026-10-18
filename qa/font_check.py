from pathlib import Path
import struct

data=(Path(__file__).resolve().parents[1]/'web/assets/fonts/Monocraft-Regular.ttf').read_bytes()
u16=lambda offset:struct.unpack_from('>H',data,offset)[0]
tables={}
for i in range(u16(4)):
    tag,checksum,offset,length=struct.unpack_from('>4sIII',data,12+16*i)
    tables[tag]=offset
cmap=tables[b'cmap']
maps=[]
for i in range(u16(cmap+2)):
    platform,encoding,offset=struct.unpack_from('>HHI',data,cmap+4+8*i)
    subtable=cmap+offset
    if u16(subtable)==4:maps.append(subtable)

def glyph(cp):
    for table in maps:
        count=u16(table+6)//2
        ends=table+14;starts=ends+count*2+2;deltas=starts+count*2;ranges=deltas+count*2
        for i in range(count):
            if u16(starts+i*2)<=cp<=u16(ends+i*2):
                delta=u16(deltas+i*2);ro=u16(ranges+i*2)
                if not ro:return (cp+delta)&65535
                value=u16(ranges+i*2+ro+2*(cp-u16(starts+i*2)))
                if value:return (value+delta)&65535
    return 0

sample='АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя0123456789:./—×<>_%'
missing=[hex(ord(c)) for c in sample if not glyph(ord(c))]
assert not missing,missing
print('font_glyphs_verified',len(sample),'missing',len(missing))
