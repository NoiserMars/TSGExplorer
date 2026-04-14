#!/usr/bin/env python3
"""
tsg_tool — The Simpsons Game asset extraction & conversion toolkit

Parses Asura engine containers and extracts/converts all embedded assets:
textures, models, audio, dialogue, scripts, level geometry, and more.

Supports both Wii (big-endian) and PS2 (little-endian) builds.
File types: .wii, .PS2, .enBE, .EN, .asr, .ASR, .guiBE, .GUI, .asrBE

Subcommands:
  info      — Container structure and chunk census
  extract   — Raw chunks and named files
  textures  — TPL/TIM2 → PNG
  models    — Props + characters → OBJ
  dialogue  — NLLD subtitles → CSV
  audio     — DSP ADPCM / VAG / Bink Audio → WAV
  script    — GSMS bytecode → decoded CSV
  env       — Level geometry → OBJ
  text      — TXTH localized strings → CSV

Also usable as a library — import and call parse/convert functions directly.
"""

import struct, os, sys, zlib, argparse, csv, base64
from collections import defaultdict

# ============================================================
# Asura Container Parser
# ============================================================

# Known chunk IDs in canonical (Wii big-endian) form
_KNOWN_CHUNK_IDS = {
    'FCSR','ITNE','NACH','MSDS','VELD','VEDS','TPXF','TSXF','TXET','LFXT',
    'LRTM','NKSH','BBSH','DNSH','TEXF','RTTC','CATC','TPMH','STUC','VETC',
    'XETA','LBTA','NAXT','GSMS','OFNF','LFSR','AMDS','NAIU','PMIU','gulp',
    'NEHP','DOME','BABL','TRTA','NSIG','HPDS','BVRM','NSBS','SUMM','1VAN',
    'NILM','DNER','BYKS','RHTW',' GOF','TATC','ANRC','DPHS','PAHS','NLLD',
    'TXTH','EULB','DDAP','TXTT','TLLD','TAXA',
}

def _detect_endian(data):
    """Detect endianness from first chunk ID after Asura header.
    Returns '>' for big-endian (Wii/GC) or '<' for little-endian (PS2)."""
    if len(data) < 12: return '>'
    cid_raw = data[8:12].decode('ascii', errors='replace')
    if cid_raw in _KNOWN_CHUNK_IDS:
        return '>'
    cid_rev = cid_raw[::-1]
    if cid_rev in _KNOWN_CHUNK_IDS:
        return '<'
    # Fallback: try parsing chunk_size both ways, see which is sane
    sz_be = struct.unpack_from('>I', data, 12)[0]
    sz_le = struct.unpack_from('<I', data, 12)[0]
    if 16 < sz_le < len(data) and (sz_be < 16 or sz_be > len(data)):
        return '<'
    return '>'

# Endian-aware read helpers
def _u32(d, off, e='>'): return struct.unpack_from(e+'I', d, off)[0]
def _i32(d, off, e='>'): return struct.unpack_from(e+'i', d, off)[0]
def _u16(d, off, e='>'): return struct.unpack_from(e+'H', d, off)[0]
def _i16(d, off, e='>'): return struct.unpack_from(e+'h', d, off)[0]
def _f32(d, off, e='>'): return struct.unpack_from(e+'f', d, off)[0]

def _get_endian(chunks):
    """Extract endianness from a chunk list (stored in each chunk dict)."""
    if chunks and 'endian' in chunks[0]:
        return chunks[0]['endian']
    return '>'

def _platform_name(endian):
    return 'PS2' if endian == '<' else 'Wii'

def read_asura(path):
    """Read and decompress an Asura container."""
    data = open(path, 'rb').read()
    magic = data[:8]
    if magic == b'Asura   ':
        return data
    elif magic == b'AsuraZlb':
        return _decompress_zlb(data)
    elif magic == b'AsuraZbb':
        return _decompress_zbb(data)
    else:
        raise ValueError(f"Unknown Asura magic: {magic}")

def _decompress_zlb(data):
    result = b''
    off = 12
    while off < len(data) - 8:
        csz = struct.unpack_from('>I', data, off)[0]
        usz = struct.unpack_from('>I', data, off+4)[0]
        if csz == 0 or off + 8 + csz > len(data): break
        result += zlib.decompress(data[off+8:off+8+csz])
        off += 8 + csz
    if result[:8] != b'Asura   ':
        result = b'Asura   ' + result
    return result

def _decompress_zbb(data):
    full_size = struct.unpack_from('>I', data, 12)[0]
    result = b''
    off = 16
    while off < len(data) - 8 and len(result) < full_size:
        csz = struct.unpack_from('>I', data, off)[0]
        if csz == 0 or off + 8 + csz > len(data): break
        result += zlib.decompress(data[off+8:off+8+csz])
        off += 8 + csz
    return b'Asura   ' + result

def parse_chunks(data, endian=None):
    """Parse an Asura container into chunk list.
    Endianness is auto-detected if not specified.
    Chunk IDs are normalized to canonical (Wii) form regardless of platform."""
    if data[:8] != b'Asura   ':
        raise ValueError(f"Not Asura container")
    if endian is None:
        endian = _detect_endian(data)
    is_le = (endian == '<')
    chunks = []
    pos = 8
    while pos + 16 <= len(data):
        cid_raw = data[pos:pos+4].decode('ascii', errors='replace')
        cid = cid_raw[::-1] if is_le else cid_raw
        csz = struct.unpack_from(endian+'I', data, pos+4)[0]
        ver = struct.unpack_from(endian+'I', data, pos+8)[0]
        unk = struct.unpack_from(endian+'I', data, pos+12)[0]
        if csz < 16 or pos + csz > len(data): break
        chunks.append({
            'id': cid, 'size': csz, 'ver': ver, 'unk': unk,
            'content': data[pos+16:pos+csz], 'offset': pos,
            'endian': endian
        })
        pos += csz
    return chunks

def extract_fcsr_files(chunks):
    """Extract named files from FCSR chunks. Returns list of {name, data, chunk_ver}."""
    files = []
    for c in chunks:
        if c['id'] != 'FCSR': continue
        d = c['content']
        e = c.get('endian', '>')
        if len(d) < 16: continue
        fsize = _u32(d, 8, e)
        null = d[12:].find(b'\x00')
        if null < 0 or fsize > len(d): continue
        fname = d[12:12+null].decode('ascii', errors='replace')
        fdata = d[len(d)-fsize:]
        files.append({'name': fname, 'data': fdata, 'chunk_size': len(d),
                      'chunk_ver': c['ver'], 'endian': e})
    return files

# ============================================================
# TPL Texture Decoder
# ============================================================

TPL_MAGIC = 0x0020AF30

def parse_tpl(data):
    if len(data) < 12: return None
    if struct.unpack_from('>I', data, 0)[0] != TPL_MAGIC: return None
    n = struct.unpack_from('>I', data, 4)[0]
    tbl = struct.unpack_from('>I', data, 8)[0]
    imgs = []
    for i in range(n):
        eo = tbl + i*8
        if eo+8 > len(data): break
        iho = struct.unpack_from('>I', data, eo)[0]
        pho = struct.unpack_from('>I', data, eo+4)[0]  # palette header offset
        if iho+12 > len(data): break
        h = struct.unpack_from('>H', data, iho)[0]
        w = struct.unpack_from('>H', data, iho+2)[0]
        fmt = struct.unpack_from('>I', data, iho+4)[0]
        doff = struct.unpack_from('>I', data, iho+8)[0]
        if w == 0 or h == 0: continue
        entry = {'w':w,'h':h,'fmt':fmt,'doff':doff}
        if pho != 0 and pho+12 <= len(data):
            pal_n = struct.unpack_from('>H', data, pho)[0]
            pal_fmt = struct.unpack_from('>I', data, pho+4)[0]
            pal_doff = struct.unpack_from('>I', data, pho+8)[0]
            if pal_n > 0 and pal_doff + pal_n*2 <= len(data):
                entry['pal_n'] = pal_n
                entry['pal_fmt'] = pal_fmt
                entry['pal_doff'] = pal_doff
        imgs.append(entry)
    return imgs if imgs else None

def _decode_i4(data,w,h):
    px=bytearray(w*h);s=0
    for ty in range((h+7)//8):
        for tx in range((w+7)//8):
            for r in range(8):
                for c in range(0,8,2):
                    if s>=len(data): break
                    b=data[s];s+=1
                    hi=(b>>4)&0xF;lo=b&0xF;hi|=hi<<4;lo|=lo<<4
                    x,y=tx*8+c,ty*8+r
                    if x<w and y<h: px[y*w+x]=hi
                    if x+1<w and y<h: px[y*w+x+1]=lo
    return px,'L'

def _decode_i8(data,w,h):
    px=bytearray(w*h);s=0
    for ty in range((h+3)//4):
        for tx in range((w+7)//8):
            for r in range(4):
                for c in range(8):
                    if s>=len(data): break
                    x,y=tx*8+c,ty*4+r
                    if x<w and y<h: px[y*w+x]=data[s]
                    s+=1
    return px,'L'

def _decode_ia4(data,w,h):
    px=bytearray(w*h*2);s=0
    for ty in range((h+3)//4):
        for tx in range((w+7)//8):
            for r in range(4):
                for c in range(8):
                    if s>=len(data): break
                    b=data[s];s+=1
                    a=(b>>4)&0xF;a|=a<<4;i=b&0xF;i|=i<<4
                    x,y=tx*8+c,ty*4+r
                    if x<w and y<h: idx=(y*w+x)*2;px[idx]=i;px[idx+1]=a
    return px,'LA'

def _decode_ia8(data,w,h):
    px=bytearray(w*h*2);s=0
    for ty in range((h+3)//4):
        for tx in range((w+3)//4):
            for r in range(4):
                for c in range(4):
                    if s+1>=len(data): break
                    a=data[s];i=data[s+1];s+=2
                    x,y=tx*4+c,ty*4+r
                    if x<w and y<h: idx=(y*w+x)*2;px[idx]=i;px[idx+1]=a
    return px,'LA'

def _decode_rgb565(data,w,h):
    px=bytearray(w*h*3);s=0
    for ty in range((h+3)//4):
        for tx in range((w+3)//4):
            for r in range(4):
                for c in range(4):
                    if s+1>=len(data): break
                    v=struct.unpack_from('>H',data,s)[0];s+=2
                    cr=((v>>11)&0x1F)*255//31;cg=((v>>5)&0x3F)*255//63;cb=(v&0x1F)*255//31
                    x,y=tx*4+c,ty*4+r
                    if x<w and y<h: idx=(y*w+x)*3;px[idx]=cr;px[idx+1]=cg;px[idx+2]=cb
    return px,'RGB'

def _decode_rgb5a3(data,w,h):
    px=bytearray(w*h*4);s=0
    for ty in range((h+3)//4):
        for tx in range((w+3)//4):
            for r in range(4):
                for c in range(4):
                    if s+1>=len(data): break
                    v=struct.unpack_from('>H',data,s)[0];s+=2
                    if v&0x8000:
                        cr=((v>>10)&0x1F)*255//31;cg=((v>>5)&0x1F)*255//31;cb=(v&0x1F)*255//31;ca=255
                    else:
                        ca=((v>>12)&7)*255//7;cr=((v>>8)&0xF)*255//15;cg=((v>>4)&0xF)*255//15;cb=(v&0xF)*255//15
                    x,y=tx*4+c,ty*4+r
                    if x<w and y<h: idx=(y*w+x)*4;px[idx]=cr;px[idx+1]=cg;px[idx+2]=cb;px[idx+3]=ca
    return px,'RGBA'

def _decode_rgba8(data,w,h):
    px=bytearray(w*h*4);s=0
    for ty in range((h+3)//4):
        for tx in range((w+3)//4):
            ar=[]
            for i in range(16):
                if s+1<len(data): ar.append((data[s],data[s+1]));s+=2
                else: ar.append((255,0))
            gb=[]
            for i in range(16):
                if s+1<len(data): gb.append((data[s],data[s+1]));s+=2
                else: gb.append((0,0))
            for r in range(4):
                for c in range(4):
                    i=r*4+c;a,cr=ar[i];cg,cb=gb[i]
                    x,y=tx*4+c,ty*4+r
                    if x<w and y<h: idx=(y*w+x)*4;px[idx]=cr;px[idx+1]=cg;px[idx+2]=cb;px[idx+3]=a
    return px,'RGBA'

def _rgb565(c):
    return(((c>>11)&0x1F)*255//31,((c>>5)&0x3F)*255//63,(c&0x1F)*255//31)

def _decode_cmpr(data,w,h):
    px=[(0,0,0,255)]*(w*h);s=0
    for ty in range((h+7)//8):
        for tx in range((w+7)//8):
            for sy in range(2):
                for sx in range(2):
                    if s+8>len(data): break
                    c0=struct.unpack_from('>H',data,s)[0];c1=struct.unpack_from('>H',data,s+2)[0]
                    r0,g0,b0=_rgb565(c0);r1,g1,b1=_rgb565(c1)
                    cl=[(r0,g0,b0,255),(r1,g1,b1,255),(0,0,0,255),(0,0,0,0)]
                    if c0>c1:
                        cl[2]=((2*r0+r1+1)//3,(2*g0+g1+1)//3,(2*b0+b1+1)//3,255)
                        cl[3]=((r0+2*r1+1)//3,(g0+2*g1+1)//3,(b0+2*b1+1)//3,255)
                    else:
                        cl[2]=((r0+r1+1)//2,(g0+g1+1)//2,(b0+b1+1)//2,255)
                        # cl[3] stays (0,0,0,0) = transparent
                    blk=[]
                    for r in range(4):
                        b=data[s+4+r]
                        for c in range(4): blk.append(cl[(b>>(6-2*c))&3])
                    s+=8
                    bx,by=tx*8+sx*4,ty*8+sy*4
                    for r in range(4):
                        for c in range(4):
                            x,y=bx+c,by+r
                            if x<w and y<h: px[y*w+x]=blk[r*4+c]
    # Convert to flat bytes
    out=bytearray(w*h*4)
    for i,(r,g,b,a) in enumerate(px): out[i*4]=r;out[i*4+1]=g;out[i*4+2]=b;out[i*4+3]=a
    return out,'RGBA'

_DECODERS={0:_decode_i4,1:_decode_i8,2:_decode_ia4,3:_decode_ia8,
           4:_decode_rgb565,5:_decode_rgb5a3,6:_decode_rgba8,
           8:_decode_i4,9:_decode_i8,  # CI4/CI8 same pixel layout as I4/I8
           14:_decode_cmpr}

# Simpsons palette LUT — extracted from game runtime TLUT (RGB565, 256 entries)
# Source: Dolphin Emulator MEM1 dump, GXTlutObj at VA 0x80589E38, data at 0x808F8920
_PAL_B64='AAAA/xgYGP9BREr/SkxS/2Jpav+LkZT/lJ2k/7S6xf/V0tX/3t7e/+bi5v///////////////////////////wA8c/8YVaT/IGXe/xiJ1f8gmfb/UqHm/3vO7v85hb3/SoG9/1J1pP9aeZz/UmmL/0FQc/////////////////8pFEH/GBA5/xAMQf8pEFr/IAB7/yAAe/8YKIP/KTy0/1JZzf9zgeb/c4Hm/6TC7v+90v//zeL//83m////////ECwx/xhMWv85cYv/MXmU/3OqtP+LvsX/lMbF/7Ta1f+DoaT/AJmU/0HOxf+c5ub/pOb2/////////////////wgcAP8AHBj/ACgg/wA4Mf8AOEH/AFBK/xBQSv85VVL/EG1i/xhhSv9BdSn/Yo1B/6zCg/+9siD/i4EQ/1JQGP8YQAD/AGEA/xiBIP9Bzhj/g9Ip/7TaUv+swhj/GHVB/zGNYv9Kqnv/lOKk/83/1f/e+u7/////////////////KSQA/0o4GP9iTCD/al0g/+7OYv/22nP///Ks///61f//7mr/9uIA///KGP//yiD/3qEA/82VKf///////////ykQCP85JBj/Ujgp/1I4IP9SNAD/c0gp/5SBav+ckYP/va6U/722rP/m1sX/tKqs/5yFi/+cfXv/g11a/3NMUv9BGBj/WiwI/2I0GP97PAD/nEwA/7R5Wv/FlXv/3q6U/+ahc///rnP//7qD///GlP//zqT//////5Rhav+shYv/g1kg/5RpMf+UXRj/pG0p/6RtGP+kbRj/pHE5/6x9Sv/NkUr/1api/+7Ki///4rT//////////////////////71QAP+9WRj/vWEA/8VlAP/mWQD/9l0A//91Of//hQD//5VK//////////////////////////////////////+sWTn/rExB/95tWv/mjWr//41q//+hi///oaz//42U//aJlP/ucYP/////////////////////////////////KQwI/1ocEP97KCD/lDAY/4ssOf97ECn/ewQQ/4MMIP+kADH/5jha//9dg////////////////////////////8UEIP/NAAD/7gAA/+YkWv/mJFr/9gBq//9ttP/2fcX//4nF//+hxf//ob3//8LV/9VMlP////////////////85HDH/OQAx/1IkQf9SFEr/YgBi/3M4av+cZZz/lAiL/71dtP/umeb//87///bK7v//////////////////////ajyk/4txtP+kmd7/vbb//97W//+cqtX/g4Wk/3Nli////////////////////////////////////////////w=='

def _get_palette_lut():
    import numpy as np
    return np.frombuffer(base64.b64decode(_PAL_B64), dtype=np.uint8).reshape(256,4).copy()

def _decode_tpl_palette(data, pal_n, pal_fmt, pal_doff):
    """Decode a TPL embedded palette into a (N,4) RGBA numpy array."""
    import numpy as np
    lut = np.zeros((pal_n, 4), dtype=np.uint8)
    for i in range(pal_n):
        off = pal_doff + i * 2
        if off + 2 > len(data): break
        val = struct.unpack_from('>H', data, off)[0]
        if pal_fmt == 2:  # RGB5A3
            if val & 0x8000:
                r = ((val >> 10) & 0x1F) * 255 // 31
                g = ((val >> 5) & 0x1F) * 255 // 31
                b = (val & 0x1F) * 255 // 31
                a = 255
            else:
                a = ((val >> 12) & 0x7) * 255 // 7
                r = ((val >> 8) & 0xF) * 255 // 15
                g = ((val >> 4) & 0xF) * 255 // 15
                b = (val & 0xF) * 255 // 15
        elif pal_fmt == 1:  # RGB565
            r = ((val >> 11) & 0x1F) * 255 // 31
            g = ((val >> 5) & 0x3F) * 255 // 63
            b = (val & 0x1F) * 255 // 31
            a = 255
        elif pal_fmt == 0:  # IA8
            a = (val >> 8) & 0xFF
            r = g = b = val & 0xFF
        else:
            r = g = b = 128; a = 255
        lut[i] = (r, g, b, a)
    return lut

def convert_tpl_to_png(tpl_data, output_path, use_palette=True):
    """Convert a TPL texture to PNG. Requires PIL."""
    from PIL import Image
    import numpy as np
    
    imgs = parse_tpl(tpl_data)
    if imgs is None: return False
    
    i0 = imgs[0]
    decoder = _DECODERS.get(i0['fmt'])
    if decoder is None: return False
    
    px, mode = decoder(tpl_data[i0['doff']:], i0['w'], i0['h'])
    img = Image.frombytes(mode, (i0['w'], i0['h']), bytes(px))
    
    # Apply palette: prefer embedded TPL palette (CI8/CI4), fall back to hardcoded LUT (I8)
    if 'pal_n' in i0 and i0['fmt'] in (8, 9) and img.mode == 'L':
        lut = _decode_tpl_palette(tpl_data, i0['pal_n'], i0['pal_fmt'], i0['pal_doff'])
        indices = np.clip(np.array(img), 0, len(lut) - 1)
        img = Image.fromarray(lut[indices], 'RGBA')
    elif use_palette and i0['fmt'] in (1, 9) and img.mode == 'L':
        lut = _get_palette_lut()
        img = Image.fromarray(lut[np.array(img)], 'RGBA')
    
    # Composite alpha from second TPL image
    if len(imgs) >= 2:
        i1 = imgs[1]
        dec1 = _DECODERS.get(i1['fmt'])
        if dec1:
            px1, m1 = dec1(tpl_data[i1['doff']:], i1['w'], i1['h'])
            alpha = Image.frombytes(m1, (i1['w'], i1['h']), bytes(px1))
            if alpha.size != img.size:
                alpha = alpha.resize(img.size, Image.NEAREST)
            if alpha.mode != 'L': alpha = alpha.convert('L')
            if img.mode in ('L','RGB','LA'): img = img.convert('RGBA')
            r,g,b,_ = img.split()
            img = Image.merge('RGBA', (r,g,b,alpha))
    
    # Apply magenta chroma key for CMPR textures
    # Magenta (255,0,255) is the standard transparency key in Asura CMPR textures.
    # CMPR 1-bit alpha handles most transparent pixels, but opaque-mode blocks (c0>c1)
    # can still contain magenta key-color pixels that should be transparent.
    if i0['fmt'] == 14 and img.mode == 'RGBA':
        import numpy as _np
        arr = _np.array(img)
        magenta = (arr[:,:,0] > 240) & (arr[:,:,1] < 16) & (arr[:,:,2] > 240)
        arr[magenta, 3] = 0
        img = Image.fromarray(arr, 'RGBA')
    
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    img.save(output_path)
    return True

# ============================================================
# TIM2 Texture Decoder (PS2)
# ============================================================

def _unswizzle_palette_8bit(palette):
    """Unswizzle a 256-entry PS2 CLUT palette (CSM1 rearrangement).
    PS2 stores 256-color palettes in groups of 32 with interleaved order:
    [0-7, 16-23, 8-15, 24-31] repeated for each block of 32."""
    if len(palette) != 256:
        return palette
    out = [None] * 256
    for i in range(256):
        block = (i // 32) * 32
        sub = i % 32
        if sub < 8:
            out[block + sub] = palette[block + sub]
        elif sub < 16:
            out[block + sub] = palette[block + sub + 8]
        elif sub < 24:
            out[block + sub] = palette[block + sub - 8]
        else:
            out[block + sub] = palette[block + sub]
    return out

def parse_tim2(data):
    """Parse a TIM2 texture file. Returns list of {width, height, pixels_rgba} or None."""
    if len(data) < 16 or data[:4] != b'TIM2':
        return None
    version = data[4]
    img_count = struct.unpack_from('<H', data, 6)[0]
    if img_count == 0: return None

    # TIM2 v4+ has image entries at offset 128, earlier versions at 16
    off = 128 if version >= 4 else 16

    results = []
    for _ in range(img_count):
        if off + 48 > len(data): break
        total_size = struct.unpack_from('<I', data, off)[0]
        palette_size = struct.unpack_from('<I', data, off+4)[0]
        img_data_size = struct.unpack_from('<I', data, off+8)[0]
        hdr_size = struct.unpack_from('<H', data, off+12)[0]
        n_colors = struct.unpack_from('<H', data, off+14)[0]
        psmct = data[off+16]         # PS2 GS pixel storage mode (0=PSMCT32, 19=PSMT8, 20=PSMT4)
        mipmaps = data[off+17]
        clut_fmt = data[off+18]
        img_type = data[off+19]      # color depth: 1=16bit 2=24bit 3=32bit 4=4bit-idx 5=8bit-idx
        width = struct.unpack_from('<H', data, off+20)[0]
        height = struct.unpack_from('<H', data, off+22)[0]

        if width == 0 or height == 0:
            off += total_size; continue

        pix_start = off + hdr_size
        pal_start = pix_start + img_data_size

        # Determine actual pixel format from psmct (GS register) and img_type
        # psmct=0 (PSMCT32) → 32-bit RGBA regardless of palette presence
        # psmct=19 (PSMT8) → 8-bit indexed
        # psmct=20 (PSMT4) → 4-bit indexed
        if psmct == 0:
            actual_bpp = 3  # 32-bit RGBA
        elif psmct in (19, 27):
            actual_bpp = 5  # 8-bit indexed
        elif psmct in (20, 36, 44):
            actual_bpp = 4  # 4-bit indexed
        elif psmct in (2, 10):
            actual_bpp = 1  # 16-bit
        elif psmct == 1:
            actual_bpp = 2  # 24-bit
        else:
            actual_bpp = img_type  # fallback to img_type

        pixels = bytearray(width * height * 4)

        if actual_bpp == 5 and n_colors > 0:
            # 8-bit indexed with palette
            palette = []
            pal_bpp = 4 if img_type == 3 else 2  # 32-bit or 16-bit palette entries
            for i in range(n_colors):
                po = pal_start + i * pal_bpp
                if po + pal_bpp > len(data): break
                if pal_bpp == 4:
                    r, g, b, a = data[po], data[po+1], data[po+2], data[po+3]
                    a = min(255, a * 2) if a < 128 else 255
                else:
                    val = struct.unpack_from('<H', data, po)[0]
                    r = (val & 0x1F) << 3; g = ((val >> 5) & 0x1F) << 3
                    b = ((val >> 10) & 0x1F) << 3; a = 255 if (val & 0x8000) else 0
                palette.append((r, g, b, a))
            while len(palette) < 256:
                palette.append((0, 0, 0, 255))
            palette = _unswizzle_palette_8bit(palette)

            for y in range(height):
                for x in range(width):
                    si = y * width + x
                    if pix_start + si >= len(data): break
                    idx = data[pix_start + si]
                    if idx < len(palette):
                        r, g, b, a = palette[idx]
                    else:
                        r, g, b, a = 0, 0, 0, 255
                    po = (y * width + x) * 4
                    pixels[po] = r; pixels[po+1] = g; pixels[po+2] = b; pixels[po+3] = a

        elif actual_bpp == 4 and n_colors > 0:
            # 4-bit indexed with palette
            palette = []
            pal_bpp = 4 if img_type == 3 else 2
            for i in range(min(n_colors, 16)):
                po = pal_start + i * pal_bpp
                if po + pal_bpp > len(data): break
                if pal_bpp == 4:
                    r, g, b, a = data[po], data[po+1], data[po+2], data[po+3]
                    a = min(255, a * 2) if a < 128 else 255
                else:
                    val = struct.unpack_from('<H', data, po)[0]
                    r = (val & 0x1F) << 3; g = ((val >> 5) & 0x1F) << 3
                    b = ((val >> 10) & 0x1F) << 3; a = 255 if (val & 0x8000) else 0
                palette.append((r, g, b, a))
            while len(palette) < 16:
                palette.append((0, 0, 0, 255))

            for y in range(height):
                for x in range(width):
                    si = (y * width + x) // 2
                    if pix_start + si >= len(data): break
                    byte = data[pix_start + si]
                    idx = (byte & 0x0F) if (x % 2 == 0) else ((byte >> 4) & 0x0F)
                    if idx < len(palette):
                        r, g, b, a = palette[idx]
                    else:
                        r, g, b, a = 0, 0, 0, 255
                    po = (y * width + x) * 4
                    pixels[po] = r; pixels[po+1] = g; pixels[po+2] = b; pixels[po+3] = a

        elif actual_bpp == 3:
            # 32-bit direct RGBA
            for y in range(height):
                for x in range(width):
                    si = pix_start + (y * width + x) * 4
                    if si + 4 > len(data): break
                    r, g, b, a = data[si], data[si+1], data[si+2], data[si+3]
                    a = min(255, a * 2) if a < 128 else 255
                    po = (y * width + x) * 4
                    pixels[po] = r; pixels[po+1] = g; pixels[po+2] = b; pixels[po+3] = a

        elif actual_bpp == 2:
            # 24-bit direct RGB
            for y in range(height):
                for x in range(width):
                    si = pix_start + (y * width + x) * 3
                    if si + 3 > len(data): break
                    r, g, b = data[si], data[si+1], data[si+2]
                    po = (y * width + x) * 4
                    pixels[po] = r; pixels[po+1] = g; pixels[po+2] = b; pixels[po+3] = 255

        elif actual_bpp == 1:
            # 16-bit direct ABGR1555
            for y in range(height):
                for x in range(width):
                    si = pix_start + (y * width + x) * 2
                    if si + 2 > len(data): break
                    val = struct.unpack_from('<H', data, si)[0]
                    r = (val & 0x1F) << 3; g = ((val >> 5) & 0x1F) << 3
                    b = ((val >> 10) & 0x1F) << 3; a = 255 if (val & 0x8000) else 0
                    po = (y * width + x) * 4
                    pixels[po] = r; pixels[po+1] = g; pixels[po+2] = b; pixels[po+3] = a
        else:
            off += total_size
            continue

        results.append({'width': width, 'height': height, 'pixels': bytes(pixels),
                        'bpp_type': actual_bpp, 'n_colors': n_colors, 'psmct': psmct})
        off += total_size

    return results if results else None

def convert_tim2_to_png(tim2_data, output_path):
    """Convert a TIM2 texture to PNG. Requires PIL."""
    from PIL import Image
    imgs = parse_tim2(tim2_data)
    if not imgs: return False
    img = imgs[0]
    pil = Image.frombytes('RGBA', (img['width'], img['height']), img['pixels'])
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    pil.save(output_path)
    return True

# ============================================================
# Model Converter
# ============================================================
#
# FORMATS (confirmed via DWARF debug symbols in proto ELF):
#
# Prop model (file version 6):
#   28-byte header + (submesh-1)*8 element table
#   vertices: vtx_count × 16 bytes (int16 XYZ + pad + normal_idx + int16 UV)
#   indices:  idx_count × 2 bytes  (tristrip uint16)
#
# Character model / Asura_GX_SmoothSkin (FCSR chunk version 0-2):
#   File layout (version 2, most common):
#     hash(4) + matID(4) + nIdx(4) + nVtx(4) + nBoneInfos(4) + nElem(4) + colDepth(4)
#     colours[colDepth × nVtx]
#     elements[nElem × 8]      → {uint32 m_uMaxIndex, int32 m_iElementID}
#     vertices[nVtx × 10]      → {int16 X,Y,Z, int8 NX,NY,NZ, int8 pad}
#     texinfo[nVtx × 4]        → {int16 U, int16 V} (scale /1024)
#     boneinfo[nBoneInfos × 2] → {uint8 m_ucBoneIndex, uint8 m_ucBoneWeight}
#     indices[nIdx × 2]        → tristrip uint16
#
#   Version 1: no colDepth field (colDepth=0)
#   Version 0: no nElem or colDepth fields (nElem=1, colDepth=0)

def _tristrip_to_tris(indices, max_idx=None):
    tris = []
    for i in range(len(indices)-2):
        i0,i1,i2 = indices[i],indices[i+1],indices[i+2]
        if i0==i1 or i1==i2 or i0==i2: continue
        if max_idx is not None and (i0>=max_idx or i1>=max_idx or i2>=max_idx): continue
        if i%2==0: tris.append((i0,i1,i2))
        else: tris.append((i0,i2,i1))
    return tris

def _parse_smoothskin(data, chunk_ver):
    """Parse a character model (Asura_GX_SmoothSkin) from file data.
    chunk_ver is the FCSR chunk version (0, 1, or 2).
    Returns dict with all arrays, or None on failure."""
    if len(data) < 20: return None
    off = 4  # skip hash
    nIdx     = struct.unpack_from('>I', data, off+4)[0]
    nVtx     = struct.unpack_from('>I', data, off+8)[0]
    nBoneInf = struct.unpack_from('>I', data, off+12)[0]
    nElem    = struct.unpack_from('>I', data, off+16)[0] if chunk_ver >= 1 else 1
    colDepth = struct.unpack_from('>I', data, off+20)[0] if chunk_ver >= 2 else 0

    hdr_end = off + (24 if chunk_ver >= 2 else 20 if chunk_ver >= 1 else 16)
    col_off  = hdr_end
    elem_off = col_off + colDepth * nVtx
    vtx_off  = elem_off + nElem * 8
    tex_off  = vtx_off + nVtx * 10
    bone_off = tex_off + nVtx * 4
    idx_off  = bone_off + nBoneInf * 2
    expected = idx_off + nIdx * 2

    if expected != len(data) or nVtx > 50000 or nIdx > 100000:
        return None

    # Elements
    elements = []
    for i in range(nElem):
        o = elem_off + i * 8
        elements.append((struct.unpack_from('>I', data, o)[0],
                          struct.unpack_from('>i', data, o+4)[0]))

    # Vertices (10-byte stride)
    positions, normals, uvs = [], [], []
    for i in range(nVtx):
        o = vtx_off + i * 10
        x = struct.unpack_from('>h', data, o)[0]
        y = struct.unpack_from('>h', data, o+2)[0]
        z = struct.unpack_from('>h', data, o+4)[0]
        nx = struct.unpack_from('>b', data, o+6)[0]
        ny = struct.unpack_from('>b', data, o+7)[0]
        nz = struct.unpack_from('>b', data, o+8)[0]
        positions.append((x, y, z))
        normals.append((nx / 127.0, ny / 127.0, nz / 127.0))
        u = struct.unpack_from('>h', data, tex_off + i*4)[0]
        v = struct.unpack_from('>h', data, tex_off + i*4 + 2)[0]
        uvs.append((u / 1024.0, v / 1024.0))

    # Bone info (separate array, 2-byte stride)
    bone_info = []
    for i in range(nBoneInf):
        o = bone_off + i * 2
        bone_info.append((data[o], data[o+1]))  # (bone_index, bone_weight)

    # Indices
    indices = [struct.unpack_from('>H', data, idx_off + i*2)[0] for i in range(nIdx)]

    return {
        'nVtx': nVtx, 'nIdx': nIdx, 'nBoneInf': nBoneInf,
        'nElem': nElem, 'colDepth': colDepth,
        'elements': elements, 'positions': positions, 'normals': normals,
        'uvs': uvs, 'bone_info': bone_info, 'indices': indices
    }


def _parse_smoothskin_cv3(data):
    """Parse a cv3 character model (Final build SmoothSkin with display lists).
    Returns dict with positions, uvs, bone_info, and triangles from display list."""
    if len(data) < 36: return None
    off = 0
    hash_val = struct.unpack_from('>I', data, off)[0]; off += 4
    matID = struct.unpack_from('>I', data, off)[0]; off += 4
    someCount = struct.unpack_from('>I', data, off)[0]; off += 4
    dlBlockSize = struct.unpack_from('>I', data, off)[0]; off += 4
    nIdx = struct.unpack_from('>I', data, off)[0] + 2; off += 4
    nVtx = struct.unpack_from('>I', data, off)[0]; off += 4
    nBoneInf = struct.unpack_from('>I', data, off)[0]; off += 4
    nElem = struct.unpack_from('>I', data, off)[0]; off += 4
    colDepth = struct.unpack_from('>I', data, off)[0]; off += 4

    if nVtx > 50000 or nBoneInf > 100000: return None

    vtx_stride = 10 if someCount == 1 else 6
    col_off = off
    elem_off = col_off + colDepth * nVtx
    vtx_off = elem_off + nElem * 8
    tex_off = vtx_off + nVtx * vtx_stride
    bone_off = tex_off + nVtx * 4
    dl_off = bone_off + nBoneInf * 2

    if dl_off + dlBlockSize > len(data) + 16: return None  # tolerance

    # Vertices
    positions = []
    normals = []
    for i in range(nVtx):
        o = vtx_off + i * vtx_stride
        if o + 6 > len(data): break
        x = struct.unpack_from('>h', data, o)[0]
        y = struct.unpack_from('>h', data, o+2)[0]
        z = struct.unpack_from('>h', data, o+4)[0]
        positions.append((x, y, z))
        if vtx_stride >= 10 and o + 10 <= len(data):
            nx = struct.unpack_from('>b', data, o+6)[0] / 127.0
            ny = struct.unpack_from('>b', data, o+7)[0] / 127.0
            nz = struct.unpack_from('>b', data, o+8)[0] / 127.0
            normals.append((nx, ny, nz))
        else:
            normals.append((0.0, 1.0, 0.0))

    # UVs
    uvs = []
    for i in range(nVtx):
        o = tex_off + i * 4
        if o + 4 > len(data): break
        u = struct.unpack_from('>h', data, o)[0] / 1024.0
        v = struct.unpack_from('>h', data, o+2)[0] / 1024.0
        uvs.append((u, v))

    # Bone info
    bone_info = []
    for i in range(nBoneInf):
        o = bone_off + i * 2
        if o + 2 > len(data): break
        bone_info.append((data[o], data[o+1]))

    # Parse display list triangles (6-byte stride: 3 × uint16 position index)
    dl = data[dl_off:dl_off + dlBlockSize]
    indices = []
    doff = 0
    while doff < len(dl) - 3:
        cmd = dl[doff]
        if 0x98 <= cmd <= 0x9f:
            cnt = struct.unpack_from('>H', dl, doff+1)[0]
            stride = 6
            vd = doff + 3
            if vd + cnt * stride <= len(dl) and cnt >= 3:
                pis = []
                for vi in range(cnt):
                    pi = struct.unpack_from('>H', dl, vd + vi * stride)[0]
                    if pi >= nVtx: break
                    pis.append(pi)
                else:
                    for i in range(len(pis) - 2):
                        a, b, c2 = pis[i], pis[i+1], pis[i+2]
                        if a == b or b == c2 or a == c2: continue
                        if i % 2 == 0: indices.append((a, b, c2))
                        else: indices.append((a, c2, b))
                doff = vd + cnt * stride; continue
        doff += 1 if cmd == 0 else 1

    return {
        'nVtx': len(positions), 'nIdx': 0, 'nBoneInf': nBoneInf,
        'nElem': nElem, 'colDepth': colDepth,
        'elements': [], 'positions': positions, 'normals': normals,
        'uvs': uvs, 'bone_info': bone_info, 'indices': [],
        'triangles': indices,  # pre-built triangle list for cv3
    }

def convert_model_to_obj(name, model_data, output_path, chunk_ver=2, scale=1.0/256.0):
    """Convert a Stripped model to OBJ. Returns (success, info_string).
    chunk_ver: FCSR chunk version (0/1/2); only used for character models."""
    data = model_data
    if len(data) < 20:
        return False, "Too small"

    file_ver = struct.unpack_from('>I', data, 4)[0]
    basename = name[8:] if name.startswith('Stripped') else name

    if file_ver == 6:
        # ── Prop model (proto format) ──
        submesh = struct.unpack_from('>I', data, 8)[0]
        idx_count = struct.unpack_from('>I', data, 12)[0]
        vtx_count = struct.unpack_from('>I', data, 16)[0]
        expected = 28 + (submesh-1)*8 + vtx_count*16 + idx_count*2
        if expected != len(data):
            return False, f"Prop size mismatch: {expected} vs {len(data)}"

        vtx_off = 28 + (submesh-1)*8
        idx_off = vtx_off + vtx_count*16

        positions, uvs = [], []
        for i in range(vtx_count):
            off = vtx_off + i*16
            x = struct.unpack_from('>h', data, off)[0] * scale
            y = struct.unpack_from('>h', data, off+2)[0] * scale
            z = struct.unpack_from('>h', data, off+4)[0] * scale
            u = struct.unpack_from('>h', data, off+12)[0] / 1024.0
            v = 1.0 - struct.unpack_from('>h', data, off+14)[0] / 1024.0
            # 180° rotation around X: negate Y and Z (preserves handedness + textures)
            positions.append((x, -y, -z)); uvs.append((u, v))

        indices = [struct.unpack_from('>H', data, idx_off+i*2)[0] for i in range(idx_count)]
        tris = _tristrip_to_tris(indices)
        if not tris: return False, "No triangles"

        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        with open(output_path, 'w') as f:
            f.write(f"# Asura Prop: {basename}\n# {vtx_count} verts, {len(tris)} tris, {submesh} submesh\n\n")
            for x,y,z in positions: f.write(f"v {x:.6f} {y:.6f} {z:.6f}\n")
            f.write("\n")
            for u,v in uvs: f.write(f"vt {u:.6f} {v:.6f}\n")
            f.write(f"\no {basename}\ns 1\n")
            for i0,i1,i2 in tris: f.write(f"f {i0+1}/{i0+1} {i1+1}/{i1+1} {i2+1}/{i2+1}\n")
        return True, f"prop v6: {vtx_count}v {len(tris)}t {submesh}sm"

    elif file_ver == 14:
        # ── Prop model (final format: vertices + GX display lists) ──
        submesh = struct.unpack_from('>I', data, 8)[0]
        dl_size = struct.unpack_from('>I', data, 12)[0]
        vtx_count = struct.unpack_from('>I', data, 16)[0]
        expected = 32 + (submesh-1)*12 + vtx_count*16 + dl_size
        if expected != len(data):
            return False, f"Prop v14 size mismatch: {expected} vs {len(data)}"

        elem_off = 32
        vtx_off = elem_off + (submesh-1)*12
        dl_off = vtx_off + vtx_count*16

        positions, uvs = [], []
        for i in range(vtx_count):
            off = vtx_off + i*16
            x = struct.unpack_from('>h', data, off)[0] * scale
            y = struct.unpack_from('>h', data, off+2)[0] * scale
            z = struct.unpack_from('>h', data, off+4)[0] * scale
            u = struct.unpack_from('>h', data, off+12)[0] / 1024.0
            v = 1.0 - struct.unpack_from('>h', data, off+14)[0] / 1024.0
            positions.append((x, -y, -z)); uvs.append((u, v))

        # Extract triangles from GX display lists (cmd 0x9E tristrip VAT6, 8-byte stride)
        dl = data[dl_off:dl_off+dl_size]
        tris = []
        doff = 0
        while doff < dl_size - 3:
            cmd = dl[doff]
            if cmd in (0x98, 0x99, 0x9A, 0x9B, 0x9C, 0x9D, 0x9E, 0x9F):  # tristrip variants
                cnt = struct.unpack_from('>H', dl, doff+1)[0]
                if 3 <= cnt <= 65535:
                    vd = doff + 3
                    ve = vd + cnt * 8  # 8-byte vertex stride for v14
                    if ve <= dl_size:
                        pis = []
                        ok = True
                        for vi in range(cnt):
                            pi = struct.unpack_from('>H', dl, vd+vi*8)[0]
                            if pi >= vtx_count: ok = False; break
                            pis.append(pi)
                        if ok:
                            for i in range(len(pis)-2):
                                a,b,c = pis[i],pis[i+1],pis[i+2]
                                if a==b or b==c or a==c: continue
                                if i%2==0: tris.append((a,b,c))
                                else: tris.append((a,c,b))
                            doff = ve; continue
                doff += 1
            elif cmd == 0: doff += 1
            else: doff += 1

        if not tris: return False, "No triangles in display list"

        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        with open(output_path, 'w') as f:
            f.write(f"# Asura Prop: {basename}\n# {vtx_count} verts, {len(tris)} tris, {submesh} submesh (v14)\n\n")
            for x,y,z in positions: f.write(f"v {x:.6f} {y:.6f} {z:.6f}\n")
            f.write("\n")
            for u,v in uvs: f.write(f"vt {u:.6f} {v:.6f}\n")
            f.write(f"\no {basename}\ns 1\n")
            for i0,i1,i2 in tris: f.write(f"f {i0+1}/{i0+1} {i1+1}/{i1+1} {i2+1}/{i2+1}\n")
        return True, f"prop v14: {vtx_count}v {len(tris)}t {submesh}sm"

    else:
        # ── Character model (Asura_GX_SmoothSkin) ──
        if chunk_ver >= 3:
            # cv=3 format: header has 2 extra fields, display lists replace indices,
            # vertex stride in file is 6 bytes (no normals) when extraCount != 1
            off = 4  # skip hash
            matID = struct.unpack_from('>I', data, off)[0]; off += 4
            extraCount = struct.unpack_from('>I', data, off)[0]; off += 4
            dlBlockSize = struct.unpack_from('>I', data, off)[0]; off += 4
            nIdx = struct.unpack_from('>I', data, off)[0] + 2; off += 4
            nVtx = struct.unpack_from('>I', data, off)[0]; off += 4
            nBoneInf = struct.unpack_from('>I', data, off)[0]; off += 4
            nElem = struct.unpack_from('>I', data, off)[0]; off += 4
            colDepth = struct.unpack_from('>I', data, off)[0]; off += 4
            # off = 36

            vtx_file_stride = 10 if extraCount == 1 else 6

            # Verify size
            expected = 36 + colDepth*nVtx + nElem*8 + nVtx*vtx_file_stride + nVtx*4 + nBoneInf*2 + dlBlockSize
            if expected != len(data):
                return False, f"SmoothSkin cv3 size mismatch: {expected} vs {len(data)}"

            # Read arrays
            col_off = 36
            elem_off = col_off + colDepth * nVtx
            vtx_off = elem_off + nElem * 8
            tex_off = vtx_off + nVtx * vtx_file_stride
            bone_off = tex_off + nVtx * 4
            dl_off = bone_off + nBoneInf * 2

            positions, uvs = [], []
            for i in range(nVtx):
                o = vtx_off + i * vtx_file_stride
                x = struct.unpack_from('>h', data, o)[0]
                y = struct.unpack_from('>h', data, o+2)[0]
                z = struct.unpack_from('>h', data, o+4)[0]
                positions.append((x, y, z))
                u = struct.unpack_from('>h', data, tex_off + i*4)[0]
                v = struct.unpack_from('>h', data, tex_off + i*4 + 2)[0]
                uvs.append((u / 1024.0, 1.0 - v / 1024.0))

            # Extract triangles from GX display lists
            # Stride is determined by vertex format: 3 uint16 index fields per vertex
            # (position_idx + normal_or_pos_idx + tex_idx, all the same value)
            dl_stride = 6
            dl = data[dl_off:dl_off+dlBlockSize]
            tris = []
            doff = 0
            while doff < dlBlockSize - 3:
                cmd = dl[doff]
                if cmd in (0x98, 0x99, 0x9A, 0x9B, 0x9C, 0x9D, 0x9E, 0x9F):
                    cnt = struct.unpack_from('>H', dl, doff+1)[0]
                    if 3 <= cnt <= 65535:
                        vd = doff + 3
                        ve = vd + cnt * dl_stride
                        if ve <= dlBlockSize:
                            pis, ok2 = [], True
                            for vi in range(cnt):
                                pi = struct.unpack_from('>H', dl, vd+vi*dl_stride)[0]
                                if pi >= nVtx: ok2 = False; break
                                pis.append(pi)
                            if ok2:
                                for i in range(len(pis)-2):
                                    a,b,c = pis[i],pis[i+1],pis[i+2]
                                    if a==b or b==c or a==c: continue
                                    if i%2==0: tris.append((a,b,c))
                                    else: tris.append((a,c,b))
                                doff = ve; continue
                    doff += 1
                elif cmd == 0: doff += 1
                else: doff += 1

            if not tris: return False, f"No triangles in cv3 display list"

            os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
            with open(output_path, 'w') as f:
                f.write(f"# Asura SmoothSkin: {basename}\n")
                f.write(f"# {nVtx}v {len(tris)}t {nElem}elem {nBoneInf}bi (cv3)\n\n")
                for x,y,z in positions:
                    f.write(f"v {x*scale:.6f} {-y*scale:.6f} {-z*scale:.6f}\n")
                f.write("\n")
                for u,v in uvs: f.write(f"vt {u:.6f} {v:.6f}\n")
                f.write(f"\no {basename}\ns 1\n")
                for i0,i1,i2 in tris:
                    f.write(f"f {i0+1}/{i0+1} {i1+1}/{i1+1} {i2+1}/{i2+1}\n")
            return True, f"skin cv3: {nVtx}v {len(tris)}t {nElem}elem"

        mesh = _parse_smoothskin(data, chunk_ver)
        if mesh is None:
            return False, f"SmoothSkin parse failed (chunk_ver={chunk_ver})"

        tris = _tristrip_to_tris(mesh['indices'], mesh['nVtx'])
        if not tris: return False, "No triangles"

        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        with open(output_path, 'w') as f:
            f.write(f"# Asura SmoothSkin: {basename}\n")
            f.write(f"# {mesh['nVtx']}v {len(tris)}t {mesh['nElem']}elem {mesh['nBoneInf']}bi\n\n")
            for x,y,z in mesh['positions']:
                f.write(f"v {x*scale:.6f} {-y*scale:.6f} {-z*scale:.6f}\n")
            f.write("\n")
            for u,v in mesh['uvs']: f.write(f"vt {u:.6f} {1.0-v:.6f}\n")
            f.write("\n")
            for nx,ny,nz in mesh['normals']:
                f.write(f"vn {nx:.6f} {-ny:.6f} {-nz:.6f}\n")
            f.write(f"\no {basename}\ns 1\n")
            for i0,i1,i2 in tris:
                f.write(f"f {i0+1}/{i0+1}/{i0+1} {i1+1}/{i1+1}/{i1+1} {i2+1}/{i2+1}/{i2+1}\n")

        return True, f"skin: {mesh['nVtx']}v {len(tris)}t {mesh['nElem']}elem {mesh['nBoneInf']}bi"

# ============================================================
# NKSH Skeleton Parser & Character Assembly
# ============================================================

import math as _math

def _quat_to_mat3(w, x, y, z):
    """Convert quaternion (w,x,y,z) to 3x3 rotation matrix (numpy-free)."""
    n = _math.sqrt(w*w + x*x + y*y + z*z)
    if n < 1e-8: return [[1,0,0],[0,1,0],[0,0,1]]
    w,x,y,z = w/n, x/n, y/n, z/n
    return [[1-2*(y*y+z*z), 2*(x*y-w*z), 2*(x*z+w*y)],
            [2*(x*y+w*z), 1-2*(x*x+z*z), 2*(y*z-w*x)],
            [2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y)]]

def _mat4_from_rot_pos(rot3, px, py, pz):
    """Build 4x4 matrix from 3x3 rotation + translation."""
    return [rot3[0]+[px], rot3[1]+[py], rot3[2]+[pz], [0,0,0,1]]

def _mat4_mul(a, b):
    """Multiply two 4x4 matrices (lists of lists)."""
    r = [[0]*4 for _ in range(4)]
    for i in range(4):
        for j in range(4):
            for k in range(4):
                r[i][j] += a[i][k] * b[k][j]
    return r

def _mat4_transform(m, x, y, z):
    """Transform point (x,y,z) by 4x4 matrix, return (rx,ry,rz)."""
    return (m[0][0]*x+m[0][1]*y+m[0][2]*z+m[0][3],
            m[1][0]*x+m[1][1]*y+m[1][2]*z+m[1][3],
            m[2][0]*x+m[2][1]*y+m[2][2]*z+m[2][3])

def _mat3_transform(m, x, y, z):
    """Transform vector (x,y,z) by 3x3 rotation part of 4x4 matrix."""
    return (m[0][0]*x+m[0][1]*y+m[0][2]*z,
            m[1][0]*x+m[1][1]*y+m[1][2]*z,
            m[2][0]*x+m[2][1]*y+m[2][2]*z)

def _identity4():
    return [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]

def _read_nksh_xform(nk, off, quat_order='XYWZ'):
    """Read one 28-byte NKSH transform: pos(3f) + quat (order depends on table).
    Rest-pose transforms store quaternion as [X,Y,W,Z] (quat_order='XYWZ').
    Inverse-bind transforms store quaternion as [X,Y,Z,W] (quat_order='XYZW').
    """
    f = [struct.unpack_from('>f', nk, off+j*4)[0] for j in range(7)]
    if quat_order == 'XYZW':
        # f[3]=X, f[4]=Y, f[5]=Z, f[6]=W → standard (W,X,Y,Z)
        rot = _quat_to_mat3(f[6], f[3], f[4], f[5])
    else:
        # f[3]=X, f[4]=Y, f[5]=W, f[6]=Z → standard (W,X,Y,Z) [default XYWZ]
        rot = _quat_to_mat3(f[5], f[3], f[4], f[6])
    return _mat4_from_rot_pos(rot, f[0], f[1], f[2])

def parse_nksh_skeleton(nk_data):
    """Parse NKSH skeleton/hierarchy chunk.
    Handles both unk=49 (full skeleton with rest transforms) and unk=1/17 (hierarchy only).
    Returns dict with bone data or None on failure."""
    nk = nk_data
    if len(nk) < 16: return None
    morph_count = struct.unpack_from('>I', nk, 0)[0]
    bone_count = struct.unpack_from('>I', nk, 4)[0]
    if bone_count < 1 or bone_count > 200: return None
    
    # Character name
    null = nk[8:].find(b'\x00')
    if null < 0: return None
    char_name = nk[8:8+null].decode('ascii', errors='replace')
    sub_start = ((8 + null + 1) + 3) & ~3
    
    # Parent table follows morph sub-entries (72 bytes each)
    parent_off = sub_start + morph_count * 72
    if parent_off + bone_count * 4 > len(nk): return None
    parents = [struct.unpack_from('>I', nk, parent_off + i*4)[0] for i in range(bone_count)]
    if not all(p < bone_count for p in parents): return None
    
    # Inverse bind transforms (position + quaternion, 28 bytes each)
    inv_off = parent_off + bone_count * 4
    if inv_off + bone_count * 28 > len(nk): return None
    
    # Bone names
    names_off = inv_off + bone_count * 28
    names = []
    off = names_off
    for i in range(bone_count):
        while off < len(nk) and nk[off] == 0: off += 1
        if off >= len(nk): return None
        end = off
        while end < len(nk) and nk[end] != 0: end += 1
        names.append(nk[off:end].decode('ascii', errors='replace'))
        off = end + 1
    if len(names) != bone_count: return None
    
    # Read inv_bind table (used as bind pose for animation)
    inv_binds = [_read_nksh_xform(nk, inv_off + i*28, quat_order='XYZW') for i in range(bone_count)]
    
    # Try to read rest-pose transforms (only present in unk=49 full skeletons)
    off = (off + 3) & ~3
    xform_off = off + bone_count * 4 + 8  # draw_order + gap
    if xform_off + bone_count * 28 <= len(nk):
        rest_xforms = [_read_nksh_xform(nk, xform_off + i*28, quat_order='XYWZ') for i in range(bone_count)]
    else:
        # No rest_xforms table — use inv_binds as rest pose (identity rotations)
        rest_xforms = list(inv_binds)
    
    return {
        'count': bone_count, 'names': names, 'parents': parents,
        'char_name': char_name, 'inv_binds': inv_binds, 'rest_xforms': rest_xforms
    }

def assemble_character(mesh, skeleton):
    """Convert character vertices to world-space OBJ coordinates.
    
    Character vertices in Asura SmoothSkin are stored pre-assembled in world space
    (the bone transforms are only used at runtime for animation offsets, not for
    rest-pose positioning). Export requires only GQR dequantization and coordinate
    system conversion (negate Y and Z for 180° X rotation from Asura to OBJ Y-up).
    
    Returns list of (x,y,z) world positions and (nx,ny,nz) normals."""
    GQR_SCALE = 1.0 / 1024.0  # GQR6 dequantization: int16 / 2^10
    
    positions = []
    normals = []
    for i in range(mesh['nVtx']):
        px, py, pz = [c * GQR_SCALE for c in mesh['positions'][i]]
        nx, ny, nz = mesh['normals'][i]
        # 180° rotation around X axis: negate Y and Z
        positions.append((px, -py, -pz))
        normals.append((nx, -ny, -nz))
    
    return positions, normals

# ============================================================
# Animation Evaluation Pipeline (Phase 4)
# ============================================================

def _quat_normalize(x, y, z, w):
    """Normalize a quaternion to unit length."""
    n = _math.sqrt(x*x + y*y + z*z + w*w)
    if n < 1e-10: return 0.0, 0.0, 0.0, 1.0
    return x/n, y/n, z/n, w/n

def _quat_slerp(q0, q1, t):
    """Spherical linear interpolation between two quaternions (w,x,y,z)."""
    w0,x0,y0,z0 = q0; w1,x1,y1,z1 = q1
    dot = w0*w1 + x0*x1 + y0*y1 + z0*z1
    if dot < 0:
        w1,x1,y1,z1 = -w1,-x1,-y1,-z1
        dot = -dot
    if dot > 0.9995:
        w = w0+(w1-w0)*t; x = x0+(x1-x0)*t; y = y0+(y1-y0)*t; z = z0+(z1-z0)*t
        n = _math.sqrt(w*w+x*x+y*y+z*z)
        return w/n, x/n, y/n, z/n
    theta = _math.acos(min(1.0, dot))
    sin_t = _math.sin(theta)
    if sin_t < 1e-10: return q0
    a = _math.sin((1-t)*theta)/sin_t
    b = _math.sin(t*theta)/sin_t
    return (a*w0+b*w1, a*x0+b*x1, a*y0+b*y1, a*z0+b*z1)

def _quat_mul(a, b):
    """Multiply quaternions (w,x,y,z)."""
    aw,ax,ay,az = a; bw,bx,by,bz = b
    return (aw*bw-ax*bx-ay*by-az*bz,
            aw*bx+ax*bw+ay*bz-az*by,
            aw*by-ax*bz+ay*bw+az*bx,
            aw*bz+ax*by-ay*bx+az*bw)

def _mat4_rigid_inverse(m):
    """Invert a rigid-body 4x4 matrix (rotation + translation). Fast path."""
    rt = [[m[j][i] for j in range(3)] for i in range(3)]
    tx = -(rt[0][0]*m[0][3] + rt[0][1]*m[1][3] + rt[0][2]*m[2][3])
    ty = -(rt[1][0]*m[0][3] + rt[1][1]*m[1][3] + rt[1][2]*m[2][3])
    tz = -(rt[2][0]*m[0][3] + rt[2][1]*m[1][3] + rt[2][2]*m[2][3])
    return [rt[0]+[tx], rt[1]+[ty], rt[2]+[tz], [0,0,0,1]]


def evaluate_animation(skeleton, animation, frame_t, skip_root=False):
    """Evaluate animation at time t (0.0 to 1.0) and return per-bone world matrices.
    
    Key format details discovered via reverse engineering:
    - NACH quaternions are stored as WXYZ (W first), not XYZW
    - Root bone (AITrajectory) always uses rest-pose transform
    - When root_motion flag is set, bone_table[0] is root motion data,
      so skeleton bone i maps to bone_table[i+1]
    - Inverse bind matrices are computed from rest-pose world matrices
      (the NKSH-stored inv_binds use a different convention)
    
    Args:
        skeleton: from parse_nksh_skeleton()
        animation: from parse_nach_keyframes()
        frame_t: normalized time 0.0–1.0
    
    Returns:
        (world_matrices, skin_matrices) — lists of 4x4 matrices per bone
    """
    n_bones = skeleton['count']
    parents = skeleton['parents']
    # The engine uses the NKSH "inv_bind" table as bone rest transforms.
    # These have IDENTITY rotations and bone-local positions (in Asura world space).
    bind_xforms = skeleton.get('inv_binds', skeleton['rest_xforms'])
    
    quats = animation['quats']
    positions = animation.get('positions', [])
    bone_table = animation['bone_table']
    pos_bone_table = animation.get('pos_bone_table', [])
    has_root_motion = animation.get('root_motion', False)
    bt_offset = 1 if has_root_motion else 0
    
    # Build rest-pose world matrices from bind transforms
    rest_world = [None] * n_bones
    for bi in range(n_bones):
        if bi == 0:
            rest_world[bi] = bind_xforms[bi]
        else:
            rest_world[bi] = _mat4_mul(rest_world[parents[bi]], bind_xforms[bi])
    
    # Build per-bone local transforms from animation data.
    # NACH quaternions are ABSOLUTE local rotations (bind poses have identity rotation).
    local_xforms = []
    for bi in range(n_bones):
        bti = bi + bt_offset
        
        # For preview: skip root bone animation (entity movement/rotation)
        if bi == 0 and skip_root:
            local_xforms.append(bind_xforms[bi])
            continue
        
        if bti >= len(bone_table) or bone_table[bti]['n_rot_keys'] <= 0:
            local_xforms.append(bind_xforms[bi])
            continue
        
        bt = bone_table[bti]
        n_keys = bt['n_rot_keys']
        first_idx = bt['first_rot_idx']
        
        # Get animation quaternion (WXYZ format, absolute local rotation)
        if n_keys == 1:
            qi = first_idx
            if qi < len(quats):
                qw, qx, qy, qz = quats[qi]
                n = _math.sqrt(qw*qw + qx*qx + qy*qy + qz*qz)
                if n > 1e-8: qw, qx, qy, qz = qw/n, qx/n, qy/n, qz/n
            else:
                qw, qx, qy, qz = 1.0, 0.0, 0.0, 0.0
        else:
            frame_f = frame_t * (n_keys - 1)
            ki0 = max(0, min(int(frame_f), n_keys - 1))
            ki1 = min(ki0 + 1, n_keys - 1)
            blend = frame_f - ki0
            
            idx0 = first_idx + ki0
            idx1 = first_idx + ki1
            if idx0 < len(quats) and idx1 < len(quats):
                w0, x0, y0, z0 = quats[idx0]
                w1, x1, y1, z1 = quats[idx1]
                n0 = _math.sqrt(w0*w0+x0*x0+y0*y0+z0*z0)
                n1 = _math.sqrt(w1*w1+x1*x1+y1*y1+z1*z1)
                if n0 > 1e-8: w0,x0,y0,z0 = w0/n0,x0/n0,y0/n0,z0/n0
                if n1 > 1e-8: w1,x1,y1,z1 = w1/n1,x1/n1,y1/n1,z1/n1
                qw,qx,qy,qz = _quat_slerp((w0,x0,y0,z0), (w1,x1,y1,z1), blend)
            else:
                qw, qx, qy, qz = 1.0, 0.0, 0.0, 0.0
        
        rot = _quat_to_mat3(qw, qx, qy, qz)
        
        # Compose with bind rotation: final_local_rot = bind_rot × NACH_delta
        # This is necessary because NACH quaternions are deltas from the bind pose.
        # For skeletons with identity bind rotations (e.g. homer), this is a no-op.
        bx = bind_xforms[bi]
        _bt = bx[0][0]+bx[1][1]+bx[2][2]
        if _bt > 0:
            _bs=0.5/_math.sqrt(_bt+1); bw=0.25/_bs; bqx=(bx[2][1]-bx[1][2])*_bs; bqy=(bx[0][2]-bx[2][0])*_bs; bqz=(bx[1][0]-bx[0][1])*_bs
        elif bx[0][0]>bx[1][1] and bx[0][0]>bx[2][2]:
            _bs=2*_math.sqrt(1+bx[0][0]-bx[1][1]-bx[2][2]); bw=(bx[2][1]-bx[1][2])/_bs; bqx=0.25*_bs; bqy=(bx[0][1]+bx[1][0])/_bs; bqz=(bx[0][2]+bx[2][0])/_bs
        elif bx[1][1]>bx[2][2]:
            _bs=2*_math.sqrt(1+bx[1][1]-bx[0][0]-bx[2][2]); bw=(bx[0][2]-bx[2][0])/_bs; bqx=(bx[0][1]+bx[1][0])/_bs; bqy=0.25*_bs; bqz=(bx[1][2]+bx[2][1])/_bs
        else:
            _bs=2*_math.sqrt(1+bx[2][2]-bx[0][0]-bx[1][1]); bw=(bx[1][0]-bx[0][1])/_bs; bqx=(bx[0][2]+bx[2][0])/_bs; bqy=(bx[1][2]+bx[2][1])/_bs; bqz=0.25*_bs
        fw, fx, fy, fz = _quat_mul((bw, bqx, bqy, bqz), (qw, qx, qy, qz))
        rot = _quat_to_mat3(fw, fx, fy, fz)
        
        # Position: bind position + animation delta
        px = bind_xforms[bi][0][3]
        py = bind_xforms[bi][1][3]
        pz = bind_xforms[bi][2][3]
        
        if bti < len(pos_bone_table) and pos_bone_table[bti]['n_pos_keys'] > 0 and len(positions) > 0:
            pbt = pos_bone_table[bti]
            n_pkeys = pbt['n_pos_keys']
            first_pidx = pbt['first_pos_idx']
            
            if n_pkeys == 1:
                # Constant position delta for this bone
                pidx = first_pidx
                if pidx < len(positions):
                    px += positions[pidx][0]
                    py += positions[pidx][1]
                    pz += positions[pidx][2]
            else:
                # Interpolate position keyframes (same timing as rotation)
                pframe_f = frame_t * (n_pkeys - 1)
                pki0 = max(0, min(int(pframe_f), n_pkeys - 1))
                pki1 = min(pki0 + 1, n_pkeys - 1)
                pblend = pframe_f - pki0
                
                pidx0 = first_pidx + pki0
                pidx1 = first_pidx + pki1
                if pidx0 < len(positions) and pidx1 < len(positions):
                    p0 = positions[pidx0]
                    p1 = positions[pidx1]
                    # Linear interpolation of delta, then add to rest
                    px += p0[0] * (1.0 - pblend) + p1[0] * pblend
                    py += p0[1] * (1.0 - pblend) + p1[1] * pblend
                    pz += p0[2] * (1.0 - pblend) + p1[2] * pblend
        
        local_xforms.append(_mat4_from_rot_pos(rot, px, py, pz))
    
    # Build world matrices by walking hierarchy
    world_mats = [None] * n_bones
    for bi in range(n_bones):
        if bi == 0:
            world_mats[bi] = local_xforms[bi]
        elif parents[bi] == bi:
            world_mats[bi] = local_xforms[bi]
        else:
            world_mats[bi] = _mat4_mul(world_mats[parents[bi]], local_xforms[bi])
    
    # Build skinning matrices per ELF CalculateBoneOffsets.
    # CalculateRestPose walks the hierarchy to build:
    #   - rest_world_pos[bone]: world-space bone positions (3 floats)
    #   - inv_rest_rot[bone]: INVERSE (transpose) of world-space bone rotations (3×3)
    # CalculateBoneOffsets then computes:
    #   skin.R = world_anim_rot × inv_rest_rot
    #   skin.t = world_anim_pos - skin.R × rest_world_pos
    
    # Extract rotation and position from rest-pose world matrices
    rest_world_rot = []  # 3×3 rotation per bone
    rest_world_pos = []  # 3-float position per bone
    for bi in range(n_bones):
        rw = rest_world[bi]
        rest_world_rot.append([[rw[r][c] for c in range(3)] for r in range(3)])
        rest_world_pos.append([rw[0][3], rw[1][3], rw[2][3]])
    
    skin_mats = []
    for bi in range(n_bones):
        # Inverse rest rotation = transpose of rest world rotation
        inv_rr = [[rest_world_rot[bi][c][r] for c in range(3)] for r in range(3)]
        
        # Animated world rotation and position
        aw = world_mats[bi]
        anim_rot = [[aw[r][c] for c in range(3)] for r in range(3)]
        anim_pos = [aw[0][3], aw[1][3], aw[2][3]]
        
        # skin.R = world_anim_rot × inv_rest_rot (3×3 multiply)
        sr = [[sum(anim_rot[r][k]*inv_rr[k][c] for k in range(3)) for c in range(3)] for r in range(3)]
        
        # skin.t = world_anim_pos - skin.R × rest_world_pos
        rwp = rest_world_pos[bi]
        rp = [sum(sr[r][c]*rwp[c] for c in range(3)) for r in range(3)]
        st = [anim_pos[r] - rp[r] for r in range(3)]
        
        skin_mats.append([
            sr[0] + [st[0]],
            sr[1] + [st[1]],
            sr[2] + [st[2]],
            [0, 0, 0, 1]
        ])
    
    return world_mats, skin_mats

def get_animation_bone_positions(skeleton, animation, frame_t):
    """Get world-space bone positions for visualization.
    Returns list of (x,y,z) per bone, and list of (parent_idx, child_idx) for drawing bones."""
    world_mats, _ = evaluate_animation(skeleton, animation, frame_t, skip_root=True)
    bone_pos = []
    for bi in range(skeleton['count']):
        m = world_mats[bi]
        bone_pos.append((m[0][3], -m[1][3], -m[2][3]))  # negate Y,Z for OBJ
    bone_links = []
    for bi in range(1, skeleton['count']):
        pi = skeleton['parents'][bi]
        if pi != bi:
            bone_links.append((pi, bi))
    return bone_pos, bone_links


def parse_bone_weights(mesh):
    """Parse SmoothSkin bone_info into per-vertex weight lists.
    
    bone_info consumed sequentially: for each vertex, read (bone_index, bone_weight)
    pairs until weights sum to 255.
    
    Returns list of [(bone_idx, weight_float), ...] per vertex.
    """
    bi = mesh['bone_info']
    vertex_weights = []
    bi_idx = 0
    for vi in range(mesh['nVtx']):
        vw = []
        total = 0
        while bi_idx < len(bi) and total < 255:
            bone_idx, weight = bi[bi_idx]
            bi_idx += 1
            if weight == 0: continue
            vw.append((bone_idx, weight / 255.0))
            total += weight
        vertex_weights.append(vw)
    return vertex_weights


def skin_character_mesh(mesh, skeleton, animation, frame_t, vertex_weights=None):
    """Apply animation skinning to a SmoothSkin character mesh.
    
    Returns list of (x, y, z) deformed positions (Y/Z negated for display).
    """
    GQR = 1.0 / 1024.0
    if vertex_weights is None:
        vertex_weights = parse_bone_weights(mesh)
    
    _, skin_mats = evaluate_animation(skeleton, animation, frame_t, skip_root=True)
    n_skin = len(skin_mats)
    
    result = []
    for vi in range(mesh['nVtx']):
        px, py, pz = [c * GQR for c in mesh['positions'][vi]]
        vw = vertex_weights[vi]
        sx, sy, sz = 0.0, 0.0, 0.0
        for bone_idx, w in vw:
            if bone_idx < n_skin:
                m = skin_mats[bone_idx]
                tx = m[0][0]*px + m[0][1]*py + m[0][2]*pz + m[0][3]
                ty = m[1][0]*px + m[1][1]*py + m[1][2]*pz + m[1][3]
                tz = m[2][0]*px + m[2][1]*py + m[2][2]*pz + m[2][3]
                sx += tx * w; sy += ty * w; sz += tz * w
        result.append((sx, -sy, -sz))
    return result


def find_skeleton_for_animation(skeletons, animation):
    """Find best skeleton for an animation by bone count + name similarity."""
    n_bones = animation['n_bones']
    anim_name = animation['name'].lower()
    matches = [sk for sk in skeletons if sk['count'] == n_bones]
    if not matches: return None
    if len(matches) == 1: return matches[0]
    best, best_score = matches[0], 0
    for sk in matches:
        sn = sk['char_name'].lower()
        score = 10 if sn in anim_name else 0
        for part in sn.replace('_', ' ').split():
            if len(part) > 2 and part in anim_name: score += 3
        if score > best_score: best_score = score; best = sk
    return best


def find_mesh_for_skeleton(files, skeleton):
    """Find the SmoothSkin mesh matching a skeleton by name.
    Tries cv0-2 parser first, then cv3 (final build with display lists)."""
    skel_name = skeleton['char_name'].lower()
    
    def try_parse(f):
        cv = f.get('chunk_ver', 2)
        m = _parse_smoothskin(f['data'], cv)
        if m: return m
        # Try cv3 parser for final build models
        m = _parse_smoothskin_cv3(f['data'])
        if m: return m
        return None
    
    for f in files:
        if not f['name'].startswith('Stripped') or f['name'] == 'StrippedEnv': continue
        mn = f['name'][8:].lower()
        if mn == skel_name:
            m = try_parse(f)
            if m: return m
    for f in files:
        if not f['name'].startswith('Stripped') or f['name'] == 'StrippedEnv': continue
        mn = f['name'][8:].lower()
        if skel_name in mn or mn in skel_name:
            m = try_parse(f)
            if m: return m
    return None


# ============================================================
# NLLD Dialogue Parser
# ============================================================

def parse_nlld_chunks(chunks):
    """Parse all NLLD chunks into dialogue entries."""
    e = _get_endian(chunks)
    utf16 = 'utf-16-le' if e == '<' else 'utf-16-be'
    entries = []
    for c in chunks:
        if c['id'] != 'NLLD': continue
        d = c['content']
        null = d.find(b'\x00')
        if null < 0: continue
        sid = d[:null].decode('ascii', errors='replace')
        after = (null + 4) & ~3
        if after + 24 > len(d): continue
        h1 = _u32(d, after, e)
        h2 = _u32(d, after+4, e)
        h3 = _u32(d, after+8, e)
        speaker_tag = _u32(d, after+12, e)
        dur = _f32(d, after+16, e)
        tlen = _u32(d, after+20, e)
        text = ''
        if tlen > 0 and after+24+tlen*2 <= len(d):
            text = d[after+24:after+24+tlen*2].decode(utf16, errors='replace').rstrip('\x00')
        entries.append({'sound_id':sid,'h1':h1,'h2':h2,'h3':h3,'speaker_tag':speaker_tag,'duration':dur,'text':text})
    return entries
    return entries


def repack_nlld_chunk(entry):
    """Repack a single NLLD dialogue entry to chunk content bytes.
    
    entry: dict with keys: sound_id, h1, h2, h3, duration, text
    Returns: bytes (chunk content, without the 16-byte chunk header)
    """
    out = bytearray()
    # Sound ID: null-terminated, 4-byte aligned
    sid_b = entry['sound_id'].encode('ascii', errors='replace') + b'\x00'
    while len(sid_b) % 4 != 0: sid_b += b'\x00'
    out += sid_b
    # 3 hashes + zero padding + duration
    out += struct.pack('>III', entry['h1'], entry['h2'], entry['h3'])
    out += struct.pack('>I', entry.get('speaker_tag', 0))
    out += struct.pack('>f', entry['duration'])
    # Text: UTF-16-BE with trailing null char
    text_with_null = entry['text'] + '\x00'
    text_encoded = text_with_null.encode('utf-16-be')
    out += struct.pack('>I', len(text_with_null))
    out += text_encoded
    return bytes(out)


def repack_txth_chunk(entries, hash_seed=0):
    """Repack a list of TXTH text entries into chunk content bytes.
    
    entries: list of dicts with keys: label, hash, text, optional _char_count
    hash_seed: the file-level hash identifier (uint32)
    Returns: bytes (chunk content)
    """
    # Build text section
    text_section = bytearray()
    total_text_bytes = 0
    for e in entries:
        # Use stored _char_count for round-trip fidelity, but recalculate if text changed
        text_with_null = e['text'] + '\x00'
        if '_char_count' in e and len(text_with_null) <= e['_char_count']:
            char_count = e['_char_count']
            # Pad text+null to match original char_count
            while len(text_with_null) < char_count:
                text_with_null += '\x00'
        else:
            char_count = len(text_with_null)
        text_encoded = text_with_null.encode('utf-16-be')
        text_section += struct.pack('>I', e.get('hash', _asura_hash_id(e.get('label', ''))))
        text_section += struct.pack('>I', char_count)
        text_section += text_encoded
        total_text_bytes += char_count * 2
    
    # Build label section
    label_section = bytearray()
    for e in entries:
        label_section += e.get('label', '').encode('latin-1', errors='replace') + b'\x00'
    
    # Assemble: header + text entries + label size + labels
    out = bytearray()
    out += struct.pack('>I', len(entries))
    out += struct.pack('>I', hash_seed)
    out += struct.pack('>I', total_text_bytes)
    out += text_section
    out += struct.pack('>I', len(label_section))
    out += label_section
    return bytes(out)

# ============================================================
# DSP ADPCM Decoder
# ============================================================

def _decode_dsp_to_wav(dsp_data, wav_path):
    """Decode Asura DSP file (DSP\\x01 + GC ADPCM) to WAV. Returns True on success."""
    import wave
    if len(dsp_data) < 100 or dsp_data[:4] != b'DSP\x01':
        return False
    hdr = 4
    num_samples = struct.unpack_from('>I', dsp_data, hdr)[0]
    sample_rate = struct.unpack_from('>I', dsp_data, hdr + 8)[0]
    if num_samples == 0 or sample_rate == 0 or sample_rate > 96000:
        return False
    coefs = [struct.unpack_from('>h', dsp_data, hdr + 28 + i*2)[0] for i in range(16)]
    yn1 = struct.unpack_from('>h', dsp_data, hdr + 64)[0]
    yn2 = struct.unpack_from('>h', dsp_data, hdr + 66)[0]
    audio = dsp_data[hdr + 96:]
    pcm = []
    for block in range(0, len(audio), 8):
        if len(pcm) >= num_samples: break
        header_byte = audio[block]
        scale = 1 << (header_byte & 0xF)
        ci = (header_byte >> 4) & 0x7
        c1, c2 = coefs[ci*2], coefs[ci*2+1]
        for i in range(1, 8):
            if block + i >= len(audio): break
            byte = audio[block + i]
            for nib_idx in range(2):
                if len(pcm) >= num_samples: break
                nib = ((byte >> 4) & 0xF) if nib_idx == 0 else (byte & 0xF)
                if nib >= 8: nib -= 16
                sample = ((nib * scale) << 11) + c1 * yn1 + c2 * yn2
                sample = max(-32768, min(32767, (sample + 1024) >> 11))
                yn2, yn1 = yn1, sample
                pcm.append(sample)
    pcm = pcm[:num_samples]
    os.makedirs(os.path.dirname(wav_path) or '.', exist_ok=True)
    with wave.open(wav_path, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f'<{len(pcm)}h', *pcm))
    return True

# ============================================================
# VAG Audio Decoder (PS2)
# ============================================================

# VAG ADPCM filter coefficients (standard PS2 table)
_VAG_COEFS = [
    (0.0, 0.0),
    (60.0/64.0, 0.0),
    (115.0/64.0, -52.0/64.0),
    (98.0/64.0, -55.0/64.0),
    (122.0/64.0, -60.0/64.0),
]

def _decode_vag_to_wav(vag_data, wav_path):
    """Decode a PS2 VAGp audio file to WAV. Returns True on success."""
    import wave
    if len(vag_data) < 48 or vag_data[:4] != b'VAGp':
        return False
    # VAG header is always big-endian regardless of platform
    version = struct.unpack_from('>I', vag_data, 4)[0]
    data_size = struct.unpack_from('>I', vag_data, 12)[0]
    sample_rate = struct.unpack_from('>I', vag_data, 16)[0]
    if sample_rate == 0 or sample_rate > 96000:
        return False

    # ADPCM data starts at offset 48 (or 0x30)
    audio_start = 48
    if version >= 3 and len(vag_data) > 0x1000:
        # Some VAG variants have a larger header
        pass

    s1, s2 = 0.0, 0.0
    pcm = []
    off = audio_start
    while off + 16 <= len(vag_data):
        predict_shift = vag_data[off]
        flags = vag_data[off + 1]
        shift = predict_shift & 0x0F
        predict = (predict_shift >> 4) & 0x0F

        if flags == 7:  # end marker
            break

        if predict >= len(_VAG_COEFS):
            predict = 0
        c1, c2 = _VAG_COEFS[predict]

        for i in range(2, 16):
            byte = vag_data[off + i]
            for nibble_idx in range(2):
                nib = (byte >> 4) & 0x0F if nibble_idx == 0 else byte & 0x0F
                if nib >= 8:
                    nib -= 16
                sample = float(nib * (1 << (12 - shift)))
                sample = sample + s1 * c1 + s2 * c2
                s2 = s1
                s1 = sample
                pcm.append(max(-32768, min(32767, int(sample + 0.5))))

        off += 16

    if not pcm:
        return False

    os.makedirs(os.path.dirname(wav_path) or '.', exist_ok=True)
    with wave.open(wav_path, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f'<{len(pcm)}h', *pcm))
    return True

# ============================================================
# Bink Audio Bank (Final .enBE dialogue files)
# ============================================================

def parse_bink_bank(fcsr_content):
    """Parse a 'streamed sounds' FCSR chunk into individual BIK audio clips.
    
    The bank contains an Asura index followed by sequential BIKi containers.
    Each BIKi is a complete Bink Video file (4×4 px, audio-only, 48kHz mono).
    
    Returns list of dicts: [{index, hash, bik_data, sample_rate, channels, frames}, ...]
    """
    d = fcsr_content
    file_size = struct.unpack_from('>I', d, 8)[0]
    bank_start = len(d) - file_size
    bank = d[bank_start:]
    count = struct.unpack_from('>I', bank, 4)[0]
    idx_end = 8 + count * 12
    
    # Read the Asura index (hash, offset, size per entry)
    index = []
    for i in range(count):
        off = 8 + i * 12
        h = struct.unpack_from('>I', bank, off)[0]
        o = struct.unpack_from('>I', bank, off + 4)[0]
        s = struct.unpack_from('>I', bank, off + 8)[0]
        index.append((h, o, s))
    
    # Scan for BIKi signatures (each is a complete BIK container)
    clips = []
    off = idx_end
    while off < len(bank) - 8 and len(clips) < count:
        if bank[off:off + 4] == b'BIKi':
            bik_size = struct.unpack_from('<I', bank, off + 4)[0] + 8
            # Parse audio info from BIK header
            tracks = struct.unpack_from('<I', bank, off + 0x28)[0]
            sr, ch = 48000, 1  # defaults
            if tracks > 0:
                sr_off = off + 0x2C + 4 * tracks
                if sr_off + 4 <= len(bank):
                    sr = struct.unpack_from('<H', bank, sr_off)[0]
                    aflags = struct.unpack_from('<H', bank, sr_off + 2)[0]
                    ch = 2 if (aflags & 0x2000) else 1
            frames = struct.unpack_from('<I', bank, off + 8)[0]
            bik_data = bytes(bank[off:off + bik_size])
            h = index[len(clips)][0] if len(clips) < len(index) else 0
            clips.append({
                'index': len(clips), 'hash': h, 'bik_data': bik_data,
                'sample_rate': sr, 'channels': ch, 'frames': frames
            })
            off += bik_size
        else:
            off += 1
    
    return clips


def _decode_bik_to_wav(bik_data):
    """Decode BIK audio data to WAV using ffmpeg subprocess.
    Returns (wav_bytes, sample_rate) or None if ffmpeg fails."""
    import subprocess, tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix='.bik', delete=False) as tmp_bik:
            tmp_bik.write(bik_data)
            tmp_bik_path = tmp_bik.name
        tmp_wav_path = tmp_bik_path.replace('.bik', '.wav')
        result = subprocess.run(
            ['ffmpeg', '-y', '-i', tmp_bik_path, '-vn', '-acodec', 'pcm_s16le', tmp_wav_path],
            capture_output=True, timeout=30
        )
        if result.returncode == 0 and os.path.exists(tmp_wav_path):
            with open(tmp_wav_path, 'rb') as f:
                wav_bytes = f.read()
            # Parse sample rate from WAV header
            sr = struct.unpack_from('<I', wav_bytes, 24)[0] if len(wav_bytes) > 28 else 48000
            return wav_bytes, sr
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass
    finally:
        for p in [tmp_bik_path, tmp_wav_path]:
            try: os.unlink(p)
            except: pass
    return None


# ============================================================
# Subcommands
# ============================================================

def cmd_info(args):
    for path in args.input:
        print(f"\n{'='*60}")
        print(f"File: {path} ({os.path.getsize(path):,} bytes)")
        raw = open(path, 'rb').read()
        magic = raw[:8]
        print(f"Magic: {magic}")
        try:
            data = read_asura(path)
        except Exception as e:
            print(f"Error: {e}"); continue
        if magic != b'Asura   ':
            print(f"Decompressed: {len(data):,} bytes")
        chunks = parse_chunks(data)
        endian = _get_endian(chunks)
        print(f"Platform: {_platform_name(endian)} ({'little-endian' if endian == '<' else 'big-endian'})")
        print(f"Chunks: {len(chunks)}")
        by_type = defaultdict(list)
        for c in chunks: by_type[c['id']].append(c)
        print(f"\nChunk types ({len(by_type)}):")
        for cid, clist in sorted(by_type.items(), key=lambda x: -len(x[1])):
            tsz = sum(len(c['content']) for c in clist)
            print(f"  {cid:6s}: {len(clist):4d} chunks, {tsz:>10,} bytes")

def cmd_extract(args):
    for path in args.input:
        print(f"\nExtracting: {path}")
        data = read_asura(path)
        chunks = parse_chunks(data)
        bn = os.path.splitext(os.path.basename(path))[0]
        out = args.output or bn + '_extract'
        os.makedirs(out, exist_ok=True)
        
        files = extract_fcsr_files(chunks)
        for f in files:
            # Normalize path separators
            fname = f['name'].replace('\\', '/').lstrip('/')
            fpath = os.path.join(out, fname)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, 'wb') as fout: fout.write(f['data'])
        
        # Also extract raw chunks
        cc = defaultdict(int)
        for c in chunks:
            idx = cc[c['id']]; cc[c['id']] += 1
            cdir = os.path.join(out, f"{c['id']}_chunk")
            os.makedirs(cdir, exist_ok=True)
            with open(os.path.join(cdir, f".{idx}.dat"), 'wb') as f:
                f.write(c['content'])
        
        print(f"  {len(files)} named files + {sum(cc.values())} chunks → {out}/")

def _parse_alphamaps(files):
    """Parse the 'alphamaps' file to build color→alpha texture mapping.
    Alpha atlases pack up to 3 sprites into BGR channels (index 0=B, 1=G, 2=R).
    Returns dict mapping color texture full path → (alpha_file_data, channel_index)."""
    alphamaps_data = None
    file_lookup = {}
    for f in files:
        if f['name'] == 'alphamaps':
            alphamaps_data = f['data']
        bn = f['name'].replace('\\', '/').split('/')[-1]
        file_lookup[bn] = f['data']

    if alphamaps_data is None or len(alphamaps_data) < 8:
        return {}

    am = alphamaps_data
    count = struct.unpack_from('>I', am, 0)[0]

    # Find all path strings by scanning for \graphics\ markers
    path_starts = []
    for i in range(4, len(am) - 10):
        if am[i:i+10] in (b'\\graphics\\', b'\\Graphics\\'):
            path_starts.append(i)

    # Separate color paths from alpha paths by content
    color_paths = []  # (start_offset, path_string)
    alpha_paths = []  # (start_offset, path_string)
    for ps in path_starts:
        null = am[ps:].find(b'\x00')
        if null < 0: continue
        path = am[ps:ps+null].decode('ascii', errors='replace')
        if 'GC_Alpha_Textures' in path:
            alpha_paths.append((ps, path, ps + null))
        else:
            color_paths.append((ps, path))

    if len(color_paths) != len(alpha_paths):
        return {}

    # For each alpha path, find the channel index:
    # It's the uint32 between the alpha null terminator and the next color path (or EOF)
    alpha_map = {}
    for i in range(len(color_paths)):
        # Normalize color path: strip leading \graphics\ prefix and normalize separators
        color_full = color_paths[i][1].replace('\\', '/').lstrip('/')
        # Remove leading 'graphics/' prefix for matching against FCSR file paths
        if color_full.lower().startswith('graphics/'):
            color_full = color_full[9:]  # strip 'graphics/'
        alpha_path = alpha_paths[i][1]
        alpha_null = alpha_paths[i][2]
        alpha_bn = alpha_path.replace('\\', '/').split('/')[-1]

        # Channel index: the uint32 immediately before the next color path (or EOF)
        # The gap between alpha null and next entry may contain extra bytes,
        # so read the last 4 bytes of the gap as the channel index
        ch_idx = 0
        next_boundary = path_starts[path_starts.index(alpha_paths[i][0]) + 1] if i < len(color_paths) - 1 else len(am)
        if next_boundary - 4 > alpha_null:
            ch_idx = struct.unpack_from('>I', am, next_boundary - 4)[0]
            if ch_idx > 2:
                ch_idx = 0  # fallback

        if alpha_bn in file_lookup:
            alpha_map[color_full] = (file_lookup[alpha_bn], ch_idx)

    return alpha_map

def cmd_textures(args):
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        print("ERROR: Pillow and numpy required. Install with: pip install Pillow numpy")
        sys.exit(1)
    
    for path in args.input:
        print(f"\nConverting textures: {path}")
        data = read_asura(path)
        chunks = parse_chunks(data)
        files = extract_fcsr_files(chunks)
        
        bn = os.path.splitext(os.path.basename(path))[0]
        out = args.output or bn + '_textures'
        use_pal = not args.no_palette
        
        # Build alpha map from alphamaps file
        alpha_map = _parse_alphamaps(files)
        if alpha_map and not args.quiet:
            print(f"  Alpha maps: {len(alpha_map)} textures have separate alpha channels")
        
        ok = skip = err = 0
        for f in files:
            ext = f['name'].rsplit('.', 1)[-1].lower() if '.' in f['name'] else ''
            if ext not in ('tga', 'bmp', 'tpl', 'tm2'): continue
            # Skip GC_Alpha_Texture files themselves (they're used as alpha sources)
            if 'GC_Alpha_Textures' in f['name']: skip += 1; continue
            
            # Detect format: TPL (Wii) or TIM2 (PS2)
            is_tpl = len(f['data']) >= 4 and struct.unpack_from('>I', f['data'], 0)[0] == TPL_MAGIC
            is_tim2 = len(f['data']) >= 4 and f['data'][:4] == b'TIM2'
            if not is_tpl and not is_tim2:
                skip += 1; continue
            
            # Build output path preserving directory structure
            rel = f['name'].replace('\\', '/').lstrip('/')
            png_path = os.path.join(out, os.path.splitext(rel)[0] + '.png')
            
            try:
                converted = False
                if is_tim2:
                    converted = convert_tim2_to_png(f['data'], png_path)
                    if converted:
                        ok += 1
                        if not args.quiet:
                            ti = parse_tim2(f['data'])
                            if ti:
                                bpp_names = {1:'16bit',2:'24bit',3:'32bit',4:'4bit-idx',5:'8bit-idx'}
                                fs = bpp_names.get(ti[0]['bpp_type'], '?')
                                print(f"  {rel:55s} {ti[0]['width']:4d}x{ti[0]['height']:<4d} TIM2 {fs:10s} OK")
                    else:
                        err += 1
                elif is_tpl:
                    if convert_tpl_to_png(f['data'], png_path, use_palette=use_pal):
                        # Check if this texture has a GC_Alpha companion
                        tex_path = rel.lower()
                        if tex_path.startswith('graphics/'):
                            tex_path = tex_path[9:]
                        alpha_entry = None
                        for akey, aval in alpha_map.items():
                            if akey.lower() == tex_path:
                                alpha_entry = aval
                                break
                        if alpha_entry:
                            alpha_data, channel_idx = alpha_entry
                            alpha_imgs = parse_tpl(alpha_data)
                            if alpha_imgs:
                                ai = alpha_imgs[0]
                                adec = _DECODERS.get(ai['fmt'])
                                if adec:
                                    apx, amode = adec(alpha_data[ai['doff']:], ai['w'], ai['h'])
                                    alpha_img = Image.frombytes(amode, (ai['w'], ai['h']), bytes(apx))
                                    alpha_rgb = alpha_img.convert('RGB')
                                    r_ch, g_ch, b_ch = alpha_rgb.split()
                                    channel_map = {0: b_ch, 1: g_ch, 2: r_ch}
                                    alpha_channel = channel_map.get(channel_idx, b_ch)
                                    color_img = Image.open(png_path)
                                    if alpha_channel.size != color_img.size:
                                        alpha_channel = alpha_channel.resize(color_img.size, Image.NEAREST)
                                    if color_img.mode not in ('RGBA',):
                                        color_img = color_img.convert('RGBA')
                                    r, g, b, existing_alpha = color_img.split()
                                    combined = Image.fromarray(
                                        (np.array(existing_alpha).astype(np.uint16) * np.array(alpha_channel).astype(np.uint16) // 255).astype(np.uint8))
                                    result = Image.merge('RGBA', (r, g, b, combined))
                                    result.save(png_path)
                    
                        ok += 1
                        if not args.quiet:
                            imgs = parse_tpl(f['data'])
                            fmt_names = {0:'I4',1:'I8',2:'IA4',3:'IA8',4:'RGB565',5:'RGB5A3',6:'RGBA8',14:'CMPR'}
                            fs = fmt_names.get(imgs[0]['fmt'], '?')
                            if len(imgs) > 1: fs += f"+{fmt_names.get(imgs[1]['fmt'],'?')}"
                            tag = " [pal]" if use_pal and imgs[0]['fmt']==1 else ""
                            alpha_tag = f" [+alpha ch{'BGR'[channel_idx]}]" if alpha_entry else ""
                            print(f"  {rel:55s} {imgs[0]['w']:4d}x{imgs[0]['h']:<4d} {fs:10s}{tag}{alpha_tag} OK")
                    else:
                        err += 1
            except Exception as e:
                err += 1
                if not args.quiet: print(f"  {rel}: ERROR {e}")
        
        print(f"\nDone: {ok} converted, {skip} skipped, {err} errors → {out}/")

def _find_model_texture(model_name, tex_lookup):
    """Find best texture for a model by name matching."""
    mn = model_name.lower()
    if mn in tex_lookup: return tex_lookup[mn]
    for key, tex in {'bart':'bart','homer':'homer','lardlad':'lardlad','mini_krusty':'minikrusty','krusty':'krusty'}.items():
        if key in mn:
            if 'gummi' in mn: return tex_lookup.get('homer_gummi', tex_lookup.get(tex))
            if 'helium' in mn: return tex_lookup.get('homer_helium', tex_lookup.get(tex))
            return tex_lookup.get(tex)
    for pfx in ['scd_','spr_']:
        if pfx+mn in tex_lookup: return tex_lookup[pfx+mn]
    for tn, tp in tex_lookup.items():
        if mn.replace('_','') in tn.replace('_','') or tn.replace('_','') in mn.replace('_',''):
            return tp
    return tex_lookup.get('simpsons_palette')

def cmd_models(args):
    for path in args.input:
        print(f"\nConverting models: {path}")
        data = read_asura(path)
        chunks = parse_chunks(data)
        files = extract_fcsr_files(chunks)
        
        bn = os.path.splitext(os.path.basename(path))[0]
        out = args.output or bn + '_models'
        
        # Build texture lookup from FCSR texture files
        tex_lookup = {}
        for f in files:
            ext = f['name'].rsplit('.', 1)[-1].lower() if '.' in f['name'] else ''
            if ext in ('tga', 'bmp', 'tpl'):
                clean = f['name'].replace(chr(92), '/').lstrip('/')
                bname = os.path.splitext(clean.split('/')[-1])[0].lower()
                if bname not in tex_lookup:
                    tex_lookup[bname] = clean
        
        # Collect NKSH character skeletons (unk==49 identifies character types)
        nksh_skeletons = {}
        for c in chunks:
            if c['id'] == 'NKSH':
                unk_field = struct.unpack_from('>I', data, 8 + sum(
                    struct.unpack_from('>I', data, p+4)[0] 
                    for p2 in range(0) # dummy
                ) + 12)[0] if False else 0
                # Re-read unk from the raw chunk position
                pass
        # Re-parse to get unk field from raw data
        pos = 8
        while pos + 16 <= len(data):
            cid = data[pos:pos+4]
            csz = struct.unpack_from('>I', data, pos+4)[0]
            if csz < 16 or pos + csz > len(data): break
            if cid == b'NKSH':
                unk = struct.unpack_from('>I', data, pos+12)[0]
                if unk == 49:
                    content = data[pos+16:pos+csz]
                    null = content[8:].find(b'\x00')
                    if null > 0:
                        skel_name = content[8:8+null].decode('ascii', errors='replace')
                        skel = parse_nksh_skeleton(content)
                        if skel:
                            nksh_skeletons[skel_name] = skel
            pos += csz
        
        if nksh_skeletons and not args.quiet:
            print(f"  Found {len(nksh_skeletons)} skeleton(s): {', '.join(nksh_skeletons.keys())}")
        
        ok = skip = err = 0
        for f in files:
            if not f['name'].startswith('Stripped'): continue
            if f['name'] == 'StrippedEnv': continue
            
            basename = f['name'][8:]
            obj_path = os.path.join(out, basename + '.obj')
            
            # Check if this is a character model with matching NKSH skeleton
            md = f['data']
            cv = f.get('chunk_ver', 2)
            is_char = len(md) > 8 and struct.unpack_from('>I', md, 4)[0] not in (6, 14)
            skel = nksh_skeletons.get(basename) if is_char else None
            
            if skel and cv <= 2:
                # Assemble character with skeleton
                mesh = _parse_smoothskin(md, cv)
                if mesh:
                    tris = _tristrip_to_tris(mesh['indices'], mesh['nVtx'])
                    if tris:
                        positions, normals = assemble_character(mesh, skel)
                        os.makedirs(os.path.dirname(obj_path) or '.', exist_ok=True)
                        with open(obj_path, 'w') as fobj:
                            fobj.write(f"# Asura Character: {basename} (assembled, {skel['count']} bones)\n")
                            fobj.write(f"# {mesh['nVtx']} verts, {len(tris)} tris\n\n")
                            for x,y,z in positions: fobj.write(f"v {x:.6f} {y:.6f} {z:.6f}\n")
                            fobj.write("\n")
                            for u,v in mesh['uvs']: fobj.write(f"vt {u:.6f} {1.0-v:.6f}\n")
                            fobj.write("\n")
                            for nx,ny,nz in normals: fobj.write(f"vn {nx:.6f} {ny:.6f} {nz:.6f}\n")
                            fobj.write(f"\no {basename}\ns 1\n")
                            for i0,i1,i2 in tris:
                                fobj.write(f"f {i0+1}/{i0+1}/{i0+1} {i1+1}/{i1+1}/{i1+1} {i2+1}/{i2+1}/{i2+1}\n")
                        ok += 1
                        info = f"char assembled: {mesh['nVtx']}v {len(tris)}t {skel['count']}bones"
                        if not args.quiet: print(f"  {basename:40s} {info}")
                        # Write MTL
                        tex_src = _find_model_texture(basename, tex_lookup)
                        mtl_path = os.path.join(out, basename + '.mtl')
                        with open(mtl_path, 'w') as mf:
                            mf.write(f"newmtl {basename}_mat\nKa 1 1 1\nKd 1 1 1\nKs 0 0 0\nd 1\n")
                            if tex_src:
                                png = os.path.splitext(tex_src)[0] + '.png'
                                if png.startswith('graphics/'): png = png[9:]
                                mf.write(f"map_Kd {png}\n")
                        with open(obj_path, 'r') as rf: obj_content = rf.read()
                        with open(obj_path, 'w') as wf:
                            wf.write(f"mtllib {basename}.mtl\n" + obj_content.replace(f"o {basename}", f"usemtl {basename}_mat\no {basename}"))
                        continue
            
            success, info = convert_model_to_obj(
                f['name'], f['data'], obj_path,
                chunk_ver=cv)
            if success:
                ok += 1
                tex_src = _find_model_texture(basename, tex_lookup)
                mtl_path = os.path.join(out, basename + '.mtl')
                with open(mtl_path, 'w') as mf:
                    mf.write(f"newmtl {basename}_mat\nKa 1 1 1\nKd 1 1 1\nKs 0 0 0\nd 1\n")
                    if tex_src:
                        png = os.path.splitext(tex_src)[0] + '.png'
                        if png.startswith('graphics/'): png = png[9:]
                        mf.write(f"map_Kd {png}\n")
                with open(obj_path, 'r') as rf: obj_content = rf.read()
                with open(obj_path, 'w') as wf:
                    wf.write(f"mtllib {basename}.mtl\n" + obj_content.replace(f"o {basename}", f"usemtl {basename}_mat\no {basename}"))
                if not args.quiet: print(f"  {basename:40s} {info}")
            elif 'mismatch' in info.lower() or 'failed' in info.lower():
                skip += 1
                if not args.quiet: print(f"  {basename:40s} SKIP: {info}")
            else:
                err += 1
                if not args.quiet: print(f"  {basename:40s} ERR: {info}")
        
        print(f"\nDone: {ok} converted, {skip} skipped, {err} errors → {out}/")

def cmd_dialogue(args):
    for path in args.input:
        print(f"\nDialogue: {path}")
        data = read_asura(path)
        chunks = parse_chunks(data)
        entries = parse_nlld_chunks(chunks)
        
        bn = os.path.splitext(os.path.basename(path))[0]
        out_dir = args.output or '.'
        os.makedirs(out_dir, exist_ok=True)
        
        csv_path = os.path.join(out_dir, bn + '_dialogue.csv')
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, ['sound_id','hash1','hash2','hash3','duration','subtitle'])
            w.writeheader()
            for e in entries:
                # Normalize Unicode smart quotes/dashes to ASCII equivalents
                text = e['text']
                text = text.replace('\u2018', "'").replace('\u2019', "'")  # smart single quotes
                text = text.replace('\u201C', '"').replace('\u201D', '"')  # smart double quotes
                text = text.replace('\u2013', '-').replace('\u2014', '--')  # en-dash, em-dash
                w.writerow({
                    'sound_id': e['sound_id'],
                    'hash1': f"{e['h1']:08x}", 'hash2': f"{e['h2']:08x}", 'hash3': f"{e['h3']:08x}",
                    'duration': f"{e['duration']:.2f}", 'subtitle': text
                })
        print(f"  {len(entries)} lines → {csv_path}")

def _decode_dsp_adpcm(dsp_data):
    """Decode a Nintendo DSP ADPCM file to PCM16 WAV bytes.
    Handles files with 'DSP\\x01' marker prefix. Returns (wav_bytes, sample_rate) or None."""
    import array, io, wave as _wave
    d = dsp_data
    off = 4 if d[:4] == b'DSP\x01' else 0
    if len(d) < off + 0x60: return None

    sample_count = struct.unpack_from('>I', d, off)[0]
    sample_rate = struct.unpack_from('>I', d, off+8)[0]
    if sample_rate < 1000 or sample_rate > 96000 or sample_count < 1: return None

    coeffs = [struct.unpack_from('>h', d, off+0x1C + i*2)[0] for i in range(16)]
    init_h1 = struct.unpack_from('>h', d, off+0x40)[0]
    init_h2 = struct.unpack_from('>h', d, off+0x42)[0]

    adpcm = d[off+0x60:]
    out = []
    h1, h2 = init_h1, init_h2
    done = 0
    bp = 0
    while done < sample_count and bp < len(adpcm):
        header = adpcm[bp]; bp += 1
        scale = 1 << (header & 0xF)
        ci = min((header >> 4) & 0xF, 7)
        c1, c2 = coeffs[ci*2], coeffs[ci*2+1]
        for j in range(7):
            if bp >= len(adpcm): break
            b = adpcm[bp]; bp += 1
            for sh in [4, 0]:
                if done >= sample_count: break
                nib = (b >> sh) & 0xF
                if nib >= 8: nib -= 16
                s = (nib * scale) + ((c1 * h1 + c2 * h2 + 1024) >> 11)
                s = max(-32768, min(32767, s))
                out.append(s)
                h2, h1 = h1, s
                done += 1

    buf = io.BytesIO()
    with _wave.open(buf, 'w') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sample_rate)
        w.writeframes(array.array('h', out).tobytes())
    return buf.getvalue(), sample_rate

def cmd_audio(args):
    for path in args.input:
        print(f"\nAudio: {path}")
        data = read_asura(path)
        chunks = parse_chunks(data)
        nlld = parse_nlld_chunks(chunks)
        
        bn = os.path.splitext(os.path.basename(path))[0]
        out = args.output or bn + '_audio'
        os.makedirs(out, exist_ok=True)
        
        # Check for Bink Audio bank (final format)
        fcsr_bank = None
        for c in chunks:
            if c['id'] == 'FCSR' and b'streamed sounds' in c['content'][:64]:
                fcsr_bank = c['content']; break
        
        if fcsr_bank:
            # Bink Audio bank extraction — parse complete BIKi containers
            clips = parse_bink_bank(fcsr_bank)
            
            rows = [['index','hash','sound_id','duration','subtitle','size','filename']]
            wav_ok = 0
            for clip in clips:
                i = clip['index']
                nl = nlld[i] if i < len(nlld) else None
                sid = nl['sound_id'] if nl else f'clip_{i:03d}'
                dur = nl['duration'] if nl else 0
                txt = nl['text'] if nl else ''
                # Normalize smart quotes/dashes
                txt = txt.replace('\u2018',"'").replace('\u2019',"'")
                txt = txt.replace('\u201C','"').replace('\u201D','"')
                txt = txt.replace('\u2013','-').replace('\u2014','--')
                
                safe_sid = sid.replace('/','_').replace(chr(92),'_')
                
                if hasattr(args, 'wav') and args.wav:
                    # Convert BIK → WAV via ffmpeg
                    result = _decode_bik_to_wav(clip['bik_data'])
                    if result:
                        fn = f"{i:03d}_{safe_sid}.wav"
                        with open(os.path.join(out, fn), 'wb') as f: f.write(result[0])
                        wav_ok += 1
                    else:
                        fn = f"{i:03d}_{safe_sid}.bik"
                        with open(os.path.join(out, fn), 'wb') as f: f.write(clip['bik_data'])
                else:
                    fn = f"{i:03d}_{safe_sid}.bik"
                    with open(os.path.join(out, fn), 'wb') as f: f.write(clip['bik_data'])
                
                rows.append([str(i),f'{clip["hash"]:08x}',sid,f'{dur:.2f}',txt,str(len(clip['bik_data'])),fn])
            
            with open(os.path.join(out,'index.csv'), 'w', newline='', encoding='utf-8-sig') as f:
                csv.writer(f).writerows(rows)
            wav_msg = f" ({wav_ok} converted to WAV)" if wav_ok else ""
            print(f"  {len(clips)} Bink Audio clips → {out}/{wav_msg}")
        else:
            # Individual audio files (DSP for Wii, VAG for PS2) — preserve folder structure
            files = extract_fcsr_files(chunks)
            rows = [['index','path','sound_id','duration','subtitle','size','filename']]
            count = 0
            wav_ok = 0
            for f in files:
                is_dsp = f['data'][:4] == b'DSP\x01'
                is_vag = f['data'][:4] == b'VAGp'
                if not f['name'].lower().endswith('.wav') and not is_dsp and not is_vag:
                    continue
                # Preserve the game's internal folder structure
                rel_path = f['name'].replace('\\', '/').lstrip('/')
                out_path = os.path.join(out, rel_path)
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                
                if hasattr(args, 'wav') and args.wav:
                    wav_out = os.path.splitext(out_path)[0] + '.wav'
                    if is_vag:
                        if _decode_vag_to_wav(f['data'], wav_out):
                            wav_ok += 1
                        else:
                            out_path = os.path.splitext(out_path)[0] + '.vag'
                            with open(out_path, 'wb') as fout: fout.write(f['data'])
                    elif is_dsp:
                        result = _decode_dsp_adpcm(f['data'])
                        if result:
                            with open(wav_out, 'wb') as fout: fout.write(result[0])
                            wav_ok += 1
                        else:
                            out_path = os.path.splitext(out_path)[0] + '.dsp'
                            with open(out_path, 'wb') as fout: fout.write(f['data'])
                    else:
                        with open(out_path, 'wb') as fout: fout.write(f['data'])
                else:
                    ext = '.vag' if is_vag else '.dsp' if is_dsp else os.path.splitext(out_path)[1]
                    out_path = os.path.splitext(out_path)[0] + ext
                    with open(out_path, 'wb') as fout: fout.write(f['data'])
                
                nl = nlld[count] if count < len(nlld) else None
                sid = nl['sound_id'] if nl else ''
                dur = nl['duration'] if nl else 0
                txt = nl['text'] if nl else ''
                rows.append([str(count), rel_path, sid, f'{dur:.2f}', txt, str(len(f['data'])),
                            os.path.splitext(rel_path)[0] + ('.wav' if hasattr(args,'wav') and args.wav else '.dsp')])
                count += 1
            
            with open(os.path.join(out,'index.csv'), 'w', newline='', encoding='utf-8-sig') as f:
                csv.writer(f).writerows(rows)
            wav_msg = f" ({wav_ok} converted to WAV)" if wav_ok else ""
            print(f"  {count} audio files → {out}/ (folder structure preserved){wav_msg}")

def parse_env_mesh_full(env_data):
    """Parse StrippedEnv into structured level geometry with UVs, colors, and materials.
    Returns dict with positions, texcoords, colors, strips (materialID + tri data)."""
    off = 0
    version = struct.unpack_from('>I', env_data, off)[0]; off += 4
    nMeshes = struct.unpack_from('>I', env_data, off)[0]; off += 4
    flags = struct.unpack_from('>I', env_data, off)[0]; off += 4
    off += 4; off += 4  # nPosTotal, nVtxTotal
    has_dl = (flags & 1) != 0
    has_normals = (flags & 2) != 0
    if version == 0:
        return _parse_env_v0(env_data)
    if has_normals: off += 4
    off += 4  # dlTotalSize or nIdxMap

    all_pos = []; all_uv = []; all_col = []
    strips = []
    gpo = 0; gvo = 0

    for m in range(nMeshes):
        nPos = struct.unpack_from('>I', env_data, off)[0]; off += 4
        nVtx = struct.unpack_from('>I', env_data, off)[0]; off += 4
        nStrips = struct.unpack_from('>I', env_data, off)[0]; off += 4
        off += 24  # bounding box

        for i in range(nPos):
            fx = struct.unpack_from('>f', env_data, off)[0]; off += 4
            fy = struct.unpack_from('>f', env_data, off)[0]; off += 4
            fz = struct.unpack_from('>f', env_data, off)[0]; off += 4
            all_pos.append((fx, -fy, -fz))

        for i in range(nVtx):
            u = struct.unpack_from('>h', env_data, off)[0] / 1024.0; off += 2
            v = 1.0 - struct.unpack_from('>h', env_data, off)[0] / 1024.0; off += 2
            all_uv.append((u, v))

        for i in range(nVtx):
            r = env_data[off]; g = env_data[off+1]; b = env_data[off+2]; off += 3
            all_col.append((r, g, b))

        for s in range(nStrips):
            if has_dl:
                off += 4  # stripFlags
                materialID = struct.unpack_from('>I', env_data, off)[0]; off += 4
                off += 4  # nExpandedIndices
                dlSize = struct.unpack_from('>I', env_data, off)[0]; off += 4
                dl = env_data[off:off+dlSize]
                tris = []
                doff = 0
                while doff < dlSize - 3:
                    cmd = dl[doff]
                    if cmd == 0x98:
                        cnt = struct.unpack_from('>H', dl, doff+1)[0]
                        if 3 <= cnt <= 65535:
                            vd = doff + 3; ve = vd + cnt * 6
                            if ve <= dlSize:
                                verts = []
                                ok = True
                                for vi in range(cnt):
                                    pi = struct.unpack_from('>H', dl, vd+vi*6)[0]
                                    ci = struct.unpack_from('>H', dl, vd+vi*6+2)[0]
                                    ti = struct.unpack_from('>H', dl, vd+vi*6+4)[0]
                                    if pi >= nPos: ok = False; break
                                    verts.append((pi + gpo, ti + gvo, ci + gvo))
                                if ok:
                                    for i in range(len(verts)-2):
                                        a, b, c = verts[i], verts[i+1], verts[i+2]
                                        if a[0]==b[0] or b[0]==c[0] or a[0]==c[0]: continue
                                        if i%2==0: tris.append((a,b,c))
                                        else: tris.append((a,c,b))
                                    doff = ve; continue
                        doff += 1
                    elif cmd == 0: doff += 1
                    else: doff += 1
                strips.append({'mat': materialID, 'tris': tris, 'mesh': m})
                off += dlSize
            else:
                off += 4
                nComp = struct.unpack_from('>I', env_data, off)[0]; off += 4
                off += 4
                off += nComp * 2
        gpo += nPos; gvo += nVtx

    return {
        'positions': all_pos, 'texcoords': all_uv, 'colors': all_col,
        'strips': strips, 'nMeshes': nMeshes
    }


def _parse_env_v0(env_data):
    """Parse StrippedEnv version 0 format (wm_hb1 final, wm_hb2 proto).
    
    ver=0 header: version(4) nMeshes(4) nNormalEntries(4) nPos(4) nVtx(4)
                  nCmpIdx(4) colorMode(4) normalTable[nNormalEntries × 12]
    Per-mesh: nPos(4) nVtx(4) nStrips(4) BB(24) positions(nPos×12)
              posMaps(nVtx×2) UVs(nVtx×4) colors(nVtx×colorMode) normals(nVtx×1)
    Per-strip: expandedFlags(4) nIndices(4) matID(4) indices(nIndices×2)
    """
    d = env_data
    nMeshes = struct.unpack_from('>I', d, 4)[0]
    nNE = struct.unpack_from('>I', d, 8)[0]
    colorMode = struct.unpack_from('>I', d, 24)[0]
    off = 28 + nNE * 12  # skip normal table

    all_pos = []; all_uv = []; all_col = []
    strips = []
    gpo = 0; gvo = 0

    for m in range(nMeshes):
        if off + 36 > len(d): break
        nPos = struct.unpack_from('>I', d, off)[0]
        nVtx = struct.unpack_from('>I', d, off+4)[0]
        nStrips = struct.unpack_from('>I', d, off+8)[0]
        off += 36  # header + BB

        # Positions (float32 XYZ)
        mesh_pos_start = len(all_pos)
        for i in range(nPos):
            fx = struct.unpack_from('>f', d, off)[0]; off += 4
            fy = struct.unpack_from('>f', d, off)[0]; off += 4
            fz = struct.unpack_from('>f', d, off)[0]; off += 4
            all_pos.append((fx, -fy, -fz))

        # PosMaps (uint16 per vertex → position index within this mesh)
        pos_maps = []
        for i in range(nVtx):
            pm = struct.unpack_from('>H', d, off)[0]; off += 2
            pos_maps.append(pm)

        # UVs (int16 pairs ÷ 1024)
        mesh_uv_start = len(all_uv)
        for i in range(nVtx):
            u = struct.unpack_from('>h', d, off)[0] / 1024.0; off += 2
            v = 1.0 - struct.unpack_from('>h', d, off)[0] / 1024.0; off += 2
            all_uv.append((u, v))

        # Colors
        for i in range(nVtx):
            if colorMode == 3:
                r, g, b = d[off], d[off+1], d[off+2]; off += 3
            else:
                # RGBA16: 2 bytes, decode as RGB565 or similar
                v16 = struct.unpack_from('>H', d, off)[0]; off += 2
                r = ((v16 >> 8) & 0xFF); g = ((v16 >> 4) & 0x0F) * 17; b = (v16 & 0x0F) * 17
            all_col.append((r, g, b))

        # Normal indices (1 byte per vertex, indexes into normal table)
        off += nVtx

        # Strips (compressed tristrip indices)
        for s in range(nStrips):
            if off + 12 > len(d): break
            expFlags = struct.unpack_from('>I', d, off)[0]; off += 4
            nIdx = struct.unpack_from('>I', d, off)[0]; off += 4
            matID = struct.unpack_from('>I', d, off)[0]; off += 4
            nExp = expFlags & 0xFFFFFF

            # Read compressed indices
            cmp_indices = []
            for i in range(nIdx):
                idx = struct.unpack_from('>H', d, off)[0]; off += 2
                cmp_indices.append(idx)

            # Decompress using algorithm from ELF Asura_GX_Env_Tristrip::Render:
            # - Lo-bit (no 0x8000): emit single vertex, advance 1
            # - Hi-bit (0x8000): read PAIR (current + next), emit range
            #   [start, end] inclusive. If next also has hi-bit, emit 3
            #   degenerate vertices (end, end+1, end+1) for strip restart.
            #   Advance 2.
            expanded = []
            ci = 0
            while ci < len(cmp_indices):
                val = cmp_indices[ci]
                if (val & 0x8000) == 0:
                    # Single vertex
                    expanded.append(val)
                    ci += 1
                else:
                    # Range pair
                    if ci + 1 >= len(cmp_indices):
                        expanded.append(val & 0x7FFF)
                        ci += 1
                        continue
                    start = val & 0x7FFF
                    end = cmp_indices[ci + 1] & 0x7FFF
                    for v in range(start, end + 1):
                        expanded.append(v)
                    # Strip restart: if next index also has hi-bit,
                    # emit 3 degenerate vertices for winding reset
                    if cmp_indices[ci + 1] & 0x8000:
                        expanded.append(end)
                        expanded.append(end + 1)
                        expanded.append(end + 1)
                    ci += 2

            # Convert tristrip to triangles using posMap
            tris = []
            for i in range(len(expanded) - 2):
                a, b, c = expanded[i], expanded[i+1], expanded[i+2]
                if a == b or b == c or a == c: continue
                if a >= nVtx or b >= nVtx or c >= nVtx: continue
                pa = pos_maps[a] + gpo
                pb = pos_maps[b] + gpo
                pc = pos_maps[c] + gpo
                ua = a + gvo; ub = b + gvo; uc = c + gvo
                if i % 2 == 0:
                    tris.append(((pa, ua, ua), (pb, ub, ub), (pc, uc, uc)))
                else:
                    tris.append(((pa, ua, ua), (pc, uc, uc), (pb, ub, ub)))

            strips.append({'mat': matID, 'tris': tris, 'mesh': m})

        gpo += nPos; gvo += nVtx

    return {
        'positions': all_pos, 'texcoords': all_uv, 'colors': all_col,
        'strips': strips, 'nMeshes': nMeshes
    }

def build_material_table(chunks):
    """Build ordered material table from TXET chunks. Returns list of texture paths."""
    materials = []
    for c in chunks:
        if c['id'] == 'TXET':
            d = c['content']
            n = struct.unpack_from('>I', d, 0)[0]
            if n > 0:
                null = d[4:].find(b'\x00')
                path = d[4:4+null].decode('ascii', errors='replace') if null > 0 else ''
                materials.append(path.replace('\\', '/').lstrip('/'))
            else:
                materials.append('')
    return materials

# --- Material system constants from Ghidra analysis ---
# LRTM material struct (36 bytes = 9 × uint32):
#   [0] tex_id_0        Primary texture index into TXET path list
#   [1] tex_id_1        Secondary texture (-1 = none, 0xFFFFFFFF)
#   [2] tex_id_2        Tertiary texture (-1 = none)
#   [3] tex_id_3        Layer 2 primary (-1 = none)
#   [4] tex_id_4        Layer 2 secondary (-1 = none)
#   [5] tex_id_5        Layer 2 tertiary (-1 = none)
#   [6] mat_flags       Material flags (transparency, animation, etc.)
#   [7] extra_flags     Low byte = SIMP_MATERIAL_TYPE (footstep sound type)
#                       High bits = rendering hints
#   [8] anim_data       Texture animation hash / extra data

# Material flags (field[6])
MAT_FLAG_TRANSPARENT    = 0x00000002  # Alpha blending enabled
MAT_FLAG_TEX_ANIM       = 0x00020000  # Has texture animation
MAT_FLAG_TEX_ANIM2      = 0x00040000  # Secondary texture animation

# SIMP_MATERIAL_TYPE values (field[7] & 0xFF) — from GetMaterialTypeFromMaterialID
MAT_TYPE_NAMES = {
    0: 'default', 1: 'concrete', 2: 'carpet', 3: 'wood',
    4: 'metal', 5: 'grass', 6: 'dirt', 7: 'water',
    8: 'tile', 9: 'gravel', 0xA: 'sand', 0xB: 'snow',
    0xC: 'mud', 0xD: 'glass',
}

def _parse_txet_paths(data):
    """Parse TXET chunk content into list of texture path strings."""
    if len(data) < 4: return []
    n = struct.unpack_from('>I', data, 0)[0]
    paths = []
    off = 4
    for _ in range(n):
        null = data[off:].find(b'\x00')
        if null < 0: break
        path = data[off:off+null].decode('ascii', errors='replace')
        paths.append(path.replace('\\', '/').lstrip('/'))
        off += null + 1
    return paths

def _parse_lrtm_materials(data):
    """Parse LRTM chunk content into list of material dicts.
    Each dict: {tex0, tex1..tex5, flags, extra_flags, anim_data, transparent, mat_type, mat_type_name}
    """
    if len(data) < 8: return []
    n = struct.unpack_from('>I', data, 0)[0]
    if n == 0: return []
    stride = (len(data) - 4) // n
    if stride < 36: return []
    materials = []
    for i in range(n):
        off = 4 + i * stride
        f = struct.unpack_from('>9I', data, off)
        tex_ids = [f[j] if f[j] != 0xFFFFFFFF else -1 for j in range(6)]
        flags = f[6]
        extra = f[7]
        anim = f[8]
        mat_type = extra & 0xFF
        materials.append({
            'tex0': tex_ids[0], 'tex1': tex_ids[1], 'tex2': tex_ids[2],
            'tex3': tex_ids[3], 'tex4': tex_ids[4], 'tex5': tex_ids[5],
            'flags': flags, 'extra_flags': extra, 'anim_data': anim,
            'transparent': bool(flags & MAT_FLAG_TRANSPARENT),
            'animated': bool(flags & (MAT_FLAG_TEX_ANIM | MAT_FLAG_TEX_ANIM2)),
            'mat_type': mat_type,
            'mat_type_name': MAT_TYPE_NAMES.get(mat_type, f'type_{mat_type}'),
        })
    return materials

def parse_env_materials(chunks):
    """Parse the env-specific TXET + LRTM into rich material data.
    
    CRITICAL: The env TXET contains many null-terminated strings with empty
    padding between them. The LRTM tex_ids index into the NON-EMPTY strings
    only (the n_entries header = count of non-empty strings). Empty strings
    are padding and must be skipped when building the index table.
    
    Returns dict with:
      'txet_paths': list of non-empty TXET path strings (indexed by LRTM tex_id)
      'materials': list of material dicts from LRTM (with decoded flags)
      'mat_table': list of resolved texture paths per materialID (for viewport)
    """
    # Step 1: Find the env LRTM (largest multi-entry) and its chunk index
    env_lrtm_data = None
    env_lrtm_n = 0
    env_lrtm_ci = -1
    for i, c in enumerate(chunks):
        if c['id'] != 'LRTM': continue
        d = c['content']
        if len(d) < 40: continue
        n = struct.unpack_from('>I', d, 0)[0]
        if n > env_lrtm_n:
            env_lrtm_n = n
            env_lrtm_data = d
            env_lrtm_ci = i
    
    if not env_lrtm_data or env_lrtm_n < 2 or env_lrtm_ci < 2:
        return {'txet_paths': [], 'materials': [], 'mat_table': []}
    
    # Step 2: Find the env TXET (TXET-LFXT-LRTM triplet before the LRTM)
    txet_data = None
    for offset in (2, 3, 1):
        ci = env_lrtm_ci - offset
        if 0 <= ci < len(chunks) and chunks[ci]['id'] == 'TXET':
            txet_data = chunks[ci]['content']
            break
    
    if not txet_data or len(txet_data) < 8:
        return {'txet_paths': [], 'materials': [], 'mat_table': []}
    
    # Step 3: Parse ALL strings from TXET, collect NON-EMPTY ones only
    # The TXET data has null-terminated strings with empty entries as padding.
    # LRTM tex_ids index into the non-empty-only list.
    tex_names = []
    off = 4
    while off < len(txet_data):
        null = txet_data[off:].find(b'\x00')
        if null < 0: break
        if null > 0:
            s = txet_data[off:off+null].decode('ascii', errors='replace')
            fn = s.replace('\\', '/').lstrip('/')
            if fn.lower().startswith('graphics/'):
                fn = fn[9:]
            tex_names.append(fn)
        off += null + 1
    
    # Step 4: Parse LRTM materials
    materials = _parse_lrtm_materials(env_lrtm_data) if env_lrtm_data else []
    
    # Step 5: Map materialID → LRTM[i].tex0 → tex_names[tex0]
    mat_table = []
    if materials:
        for m in materials:
            tex_idx = m['tex0']
            if 0 <= tex_idx < len(tex_names):
                path = tex_names[tex_idx]
                fname = path.split('/')[-1].lower()
                if fname in ('collision.tga', 'collision.bmp'):
                    mat_table.append('')  # collision — vertex colored
                else:
                    mat_table.append(path)
            else:
                mat_table.append('')
    else:
        mat_table = list(tex_names)
    
    return {'txet_paths': tex_names, 'materials': materials, 'mat_table': mat_table}

def build_env_material_table(chunks, files=None):
    """Build env mesh materialID→texture path mapping.
    Returns list of path strings indexed by materialID (backward compatible).
    """
    result = parse_env_materials(chunks)
    return result['mat_table']

# Complete entity type map from Ghidra analysis (exe+elf Project_Process + Process)
# Dialogue character name mappings from ELF: Simp_Dialogue_Name_Mappings.cpp
DIALOGUE_CHARACTER_NAMES = {
    0x05eda773: 'Homer',           0x002e06c1: 'Bart',
    0x0032b08b: 'Lisa',            0x062dd2bc: 'Marge',
    0x00017a26: 'Apu',             0xacc750af: 'Barney',
    0x22ccb979: 'Bumblebee Man',   0x20ef7b24: 'Captain',
    0x6785d553: 'Carl Carlson',    0xaf115b76: 'Cletus',
    0x8c228307: 'Comic Book Guy',  0x05b546f9: 'Dolph',
    0x05be3f81: 'Eddie',           0x0001907c: 'God',
}

# Video Game Cliché names and descriptions from Menu_En.asrBE (FE_Cliché_XX / FE_ClichéDesc_XX)
# 31 clichés in final game + 6 prototype-only extras (32-37)
CLICHE_NAMES = {
    1: 'Double Jump', 2: 'Switches and Levers', 3: 'The Doors',
    4: 'Pressure Pads', 5: 'Collectible Placement', 6: 'Time Trial',
    7: 'Giant Saw Blades', 8: 'Invisible Barrier', 9: 'Cracked Up',
    10: 'Red Ones Go Faster', 11: 'Trampolines', 12: 'AI Running into Walls',
    13: 'Water Warp', 14: 'Obvious Weakness', 15: 'Temporary Power Up',
    16: 'Chasm Death', 17: 'Lame Tutorials', 18: 'Key Card',
    19: 'Rift Portal', 20: 'Lava', 21: 'Ammo Box',
    22: 'Enemy Spawners', 23: 'The Road to Nowhere', 24: 'Wooden Crate',
    25: 'Explosive Barrel', 26: 'Flying Boat', 27: 'Elemental Enemies',
    28: 'Evil Genius', 29: 'Timing Puzzle', 30: 'Re-Used Enemies',
    31: 'Collecting Every Cliché',
    32: 'Flower Power', 33: 'Behold', 34: 'Boxes',
    35: 'Unfeasible Sword', 36: 'Hacker Crack', 37: 'Ports Ahoy',
}
CLICHE_DESCRIPTIONS = {
    1: "Oh, a double jump. That's real original.",
    2: "What would video games be without switches and levers? Original.",
    3: "Video games filled with doors that never open. Sounds delightful!",
    4: "It takes two losers to make these work.",
    5: "You just spent three hours to get one item. A life well-spent.",
    6: "If your game's boring -- just add a stopwatch!",
    7: "Giant saw blades, by any other name, would still be as clichéd.",
    8: "An invisible barrier. Sorry, your precious game doesn't go on forever.",
    9: "Yes, waste your youth looking for secret passages in rocky tunnels!",
    10: "What simpleton concocted the cliché that a lame palette change equals a more potent adversary?",
    11: "Trampolines. Where have those been before? Oh right, everywhere.",
    12: "In case you didn't already know your game sucked.",
    13: "Don't you know that you never learn to swim until the sequel?",
    14: "A must-have for masters of the obvious.",
    15: "Power up. By any other name, an extra life.",
    16: "Expect a plagiarism lawsuit from Wile E. Coyote.",
    17: "Oh, I'm too lazy to read the manual! I need help! Wah! Wah!",
    18: "News flash! A key card is really... a key.",
    19: "This cliché makes other clichés seem un-clichéd.",
    20: "Lava. As original as sand, snow, water and jungle. That is -- NOT ORIGINAL AT ALL!",
    21: "Isn't it convenient that you find ammo at the right place and time?",
    22: "Infinite bad guys from a small door. Way to rip off the clown car.",
    23: "If only this game was as mercifully short as this puny road!",
    24: "Ah, the crate. As seen in everything.",
    25: "The explosive barrel, frustrating AND hackneyed.",
    26: "A flying boat. What next, an underwater plane?",
    27: "Nice. Steal from Dungeons and Dragons -- for only the millionth time!",
    28: "Every single one a rip-off of Lex Luthor!",
    29: "The wonderful timing puzzle, a welcome addition to any classic...Not!",
    30: "The same bad guy, but now with a different-color shirt? What a gyp!",
    31: "Worst Cliché Ever!",
    32: "The player can find allies in the most unlikely places",
    33: "Not all objects are as impressive as they are made to look",
    34: "Some disguises are better than others",
    35: "Some weapons just don't make sense",
    36: "Not all hardware is easy to crack",
    37: "Sometimes all you can find are ports",
}

# Prototype build cliché names from PROTOMenu_En.asrBE (HUD_CLICHE_X_...)
# Indices differ from final build — many clichés were reshuffled during development.
# Proto has 37 entries (31 active + 2 reserved + 4 extras); final has 31 active + 6 cut.
PROTO_CLICHE_NAMES = {
    1: 'Double Jump', 2: 'Enemy Spawners', 3: 'Crate', 4: 'Pressure Pads',
    5: 'Switches and Levers', 6: "I'm Out of Here!", 7: 'Time Trial', 8: 'Ammo Box',
    9: 'Invisible Barrier', 10: 'Cracked Up', 11: 'Portal', 12: 'Red Ones Go Faster',
    13: 'Water Warp', 14: 'Magic Doors', 15: 'Obvious Weakness', 16: 'Collectible Placement',
    17: 'Chasm Death', 18: 'Hot Rocks', 19: 'The Road to Nowhere', 20: 'Trampolines',
    21: 'Explosive Barrel', 22: 'Armory', 23: 'Evil Genius out to Get You!',
    24: "I've Seen You Before", 25: 'The Doors', 26: 'Nowhere to Run', 27: 'Key Card',
    28: 'Tutorial Hell', 29: 'RESERVED FOR PLATFORM X', 30: 'RESERVED FOR PLATFORM X',
    31: 'Worst Cliché Ever!', 32: 'Flower Power', 33: 'Behold', 34: 'Boxes',
    35: 'Unfeasible Sword', 36: 'Hacker Crack', 37: 'Ports Ahoy',
}

ENTITY_TYPES = {
    # Asura engine entity types (from Ghidra: Asura_Chunk_Entity::Process switch)
    0x0001:'TimeTrigger', 0x0003:'CutsceneController', 0x0007:'PhysicsObject',
    0x0009:'DestructibleLight', 0x000B:'SplitterBlock', 0x000D:'CountedTrigger',
    0x000E:'SoundController', 0x0011:'AdvancedLight', 0x0014:'AdvVolumeTrigger',
    0x0015:'Lift', 0x0016:'DamageVolume', 0x0018:'MusicTrigger',
    0x001C:'FMVTrigger', 0x001F:'MetaMusicTrigger',
    0x0021:'PFX_Effect', 0x0022:'Template', 0x0023:'LookAtTrigger',
    0x0024:'ClockTrigger', 0x0026:'LogicTrigger',
    0x0028:'ClientVolumeTrigger', 0x0029:'StartPoint', 0x002A:'TimelineTrigger',
    0x002B:'EnvTextureAnimControl', 0x002F:'DebugMsgTrigger',
    0x0033:'CameraVolume', 0x0034:'ProxyTrigger',
    0x0035:'Node', 0x0036:'OrientedNode', 0x0037:'GamesceneNode',
    0x0038:'Coverpoint', 0x0039:'GuardZone', 0x003A:'GamesceneAttractor',
    0x003B:'Spline', 0x003C:'GamesceneSpline', 0x003E:'DialogueTrigger',
    0x003F:'LiftNode', 0x0040:'LiftSpline',
    0x0044:'Teleporter', 0x0045:'TeleportDestination',
    0x0048:'ForceField_Conveyor', 0x0049:'AttractorController',
    0x004A:'ConsoleVar', 0x004C:'StreamingBGSoundController',
    # Simpsons game entity types (from Ghidra: Project_Process switch)
    0x8001:'Actor', 0x8003:'NPC', 0x8004:'UsableObject',
    0x8005:'Pickup', 0x8006:'DestructibleObj', 0x8007:'StartPoint_Game',
    0x8008:'NavWaypoint', 0x800C:'Player_Server', 0x800D:'Player_Client',
    0x800E:'Shover', 0x800F:'NavWaypoint_Alt', 0x8010:'Updraft',
    0x8011:'Bunny', 0x8012:'Trampoline', 0x8013:'Interactive',
    0x8014:'HandOfBuddha', 0x8015:'Respawn', 0x8016:'DeathVolume',
    0x8017:'NPCSpawner', 0x8018:'HandOfBuddhaPort',
    0x8019:'HandOfBuddha_SnapTo', 0x801A:'InteractionTrigger',
    0x801B:'SeeSaw', 0x801C:'NavWaypoint_LB', 0x801D:'NavWaypoint_RB',
    0x801E:'OrientedNode_Game', 0x801F:'DamagerObject',
    0x8020:'StubbornApe', 0x8021:'LardLad', 0x8022:'Objective',
    0x8023:'Selmatty', 0x8024:'TransitionTrigger',
    0x8025:'Groening', 0x8026:'Shakespeare', 0x8027:'BartRing',
    0x8028:'LardLadFlap', 0x8029:'StateTrigger', 0x802A:'Parachute',
}

def _find_entity_position(d):
    """Find XYZ position + quaternion in ITNE data by scanning for the pattern:
    3 floats (position) followed by 4 floats (unit quaternion).
    Returns (pos_xyz, quat_xyzw) or None. Position is transformed to match
    env mesh coordinate space: (x, -y, -z).
    
    Uses two-pass scan: first at offset 40+ (standard entities/props),
    then at offset 24+ (NPCs/actors with position in header area).
    Near-origin results from pass 1 are deferred to allow pass 2 to find real positions."""
    import math
    def _scan(start, end_start=200):
        for off in range(start, min(len(d) - 27, end_start), 4):
            x = struct.unpack_from('>f', d, off)[0]
            y = struct.unpack_from('>f', d, off + 4)[0]
            z = struct.unpack_from('>f', d, off + 8)[0]
            if any(math.isnan(v) or math.isinf(v) or abs(v) > 10000 for v in (x, y, z)):
                continue
            # Skip common float constant patterns in entity headers
            if abs(x - 1.0) < 0.02 and abs(y - 0.5) < 0.02 and abs(z - 1.0) < 0.02:
                continue
            if abs(x) < 0.02 and abs(y - 1.0) < 0.02 and abs(z - 1.0) < 0.02:
                continue
            # Skip if all three are common small constants (scale/flag values)
            n_const = sum(1 for v in (x,y,z) if abs(v) < 0.02 or abs(abs(v)-0.5) < 0.02 or abs(abs(v)-1.0) < 0.02)
            if n_const == 3:
                continue
            # Check if followed by a unit quaternion
            if off + 28 > len(d):
                continue
            qx = struct.unpack_from('>f', d, off + 12)[0]
            qy = struct.unpack_from('>f', d, off + 16)[0]
            qz = struct.unpack_from('>f', d, off + 20)[0]
            qw = struct.unpack_from('>f', d, off + 24)[0]
            if any(math.isnan(v) or math.isinf(v) for v in (qx, qy, qz, qw)):
                continue
            qmag = qx * qx + qy * qy + qz * qz + qw * qw
            if 0.9 < qmag < 1.1:
                return (x, -y, -z), (qx, qy, qz, qw)
        return None
    # Pass 1: standard offset range (40+) for props/standard entities
    result = _scan(40)
    if result:
        # Accept if position has meaningful magnitude (not near-origin)
        pos = result[0]
        if any(abs(v) > 1.0 for v in pos):
            return result
        # Near-origin: save as fallback but try pass 2 first
        fallback = result
    else:
        fallback = None
    # Pass 2: lower offset range (24-39) for NPCs/actors
    result2 = _scan(24, 40)
    if result2 and any(abs(v) > 1.0 for v in result2[0]):
        return result2
    # Return whichever we found (pass 1 fallback or pass 2 result)
    return result2 or fallback

def parse_entity_placements(chunks):
    """Parse ITNE entities with positions, bounding boxes, debug text, and metadata.
    
    Returns list of entity dicts with keys:
      id, type, type_name, size, flags,
      pos (x,y,z), quat (x,y,z,w), radius  — for positioned entities
      bb_min, bb_max  — for volume entities (trigger zones, camera volumes, etc.)
      debug_text  — for DebugMsgTrigger (type 0x002F)
    """
    # Entity types that reliably have position at offset 72 (validated against all 56 levels)
    POSITIONED_TYPES = {
        0x0007, 0x0009, 0x0011, 0x0014, 0x0015, 0x0021, 0x0023,
        0x0033, 0x0037, 0x003A, 0x003C, 0x0040, 0x0048,
        0x8003, 0x8005, 0x8006, 0x8007, 0x8011, 0x8012, 0x8013,
        0x8014, 0x8016, 0x8017, 0x8018, 0x8019, 0x801E, 0x801F,
        0x8020, 0x8021, 0x8027,
    }
    entities = []
    for c in chunks:
        if c['id'] != 'ITNE': continue
        d = c['content']
        if len(d) < 8: continue
        eid = struct.unpack_from('>I', d, 0)[0]
        etype = struct.unpack_from('>H', d, 4)[0]
        eflags = struct.unpack_from('>H', d, 6)[0]
        ent = {
            'id': eid, 'type': etype,
            'type_name': ENTITY_TYPES.get(etype, f'0x{etype:04x}'),
            'size': len(d), 'flags': eflags,
        }
        # Type-specific position extraction (runs BEFORE generic scanner)
        # Position offset lookup: verified against multiple levels.
        # Most game logic entities store position at offset 32 (after GUID+type+flags+hash).
        # Standard game objects (props, pickups) use offset 72.
        # Volume entities use bounding box center instead of a direct position.
        POS_AT_32 = {0x0037, 0x003A, 0x003C, 0x003F, 0x0040,  # GamesceneNode, Attractor, Spline, LiftNode, LiftSpline
                     0x801D, 0x801E}  # NavWaypoint_RB, OrientedNode_Game
        POS_AT_36 = {0x8011}  # Bunny: position at 36 (offset 32 is a hash/flag)
        POS_AT_72 = {0x8005, 0x8006, 0x8012, 0x8013}  # Pickup, Destructible, Trampoline, Interactive
        POS_AT_64 = {0x0007}  # PhysicsObject: position at 64
        POS_AT_28 = {0x800E}  # Shover: rotation at 20, position at 28
        POS_AT_48 = {0x0021}  # PFX_Effect: position at 48 (local offset, often small)
        BB_CENTER = {0x0014, 0x0033}  # AdvVolumeTrigger, CameraVolume: use BB center
        
        def _read_pos(data, off):
            if off + 12 > len(data): return None
            x = struct.unpack_from('>f', data, off)[0]
            y = struct.unpack_from('>f', data, off+4)[0]
            z = struct.unpack_from('>f', data, off+8)[0]
            if all(-10000 < v < 10000 for v in (x, y, z)):
                return (x, -y, -z)
            return None
        
        # NPCSpawner: 3×3 rotation matrix at +24, position at +60
        if etype == 0x8017:
            p = _read_pos(d, 60)
            if p: ent['pos'] = p
        # StartPoint_Game: position at +24
        elif etype == 0x8007:
            p = _read_pos(d, 24)
            if p: ent['pos'] = p
        # DeathVolume: position at +32, half-extents at +48
        elif etype == 0x8016:
            p = _read_pos(d, 32)
            if p: ent['pos'] = p
        # DamageVolume: position at +32
        elif etype == 0x0016:
            p = _read_pos(d, 32)
            if p: ent['pos'] = p
        # Respawn: position at +32 (small values near spawn area)
        elif etype == 0x8015:
            p = _read_pos(d, 32)
            if p: ent['pos'] = p
        # Entities with position at offset 36
        elif etype in POS_AT_36:
            p = _read_pos(d, 36)
            if p: ent['pos'] = p
        # Entities with position at offset 32
        elif etype in POS_AT_32:
            p = _read_pos(d, 32)
            if p: ent['pos'] = p
        # Entities with position at offset 28 (after rotation data)
        elif etype in POS_AT_28:
            p = _read_pos(d, 28)
            if p: ent['pos'] = p
        # Entities with position at offset 64
        elif etype in POS_AT_64:
            p = _read_pos(d, 64)
            if p: ent['pos'] = p
        # Entities with position at offset 48
        elif etype in POS_AT_48:
            p = _read_pos(d, 48)
            if p: ent['pos'] = p
        # Volume entities: use bounding box center
        elif etype in BB_CENTER and len(d) >= 84:
            v = struct.unpack_from('>6f', d, 60)
            if all(abs(f) < 10000 for f in v):
                cx = (v[0] + v[1]) / 2
                cy = -(v[2] + v[3]) / 2
                cz = -(v[4] + v[5]) / 2
                ent['pos'] = (cx, cy, cz)
        # Standard game objects with position at offset 72
        elif etype in POS_AT_72:
            p = _read_pos(d, 72)
            if p: ent['pos'] = p
        
        # Generic scanner fallback for remaining/unknown entity types
        if 'pos' not in ent:
            result = _find_entity_position(d)
            if result:
                ent['pos'] = result[0]
                ent['quat'] = result[1]
        
        # Final fallback: try fixed offset 72 then 32
        if 'pos' not in ent:
            for try_off in [72, 32]:
                p = _read_pos(d, try_off)
                if p and any(abs(v) > 0.5 for v in p):
                    ent['pos'] = p; break
        
        # Bounding boxes for volume entities (for 3D visualization)
        # AdvVolumeTrigger (0x0014): BB at offset 60, paired format (minX,maxX,minY,maxY,minZ,maxZ)
        if etype == 0x0014 and len(d) >= 84:
            v = struct.unpack_from('>6f', d, 60)
            if all(abs(f) < 10000 for f in v):
                ent['bb_min'] = (min(v[0],v[1]), -max(v[2],v[3]), -max(v[4],v[5]))
                ent['bb_max'] = (max(v[0],v[1]), -min(v[2],v[3]), -min(v[4],v[5]))
        
        # CameraVolume (0x0033): BB at offset 32, paired format
        elif etype == 0x0033 and len(d) >= 56:
            v = struct.unpack_from('>6f', d, 32)
            if all(abs(f) < 10000 for f in v):
                ent['bb_min'] = (min(v[0],v[1]), -max(v[2],v[3]), -max(v[4],v[5]))
                ent['bb_max'] = (max(v[0],v[1]), -min(v[2],v[3]), -min(v[4],v[5]))
        
        # DeathVolume (0x8016): position at offset 32, half-extents at offset 48
        elif etype == 0x8016 and len(d) >= 72:
            px2, py2, pz2 = struct.unpack_from('>fff', d, 32)
            hx, hy, hz = struct.unpack_from('>fff', d, 48)
            if all(abs(v) < 10000 for v in (px2, py2, pz2)):
                ent['pos'] = (px2, -py2, -pz2)
                r = max(abs(hx), abs(hy), abs(hz), 2.0)
                ent['bb_min'] = (px2 - r, -(py2 + r), -(pz2 + r))
                ent['bb_max'] = (px2 + r, -(py2 - r), -(pz2 - r))
        
        # DebugMsgTrigger: extract debug text at offset 48
        if etype == 0x002F and len(d) > 50:
            null = d[48:].find(b'\x00')
            if null > 0:
                try:
                    txt = d[48:48+null].decode('ascii')
                    if all(32 <= ord(ch) < 127 for ch in txt):
                        ent['debug_text'] = txt
                except:
                    pass
        entities.append(ent)
    return entities

def extract_debug_text(chunks):
    """Extract all developer debug text strings from DebugMsgTrigger entities.
    
    Returns list of dicts: {id, text, pos}
    """
    results = []
    for ent in parse_entity_placements(chunks):
        if ent.get('debug_text'):
            results.append({
                'id': ent['id'],
                'text': ent['debug_text'],
                'pos': ent.get('pos'),
            })
    return results

def parse_navmesh(chunks):
    """Parse 1VAN navigation mesh. Returns dict with vertices, connections, coverpoints.
    
    Based on Ghidra analysis of Asura_Chunk_Navigation::Process.
    On-disk vertex stride = 21 bytes (validated across all levels).
    """
    for c in chunks:
        if c['id'] != '1VAN': continue
        d = c['content']
        ver = c['ver']
        if len(d) < 8: return None
        nv = struct.unpack_from('>I', d, 0)[0]
        n_coverpoints = struct.unpack_from('>I', d, 4)[0]
        if nv == 0: return {'vertices': [], 'connections': [], 'coverpoints': [], 'version': ver}
        
        stride = 21  # 12(pos) + 4(radius) + 1(nConns) + 1(zone) + 2(flags) + 1(pad)
        vertices = []
        off = 8
        for vi in range(nv):
            vo = off + vi * stride
            if vo + stride > len(d): break
            px, py, pz = struct.unpack_from('>fff', d, vo)
            rad = struct.unpack_from('>f', d, vo + 12)[0]
            nc = d[vo + 16]
            zone = d[vo + 17]
            flags = struct.unpack_from('>H', d, vo + 18)[0]
            vertices.append({
                'pos': (px, -py, -pz),  # negate Y,Z for OBJ convention
                'radius': rad, 'n_connections': nc,
                'zone': zone, 'flags': flags,
            })
        
        # Connections
        conn_off = off + nv * stride
        connections = []
        if conn_off + 4 <= len(d):
            n_conn = struct.unpack_from('>I', d, conn_off)[0]
            co = conn_off + 4
            for ci in range(n_conn):
                if co + 8 > len(d): break
                target = struct.unpack_from('>H', d, co)[0]
                cflags = struct.unpack_from('>H', d, co + 2)[0]
                cost = struct.unpack_from('>f', d, co + 4)[0]
                connections.append({
                    'target': target, 'flags': cflags, 'cost': cost,
                })
                co += 8
        
        return {
            'vertices': vertices, 'connections': connections,
            'coverpoints': [], 'version': ver,
            'n_coverpoints_header': n_coverpoints,
        }
    return None

def parse_nach_animations(chunks):
    """Parse all NACH animation chunks. Returns list of animation dicts.
    
    Based on Ghidra analysis of Asura_Chunk_Hierarchy_CompressedAnim::Process.
    NACH extended header is 0x2C (44 bytes): 28 bytes of fields after standard 16.
    """
    animations = []
    for c in chunks:
        if c['id'] != 'NACH': continue
        d = c['content']
        flags = c['unk']
        ver = c['ver']
        if len(d) < 28: continue
        
        n_bones = struct.unpack_from('>I', d, 0)[0]
        field_14 = struct.unpack_from('>I', d, 4)[0]
        field_18 = struct.unpack_from('>I', d, 8)[0]
        field_1c = struct.unpack_from('>I', d, 12)[0]
        n_unique_quats = struct.unpack_from('>I', d, 16)[0]
        n_unique_pos = struct.unpack_from('>I', d, 20)[0]
        n_sound_events = struct.unpack_from('>I', d, 24)[0]
        
        # Name string at offset 28
        null = d[28:].find(b'\x00')
        name = d[28:28+null].decode('ascii', errors='replace') if null > 0 else '?'
        name_end = ((28 + null + 1) + 3) & ~3
        
        # Bone table
        has_root_motion = bool(flags & 0x10)
        n_bone_entries = (n_bones + 1) if has_root_motion else n_bones
        bone_table = []
        off = name_end
        if off + n_bone_entries * 4 <= len(d):
            for bi in range(n_bone_entries):
                nrk = struct.unpack_from('>h', d, off + bi * 4)[0]
                fri = struct.unpack_from('>h', d, off + bi * 4 + 2)[0]
                bone_table.append({'n_rot_keys': nrk, 'first_rot_idx': fri})
        
        anim = {
            'name': name, 'version': ver, 'flags': flags,
            'n_bones': n_bones, 'n_unique_quats': n_unique_quats,
            'n_unique_pos': n_unique_pos, 'n_sound_events': n_sound_events,
            'loop': bool(flags & 0x02), 'root_motion': has_root_motion,
            'pre_packed': bool(flags & 0x20),
            'bone_table': bone_table,
            'total_rot_keys': sum(max(0, b['n_rot_keys']) for b in bone_table),
            'animated_bones': sum(1 for b in bone_table if b['n_rot_keys'] > 0),
        }
        animations.append(anim)
    return animations

    """Parse DOME chunk to get level section names and data.
    
    DOME sections map 1:1 to StrippedEnv meshes (validated across all levels).
    Format: nSections + level_name + per-section: name(var) + data(96 bytes fixed).
    
    Returns list of {name, center, ...} indexed by mesh index, or empty list.
    """
    for c in chunks:
        if c['id'] != 'DOME': continue
        d = c['content']
        if len(d) < 8: return []
        n = struct.unpack_from('>I', d, 0)[0]
        
        # Level name
        null = d[4:].find(b'\x00')
        if null < 0: return []
        off = ((4 + null + 1) + 3) & ~3
        
        sections = []
        for si in range(n):
            if off >= len(d): break
            null = d[off:off+128].find(b'\x00')
            if null < 0: break
            try:
                sname = d[off:off+null].decode('ascii')
            except:
                sname = '?'
            off = ((off + null + 1) + 3) & ~3
            
            if off + 96 <= len(d):
                # First 3 floats appear to be a center/position
                cx, cy, cz = struct.unpack_from('>fff', d, off)
                sections.append({'name': sname, 'center': (cx, -cy, -cz)})
                off += 96
            else:
                sections.append({'name': sname})
                break
        
        return sections
    return []

def parse_uv_animations(chunks):
    """Parse NAXT chunks for UV/texture scroll animations.
    
    Returns list of dicts with name, n_layers, and per-layer scroll speeds.
    These control texture coordinate animation on env mesh materials.
    """
    results = []
    for c in chunks:
        if c['id'] != 'NAXT': continue
        d = c['content']
        if len(d) < 8: continue
        null = d[:64].find(b'\x00')
        if null < 0: continue
        name = d[:null].decode('ascii', errors='replace')
        off = ((null + 1) + 3) & ~3
        if off + 4 > len(d): continue
        n_layers = struct.unpack_from('>I', d, off)[0]
        off += 4
        layers = []
        for li in range(n_layers):
            if off + 24 > len(d): break
            # Per layer: float32 u_speed, v_speed + additional params
            vals = [struct.unpack_from('>f', d, off + i*4)[0] for i in range(min(6, (len(d)-off)//4))]
            u_speed = vals[0] if len(vals) > 0 else 0.0
            v_speed = vals[1] if len(vals) > 1 else 0.0
            layers.append({'u_speed': u_speed, 'v_speed': v_speed, 'params': vals})
            off += 24  # approximate layer stride
        results.append({
            'name': name, 'n_layers': n_layers, 'layers': layers,
            'version': c['ver']
        })
    return results

def parse_dome_sections(chunks):
    """Parse DOME chunk to get level section names and data.
    
    DOME sections map 1:1 to StrippedEnv meshes (validated across all levels).
    Format: nSections + level_name + per-section: name(var) + data(96 bytes fixed).
    
    Returns list of {name, center, ...} indexed by mesh index, or empty list.
    """
    for c in chunks:
        if c['id'] != 'DOME': continue
        d = c['content']
        if len(d) < 8: return []
        n = struct.unpack_from('>I', d, 0)[0]
        
        # Level name
        null = d[4:].find(b'\x00')
        if null < 0: return []
        off = ((4 + null + 1) + 3) & ~3
        
        sections = []
        for si in range(n):
            if off >= len(d): break
            null = d[off:off+128].find(b'\x00')
            if null < 0: break
            try:
                sname = d[off:off+null].decode('ascii')
            except:
                sname = '?'
            off = ((off + null + 1) + 3) & ~3
            
            if off + 96 <= len(d):
                cx, cy, cz = struct.unpack_from('>fff', d, off)
                sections.append({'name': sname, 'center': (cx, -cy, -cz)})
                off += 96
            else:
                sections.append({'name': sname})
                break
        
        return sections
    return []

def parse_level_environment(chunks):
    """Parse level environment settings (fog, skybox, weather, lights, start position).
    Returns dict with decoded values for 3D viewport rendering.
    """
    env = {}
    for c in chunks:
        cid = c['id']
        d = c['content']
        
        # GOF: Fog settings
        if cid == ' GOF' and len(d) >= 28:
            env['fog'] = {
                'r': struct.unpack_from('>f', d, 0)[0],
                'g': struct.unpack_from('>f', d, 4)[0],
                'b': struct.unpack_from('>f', d, 8)[0],
                'near': struct.unpack_from('>f', d, 12)[0],
                'far': struct.unpack_from('>f', d, 16)[0],
                'range': struct.unpack_from('>f', d, 20)[0],
                'density': struct.unpack_from('>f', d, 24)[0],
            }
        
        # BYKS: Skybox
        elif cid == 'BYKS' and len(d) >= 12:
            sky = {
                'r': struct.unpack_from('>f', d, 0)[0],
                'g': struct.unpack_from('>f', d, 4)[0],
                'b': struct.unpack_from('>f', d, 8)[0],
                'faces': [],
            }
            # Skip color (12 bytes) + extra fields, scan for null-terminated paths
            off = 12
            # Skip any zero-valued floats after color
            while off + 4 <= len(d):
                v = struct.unpack_from('>I', d, off)[0]
                if v != 0: break
                off += 4
            # Now read texture paths
            while off < len(d):
                null = d[off:].find(b'\x00')
                if null <= 0: break
                try:
                    path = d[off:off+null].decode('ascii')
                    if path.startswith('\\') or path.startswith('/'):
                        sky['faces'].append(path)
                except: pass
                off += null + 1
            env['skybox'] = sky
        
        # RHTW: Weather
        elif cid == 'RHTW' and len(d) >= 20:
            env['weather'] = {
                'color': struct.unpack_from('>I', d, 0)[0],
                'wind': struct.unpack_from('>f', d, 4)[0],
            }
        
        # SPTS: Start Position (from Ghidra: 64-byte header with look_dir + position + BB)
        elif cid == 'SPTS' and len(d) >= 48:
            off = 0
            if c['ver'] >= 2 and len(d) >= 52:
                off = 4  # skip index field
            env['start_pos'] = {
                'look_dir': tuple(struct.unpack_from('>fff', d, off)),
                'position': tuple(struct.unpack_from('>fff', d, off + 12)),
                'bb_min': tuple(struct.unpack_from('>fff', d, off + 24)),
                'bb_max': tuple(struct.unpack_from('>fff', d, off + 36)),
            }
    
    return env

# Complete chunk header size registry from Ghidra ProcessChunk dispatcher
def parse_collision_mesh(chunks):
    """Parse NEHP physics collision mesh. Returns dict with vertices, faces, and per-sector data.
    
    From Ghidra: Asura_Physics_Zone::Process + DoesFaceIntersectOBB.
    
    CRITICAL: Face vertex indices are SECTOR-LOCAL. Each sector has:
      - face_base (field0): absolute index into the face array where this sector's faces start
      - vertex_base (field4): offset to ADD to face vertex indices to get global vertex index
    
    DoesFaceIntersectOBB resolves vertices as:
      global_vtx = face_local_idx + sector.vertex_base
      vertex_pos = vertices[global_vtx]
    
    Header: 6×u32 (nSec, nVtx, nFaces, nFaceVerts, nFaces2, nConvexHulls)
    Sectors: nSec × 52 bytes each (56 in memory, 52 in file — 0x28 is runtime-only)
    Then: vertices(nVtx×12), faces(nFaces×8), normals, faceVerts, materials, convexHulls
    """
    for c in chunks:
        if c['id'] != 'NEHP': continue
        d = c['content']
        if len(d) < 24: return None
        nSec, nVtx, nFaces, nFVerts, nFaces2, nConvex = struct.unpack_from('>6I', d, 0)
        
        off = 24
        sectors = []
        for si in range(nSec):
            if off + 52 > len(d): break
            s = struct.unpack_from('>4I6f', d, off)
            sectors.append({
                'face_base': s[0],       # absolute face index start in face array
                'vertex_base': s[1],     # add to face vertex indices for global index
                'field8': s[2],          # = nFaces + 1
                'nFaces': s[3],
                'bb_min': (s[4], -s[7], -s[8]),
                'bb_max': (s[6], -s[5], -s[9]),
            })
            off += 52
        
        # Read vertices (float32 XYZ, negate Y/Z for display)
        vertices = []
        for vi in range(nVtx):
            if off + 12 > len(d): break
            x, y, z = struct.unpack_from('>fff', d, off)
            vertices.append((x, -y, -z))
            off += 12
        
        # Read raw face data (8 bytes each: 3×uint16 vertex indices + uint16 material)
        # These indices are SECTOR-LOCAL — need vertex_base added per sector
        raw_faces = []
        for fi in range(nFaces):
            if off + 8 > len(d): break
            i0, i1, i2, mat = struct.unpack_from('>HHHH', d, off)
            raw_faces.append((i0, i1, i2, mat))
            off += 8
        
        # Build corrected faces using sector vertex_base remapping
        faces = []
        for sec in sectors:
            fb = sec['face_base']
            vb = sec['vertex_base']
            nf = sec['nFaces']
            sec['face_start'] = len(faces)
            for fi in range(nf):
                abs_fi = fb + fi
                if abs_fi >= len(raw_faces): continue
                li0, li1, li2, mat = raw_faces[abs_fi]
                gi0, gi1, gi2 = li0 + vb, li1 + vb, li2 + vb
                if max(gi0, gi1, gi2) < nVtx:
                    faces.append((gi0, gi1, gi2))
            sec['face_end'] = len(faces)
        
        return {
            'vertices': vertices, 'faces': faces,
            'sectors': sectors, 'nSections': nSec,
        }
    return None

def parse_prop_bounding_boxes(chunks):
    """Parse BBSH bounding box shapes for props/characters.
    
    Returns dict mapping model_name → list of (minX,minY,minZ,maxX,maxY,maxZ) in Asura local space.
    Filters out uninitialized boxes (1e30 sentinel values used for runtime-computed character BBs).
    """
    result = {}
    for c in chunks:
        if c['id'] != 'BBSH': continue
        d = c['content']
        if len(d) < 8: continue
        null = d.find(b'\x00')
        if null < 1: continue
        name = d[:null].decode('ascii', errors='replace')
        off = (null + 4) & ~3
        if off + 4 > len(d): continue
        count = struct.unpack_from('>I', d, off)[0]
        off += 4
        bbs = []
        valid = True
        for i in range(count):
            if off + 24 > len(d): break
            vals = struct.unpack_from('>6f', d, off)
            if any(abs(v) > 1e20 for v in vals):
                valid = False; break
            bbs.append(vals)
            off += 24
        if valid and bbs:
            result[name] = bbs
    return result

def parse_bone_attachments(chunks):
    """Parse TATC bone attachment chunks.
    
    From Ghidra: maps attachment names to bone indices for split character models
    (e.g., homer_gummi_armL → bone index for left arm).
    
    Returns list of {bone_index, name}.
    """
    attachments = []
    for c in chunks:
        if c['id'] != 'TATC': continue
        d = c['content']
        if len(d) < 8: continue
        bone_idx = struct.unpack_from('>I', d, 0)[0]
        null = d.find(b'\x00', 4)
        name = d[4:null].decode('ascii', errors='replace') if null > 4 else ''
        attachments.append({'bone_index': bone_idx, 'name': name})
    return attachments


# ── Additional Chunk Parsers ──

def parse_lfsr_resources(chunks):
    """Parse LFSR resource file list.
    Returns list of {path, hash, field1, field2} for external ASR/pfx dependencies."""
    for c in chunks:
        if c['id'] != 'LFSR': continue
        d = c['content']
        if len(d) < 4: continue
        count = struct.unpack_from('>I', d, 0)[0]
        off = 4; entries = []
        for i in range(count):
            null = d[off:].find(b'\x00')
            if null < 0: break
            path = d[off:off+null].decode('ascii', 'replace')
            off = ((off + null + 1) + 3) & ~3
            if off + 12 > len(d): break
            h = struct.unpack_from('>I', d, off)[0]
            f1 = struct.unpack_from('>I', d, off+4)[0]
            f2 = struct.unpack_from('>I', d, off+8)[0]
            off += 12
            entries.append({'path': path, 'hash': h, 'field1': f1, 'field2': f2})
        return entries
    return []

def parse_summ_level_summary(chunks):
    """Parse SUMM level summary: name, bounding box, scale, music tracks.
    Returns dict with level metadata."""
    for c in chunks:
        if c['id'] != 'SUMM': continue
        d = c['content']
        if len(d) < 8: continue
        off = 0
        n_sections = struct.unpack_from('>I', d, off)[0]; off = 4
        null = d[off:].find(b'\x00')
        if null < 0: continue
        name = d[off:off+null].decode('ascii', 'replace')
        off = ((off + null + 1) + 3) & ~3
        result = {'name': name, 'n_sections': n_sections}
        if off + 4 <= len(d):
            result['hash'] = struct.unpack_from('>I', d, off)[0]; off += 4
        if off + 28 <= len(d):
            bb = struct.unpack_from('>6f', d, off)
            result['bb_min'] = (bb[0], bb[1], bb[2])
            result['bb_max'] = (bb[3], bb[4], bb[5])
            off += 24
            result['scale'] = struct.unpack_from('>f', d, off)[0]; off += 4
        # Extract music/section strings from remaining data
        music = []; sections = []
        i = off
        while i < len(d):
            if 32 <= d[i] < 127:
                end = i
                while end < len(d) and d[end] != 0: end += 1
                if end - i >= 3:
                    s = d[i:end].decode('ascii', 'replace')
                    if s.isprintable():
                        if 'sounds' in s.lower() or '.wav' in s.lower():
                            music.append(s)
                        elif len(s) > 2:
                            sections.append(s)
                i = end + 1
            else: i += 1
        result['music_tracks'] = music
        result['section_names'] = sections
        return result
    return None

def parse_nilm_material_indices(chunks):
    """Parse NILM material index list.
    First u32 = n_sections (matching DOME), remaining = per-strip material indices."""
    for c in chunks:
        if c['id'] != 'NILM': continue
        d = c['content']
        if len(d) < 8: continue
        n = len(d) // 4
        vals = [struct.unpack_from('>I', d, i*4)[0] for i in range(n)]
        return {'n_sections': vals[0], 'indices': vals[1:]}
    return None

def parse_nsig_signals(chunks):
    """Parse NSIG AI signal configuration.
    Returns dict with count and signal config bytes."""
    for c in chunks:
        if c['id'] != 'NSIG': continue
        d = c['content']
        if len(d) < 4: continue
        count = struct.unpack_from('>I', d, 0)[0]
        return {'count': count, 'data_size': len(d), 'raw': d}
    return None

def parse_nsbs_streaming(chunks):
    """Parse NSBS streaming sound definitions.
    Format: header fields + null-terminated wav path + playback params.
    Returns list of streaming sound entries."""
    results = []
    for c in chunks:
        if c['id'] != 'NSBS': continue
        d = c['content']
        # Find all wav path strings in the chunk
        i = 0
        while i < len(d):
            if i + 6 < len(d) and d[i:i+6] in (b'Sounds', b'sounds'):
                end = i
                while end < len(d) and d[end] != 0: end += 1
                path = d[i:end].decode('ascii', 'replace')
                results.append({'path': path, 'offset': i})
                i = end + 1
            else:
                i += 1
    return results

def parse_xeta_tex_anims(chunks):
    """Parse XETA texture/UV animation keyframes.
    Returns list of {hash, count, n_keyframes, keyframe_data}."""
    results = []
    for c in chunks:
        if c['id'] != 'XETA': continue
        d = c['content']
        if len(d) < 16: continue
        h = struct.unpack_from('>I', d, 0)[0]
        count = struct.unpack_from('>I', d, 4)[0]
        n_keys = struct.unpack_from('>I', d, 8)[0]
        unk = struct.unpack_from('>I', d, 12)[0]
        results.append({
            'hash': h, 'count': count, 'n_keyframes': n_keys,
            'unk': unk, 'data_size': len(d) - 16
        })
    return results

def parse_lbta_blend_tables(chunks):
    """Parse LBTA animation blend tables.
    Returns list of {unk, count, entries} with blend transition data."""
    results = []
    for c in chunks:
        if c['id'] != 'LBTA': continue
        d = c['content']
        if len(d) < 4: continue
        count = struct.unpack_from('>I', d, 0)[0]
        # 21-byte entries: blend transition data between animation states
        entries = []
        off = 4
        for i in range(count):
            if off + 21 > len(d): break
            entry_data = d[off:off+21]
            entries.append(entry_data)
            off += 21
        results.append({
            'unk': c['unk'], 'count': count,
            'entries': entries, 'remainder': d[off:]
        })
    return results

def parse_tpmh_morph_targets(chunks):
    """Parse TPMH morph target definitions.
    Format: morph_count(4) + name(string,4-aligned) + morph_count × 72 bytes morph data.
    Morph data contains bone attachment names embedded within entries.
    Returns list of {name, morph_count, attachments}."""
    results = []
    for c in chunks:
        if c['id'] != 'TPMH': continue
        d = c['content']
        if len(d) < 8: continue
        morph_count = struct.unpack_from('>I', d, 0)[0]
        off = 4
        null = d[off:].find(b'\x00')
        if null < 1: continue
        name = d[off:off+null].decode('ascii', 'replace')
        off = ((off + null + 1) + 3) & ~3
        # Extract any printable strings from the morph data (attachment names)
        attachments = []
        morph_end = off + morph_count * 72
        if morph_end <= len(d):
            i = off
            while i < morph_end:
                if 32 <= d[i] < 127:
                    end = i
                    while end < morph_end and d[end] != 0: end += 1
                    if end - i >= 2:
                        s = d[i:end].decode('ascii', 'replace')
                        if s.isprintable() and not any(c2 in s for c2 in '?{}|'):
                            attachments.append(s)
                    i = end + 1
                else: i += 1
        results.append({'name': name, 'morph_count': morph_count, 'attachments': attachments})
    return results

def parse_pmiu_menus(chunks):
    """Parse PMIU GUI menu definitions.
    Returns list of menu dicts with name, widgets, and texture paths."""
    results = []
    for c in chunks:
        if c['id'] != 'PMIU': continue
        d = c['content']
        if len(d) < 8: continue
        # Extract meaningful strings (widget names, texture paths)
        strings = []
        i = 0
        while i < len(d):
            if 32 <= d[i] < 127:
                end = i
                while end < len(d) and d[end] != 0: end += 1
                if end - i >= 3:
                    s = d[i:end].decode('ascii', 'replace')
                    if s.isprintable():
                        strings.append((i, s))
                i = end + 1
            else: i += 1
        menu_name = strings[0][1] if strings else f'menu_{len(results)}'
        widgets = [s for _, s in strings if not s.startswith('\\') and '.' not in s and len(s) > 2]
        textures = [s for _, s in strings if '\\' in s or '.' in s]
        results.append({
            'name': menu_name, 'widgets': widgets, 'textures': textures,
            'size': len(d), 'n_strings': len(strings)
        })
    return results

def parse_naiu_ui_anims(chunks):
    """Parse NAIU UI animation keyframe sequences.
    Returns list of named animation sequences."""
    results = []
    for c in chunks:
        if c['id'] != 'NAIU': continue
        d = c['content']
        if len(d) < 8: continue
        # Extract named sequences
        sequences = []
        i = 0
        while i < len(d):
            if 32 <= d[i] < 127:
                end = i
                while end < len(d) and d[end] != 0: end += 1
                if end - i >= 3:
                    s = d[i:end].decode('ascii', 'replace')
                    if s.isprintable() and not any(c in s for c in '?{}'):
                        sequences.append((i, s))
                i = end + 1
            else: i += 1
        results.append({
            'size': len(d),
            'sequences': [s for _, s in sequences]
        })
    return results

def parse_anrc(chunks):
    """Parse ANRC chunks (camera/render config).
    Returns list of parsed data blocks."""
    results = []
    for c in chunks:
        if c['id'] != 'ANRC': continue
        d = c['content']
        if len(d) < 16: continue
        # Header: count fields + float data (camera paths or render parameters)
        v0 = struct.unpack_from('>I', d, 0)[0]
        v1 = struct.unpack_from('>I', d, 4)[0]
        v2 = struct.unpack_from('>I', d, 8)[0]
        v3 = struct.unpack_from('>I', d, 12)[0]
        # Read float data
        n_floats = (len(d) - 16) // 4
        floats = [struct.unpack_from('>f', d, 16 + i*4)[0] for i in range(min(n_floats, 200))]
        results.append({
            'header': [v0, v1, v2, v3],
            'n_entries': v0,
            'float_data': floats,
            'size': len(d)
        })
    return results

def parse_gulp(chunks):
    """Parse gulp chunk (minimal flags). Returns dict or None."""
    for c in chunks:
        if c['id'] != 'gulp': continue
        d = c['content']
        vals = [struct.unpack_from('>I', d, i*4)[0] for i in range(len(d)//4)]
        return {'values': vals, 'size': len(d)}
    return None


def find_character_parts(files, skeleton):
    """Find all mesh parts for a character (main mesh + split parts).
    
    Split characters like homer_gummi have separate models for armL, armR, head, etc.
    These are identified by naming convention: {skeleton_name}_{variant}_{part}.
    Costume-specific parts (homer_gummi_*, homer_helium_*) are excluded from the
    base body's part list since they use different body meshes at runtime.
    
    Returns dict: {'main': mesh_dict, 'parts': [{'name': str, 'suffix': str, 'mesh': dict}]}
    """
    skel_name = skeleton['char_name'].lower()
    
    def try_parse(f):
        cv = f.get('chunk_ver', 2)
        d = f['data']
        # Try SmoothSkin cv0-2
        m = _parse_smoothskin(d, cv)
        if m: return m
        # Try SmoothSkin cv3
        m = _parse_smoothskin_cv3(d)
        if m: return m
        # Try v6/v14 prop (split character parts are often props)
        if len(d) >= 20:
            fv = struct.unpack_from('>I', d, 4)[0]
            sm = struct.unpack_from('>I', d, 8)[0]
            vc = struct.unpack_from('>I', d, 16)[0]
            if fv == 6 and sm >= 1 and vc < 10000:
                ic = struct.unpack_from('>I', d, 12)[0]
                if 28+(sm-1)*8+vc*16+ic*2 == len(d):
                    return {'nVtx': vc, 'nIdx': ic, 'is_prop': True, 'file_ver': 6}
            elif fv == 14 and sm >= 1 and vc < 10000:
                dls = struct.unpack_from('>I', d, 12)[0]
                if 32+(sm-1)*12+vc*16+dls == len(d):
                    return {'nVtx': vc, 'is_prop': True, 'file_ver': 14}
        return None
    
    main_mesh = None
    parts = []
    
    for f in files:
        if not f['name'].startswith('Stripped') or f['name'] == 'StrippedEnv': continue
        mn = f['name'][8:].lower()
        
        if mn == skel_name:
            m = try_parse(f)
            if m: main_mesh = m
        elif mn.startswith(skel_name + '_'):
            suffix = mn[len(skel_name)+1:]
            if 'eyelid' in suffix: continue
            # Exclude costume-specific parts (they assemble with a different body)
            is_costume = False
            for cp in COSTUME_BODY_MAP:
                if (skel_name + '_' + suffix).startswith(cp + '_') and cp != skel_name:
                    is_costume = True; break
            if is_costume: continue
            m = try_parse(f)
            if m:
                parts.append({'name': f['name'][8:], 'suffix': suffix, 'mesh': m})
    
    # Also check partial name matches if no exact match
    if not main_mesh:
        for f in files:
            if not f['name'].startswith('Stripped') or f['name'] == 'StrippedEnv': continue
            mn = f['name'][8:].lower()
            if skel_name in mn or mn in skel_name:
                m = try_parse(f)
                if m:
                    main_mesh = m
                    break
    
    return {'main': main_mesh, 'parts': parts}


# Costume → body mesh mapping. Derived from NACH animation names.
# e.g. "homer_gummi_homerball_to_gummi" proves gummi uses homerball body.
COSTUME_BODY_MAP = {
    'homer_gummi':  'homerball',   # Gummi Homer = homerball body + gummi head/arms
    'homer_helium': 'homer',       # Helium Homer = homer body + inflated head/arms
    'homerball':    'homerball',   # HomerBall = ball body + head/arms/shoes
}

# Maps attachment names to most likely skeleton bone names
ATTACHMENT_BONE_MAP = {
    'arml': 'l_shoulder',
    'armr': 'r_shoulder',
    'head': 'm_neck2',
    'legl': 'l_hip',
    'legr': 'r_hip',
    'shoel': 'l_ankle',
    'shoer': 'r_ankle',
}


def get_costume_for_animation(anim_name):
    """Determine which costume variant an animation belongs to.
    Returns (costume_prefix, body_name) or (None, None) for regular animations.
    
    Animation naming patterns:
      homer_gummi_*   (43 bones) → body = homerball
      homer_helium_*  (43 bones) → body = homer
      hg_arml_*       (1 bone)   → gummi arm part animation
      hh_head_*       (1 bone)   → helium head part animation  
      homerball_*     (1 bone)   → homerball part animation
    """
    name = anim_name.lower()
    # Check direct costume prefixes
    for prefix, body in COSTUME_BODY_MAP.items():
        if name.startswith(prefix + '_'):
            return prefix, body
    # Check abbreviated part animation prefixes
    PART_ANIM_PREFIXES = {
        'hg_': ('homer_gummi', 'homerball'),
        'hh_': ('homer_helium', 'homer'),
        'hb_': ('homerball', 'homerball'),
    }
    for abbr, (costume, body) in PART_ANIM_PREFIXES.items():
        if name.startswith(abbr):
            return costume, body
    return None, None


def get_costume_parts(files, costume_prefix):
    """Find all split parts for a costume variant.
    Returns list of file dicts matching the costume prefix.
    e.g. costume_prefix='homer_gummi' → [homer_gummi_head, homer_gummi_armL, homer_gummi_armR]
    """
    prefix_lower = costume_prefix.lower() + '_'
    parts = []
    for f in files:
        if not f['name'].startswith('Stripped'): continue
        fn = f['name'][8:].lower()
        if fn.startswith(prefix_lower) and 'eyelid' not in fn:
            parts.append(f)
    return parts


def get_body_mesh_for_animation(files, anim_name, skeletons=None):
    """Determine the correct body mesh for an animation.
    Returns (body_file, costume_prefix, costume_parts) or (None, None, []).
    """
    costume, body_name = get_costume_for_animation(anim_name)
    if not costume:
        return None, None, []
    
    # Find body mesh
    body_name_stripped = 'Stripped' + body_name
    body_file = None
    for f in files:
        if f['name'].lower() == body_name_stripped.lower():
            body_file = f; break
    
    parts = get_costume_parts(files, costume)
    return body_file, costume, parts


def parse_splines(chunks):
    """Parse GamesceneSpline (0x003C) and LiftSpline (0x0040) entities.
    Returns list of {id, type, points, pos} where points are control point lists.
    
    From Ghidra: nPoints at ITNE offset 64, tangent quats, then nPoints Vector3s.
    """
    splines = []
    for c in chunks:
        if c['id'] != 'ITNE': continue
        d = c['content']
        if len(d) < 80: continue
        etype = struct.unpack_from('>H', d, 4)[0]
        if etype not in (0x003C, 0x0040): continue
        eid = struct.unpack_from('>I', d, 0)[0]
        
        # nPoints at offset 64
        if 68 > len(d): continue
        n = struct.unpack_from('>I', d, 64)[0]
        if not (2 <= n <= 50): continue
        
        # Control points at offset 68 + (n-2)*16
        cp_off = 68 + max(0, n - 2) * 16
        points = []
        valid = True
        for pi in range(n):
            if cp_off + 12 > len(d): valid = False; break
            px, py, pz = struct.unpack_from('>fff', d, cp_off)
            if not all(abs(v) < 10000 for v in (px, py, pz)): valid = False; break
            points.append((px, -py, -pz))
            cp_off += 12
        
        if valid and len(points) >= 2:
            # Entity position at offset 32 (for GamesceneSpline node base)
            epos = None
            if len(d) >= 44:
                ex, ey, ez = struct.unpack_from('>fff', d, 32)
                if all(abs(v) < 10000 for v in (ex, ey, ez)):
                    epos = (ex, -ey, -ez)
            splines.append({
                'id': eid, 'type': etype,
                'type_name': ENTITY_TYPES.get(etype, ''),
                'points': points, 'pos': epos,
            })
    return splines

def parse_cliche_locations(chunks):
    """Find AWARD_CLICHE triggers and map them to spatial locations.
    
    Clichés are awarded via GSMS opcode 0x8019. The cliché index (1–31) is stored
    as a uint32 BE in the GSMS message's name field (not param/extra/guid).
    The param field is always 1.0 (show popup boolean).
    
    Trigger location resolution (in priority order):
    1. AdvVolumeTrigger owner → use BB center (most common)
    2. Co-message targets with positions → use first positioned entity
    3. Owner entity scanned position
    4. Cutscene/logic trigger (no fixed position)
    
    Returns list of dicts with cliche_index, cliche_name, slot, pos, bb_min, bb_max,
    owner_type, trigger_type, delay.
    """
    # Build entity GUID→data map
    itne_list = []  # ordered list of (guid, etype, data)
    for c in chunks:
        if c['id'] != 'ITNE': continue
        d = c['content']
        if len(d) < 6: continue
        guid = struct.unpack_from('>I', d, 0)[0]
        etype = struct.unpack_from('>H', d, 4)[0]
        itne_list.append((guid, etype, d))
    
    # Build GUID → position map using scanner
    ent_pos = {}
    for guid, etype, d in itne_list:
        result = _find_entity_position(d)
        if result:
            ent_pos[guid] = result[0]
    
    # Build GUID → BB map for volume triggers
    ent_bb = {}
    for guid, etype, d in itne_list:
        if etype == 0x0014 and len(d) >= 84:  # AdvVolumeTrigger
            bb = struct.unpack_from('>6f', d, 60)
            # Paired format: minX,maxX,minY,maxY,minZ,maxZ → display coords
            minX, maxX = bb[0], bb[1]
            minY, maxY = -bb[3], -bb[2]  # negate+swap
            minZ, maxZ = -bb[5], -bb[4]
            cx = (minX + maxX) / 2; cy = (minY + maxY) / 2; cz = (minZ + maxZ) / 2
            ent_bb[guid] = {
                'pos': (cx, cy, cz),
                'bb_min': (minX, minY, minZ), 'bb_max': (maxX, maxY, maxZ)
            }
    
    results = []
    for gc in chunks:
        if gc['id'] != 'GSMS': continue
        msgs, sc = parse_gsms_messages(gc['content'], gc['ver'], gc['unk'], gc.get('endian', '>'))
        for m in msgs:
            if m['opcode'] != 0x8019: continue
            slot = m['slot']
            
            # Extract cliché index from name field (uint32 BE)
            raw_name = m.get('_raw_name', b'')
            cliche_index = struct.unpack('>I', raw_name[:4])[0] if len(raw_name) >= 4 else 0
            cliche_name = CLICHE_NAMES.get(cliche_index, f'Cliché #{cliche_index}')
            
            # Get slot owner entity
            owner_guid = itne_list[slot][0] if slot < len(itne_list) else 0
            owner_type = itne_list[slot][1] if slot < len(itne_list) else 0
            owner_name = ENTITY_TYPES.get(owner_type, f'0x{owner_type:04X}')
            
            pos = None; bb_min = None; bb_max = None; trigger_type = 'unknown'
            
            # Strategy 1: Owner is AdvVolumeTrigger → use BB center
            if owner_guid in ent_bb:
                bb_data = ent_bb[owner_guid]
                pos = bb_data['pos']
                bb_min = bb_data['bb_min']
                bb_max = bb_data['bb_max']
                trigger_type = 'volume'
            
            # Strategy 2: Co-messages target positioned entities
            if not pos:
                slot_msgs = [m2 for m2 in msgs if m2['slot'] == slot and m2['opcode'] != 0x8019]
                for sm in slot_msgs:
                    if sm['guid'] in ent_bb:
                        bd = ent_bb[sm['guid']]
                        pos = bd['pos']; bb_min = bd['bb_min']; bb_max = bd['bb_max']
                        trigger_type = 'co-volume'; break
                    elif sm['guid'] in ent_pos:
                        pos = ent_pos[sm['guid']]
                        trigger_type = 'co-entity'; break
            
            # Strategy 3: Owner has scanned position
            if not pos and owner_guid in ent_pos:
                pos = ent_pos[owner_guid]
                trigger_type = 'entity'
            
            # Strategy 4: Cutscene/logic trigger (no position)
            if not pos:
                trigger_type = 'cutscene' if owner_type == 0x0003 else 'logic'
            
            results.append({
                'slot': slot, 'pos': pos,
                'bb_min': bb_min, 'bb_max': bb_max,
                'owner_type': owner_name, 'trigger_type': trigger_type,
                'cliche_index': cliche_index, 'cliche_name': cliche_name,
                'delay': m.get('delay', 0.0),
                'label': f"#{cliche_index} {cliche_name} ({owner_name})",
            })
    return results

def parse_blueprints(chunks):
    """Parse EULB blueprint chunks. Returns list of blueprint type groups.
    
    From Ghidra: Asura_Blueprint_System::ReadFromChunkStream + Asura_Blueprint::ReadParameterFromStream.
    Container: version(4) + nTypes(4) + per-type: hash(4) + nBP(4) + name + per-BP: params.
    Parameter values: int(4), float(4), bool(1), hash(4+opt_string), string(4+opt_string).
    """
    PTYPES = {0:'Int',1:'Float',2:'Bool',3:'Hash',4:'String',5:'Vector',6:'Entity',7:'Enum'}
    
    def _read_str(d, off):
        if off >= len(d): return '', off
        null = d[off:off+512].find(b'\x00')
        if null < 0: return '', off
        s = d[off:off+null].decode('ascii', errors='replace')
        # Stream reads strings in 4-byte chunks from current position (Ghidra: ReadString)
        consumed = ((null + 1) + 3) & ~3  # ceil(len_with_null / 4) * 4
        return s, off + consumed
    
    def _read_value(d, off, bp_ver=3):
        if off + 4 > len(d): return None, off
        vt = struct.unpack_from('>i', d, off)[0]; off += 4
        if bp_ver >= 4:
            # Final ELF: v4 has different type 3 handling
            # Type 3: hash(u32) + flag(u32) + if(flag) ReadString
            if vt == 1:
                if off + 4 > len(d): return None, off
                return struct.unpack_from('>f', d, off)[0], off + 4
            elif vt == 2:
                if off >= len(d): return None, off
                return bool(d[off]), off + 1
            elif vt == 3:
                if off + 8 > len(d): return None, off
                hash_val = struct.unpack_from('>I', d, off)[0]; off += 4
                flag = struct.unpack_from('>I', d, off)[0]; off += 4
                name = ''
                if flag:
                    name, off = _read_str(d, off)
                return {'hash': hash_val, 'name': name}, off
            else:  # type 0 and all others → int
                if off + 4 > len(d): return None, off
                return struct.unpack_from('>i', d, off)[0], off + 4
        else:
            # Proto ELF: v3 treats types 3 and 4 the same (one u32 + optional string)
            if vt == 0:
                if off + 4 > len(d): return None, off
                return struct.unpack_from('>i', d, off)[0], off + 4
            elif vt == 1:
                if off + 4 > len(d): return None, off
                return struct.unpack_from('>f', d, off)[0], off + 4
            elif vt == 2:
                if off >= len(d): return None, off
                return bool(d[off]), off + 1
            elif vt in (3, 4):
                if off + 4 > len(d): return None, off
                flag = struct.unpack_from('>I', d, off)[0]; off += 4
                if flag and off < len(d):
                    s, off = _read_str(d, off)
                    return s if vt == 4 else {'hash': flag, 'name': s}, off
                return '' if vt == 4 else {'hash': flag}, off
            else:
                if off + 4 > len(d): return None, off
                return struct.unpack_from('>I', d, off)[0], off + 4
    
    results = []
    for c in chunks:
        if c['id'] != 'EULB': continue
        d = c['content']
        if len(d) < 8: continue
        off = 0
        container_ver = struct.unpack_from('>i', d, off)[0]; off += 4
        n_types = struct.unpack_from('>i', d, off)[0]; off += 4
        if container_ver != 0 or n_types < 0 or n_types > 100: continue
        
        for ti in range(n_types):
            if off + 8 > len(d): break
            type_hash = struct.unpack_from('>I', d, off)[0]; off += 4
            n_bps = struct.unpack_from('>I', d, off)[0]; off += 4
            type_name, off = _read_str(d, off)
            
            blueprints = []
            for bi in range(n_bps):
                if off + 16 > len(d): break
                try:
                    bp_ver = struct.unpack_from('>i', d, off)[0]; off += 4
                    bp_hash = struct.unpack_from('>I', d, off)[0]; off += 4
                    bp_type = struct.unpack_from('>I', d, off)[0]; off += 4
                    bp_name, off = _read_str(d, off)
                    if off + 4 > len(d): break
                    n_params = struct.unpack_from('>i', d, off)[0]; off += 4
                    if n_params < 0 or n_params > 200: break
                    
                    params = []
                    ok = True
                    for pi in range(n_params):
                        if off + 8 > len(d): ok = False; break
                        p_hash = struct.unpack_from('>I', d, off)[0]; off += 4
                        p_name, off = _read_str(d, off)
                        if off + 4 > len(d): ok = False; break
                        p_type = struct.unpack_from('>i', d, off)[0]; off += 4
                        
                        if bp_ver >= 2:
                            if off + 4 > len(d): ok = False; break
                            off += 4  # extra field
                        
                        n_vals = 1
                        if bp_ver >= 3:
                            if off + 4 > len(d): ok = False; break
                            n_vals = struct.unpack_from('>I', d, off)[0]; off += 4
                        if n_vals > 50: ok = False; break
                        
                        vals = []
                        for vi in range(n_vals):
                            v, off = _read_value(d, off, bp_ver)
                            if v is None: ok = False; break
                            vals.append(v)
                        if not ok: break
                        params.append({
                            'name': p_name, 'hash': p_hash,
                            'type': PTYPES.get(p_type, 'T' + str(p_type)),
                            'values': vals,
                        })
                    
                    blueprints.append({
                        'name': bp_name, 'hash': bp_hash, 'type_hash': bp_type,
                        'version': bp_ver, 'params': params, 'ok': ok,
                    })
                except:
                    break
            
            results.append({
                'type_name': type_name, 'type_hash': type_hash,
                'blueprints': blueprints,
            })
    return results

def _decode_packed_quat(packed):
    """Decode a 10-10-10-2 packed quaternion (uint32 BE).
    
    From Ghidra: Asura_Hierarchy_Anim::GetRelativeBonePosition.
    Bits 31-22: component A (10 bits, 0-1023)
    Bits 21-12: component B (10 bits, 0-1023)
    Bits 11-2:  component C (10 bits, 0-1023)
    Bits 1-0:   index of the reconstructed (largest) component in XYZW order
    
    Scale: val = (comp - 511) / 1023.0 (maps 0-1023 to ~[-0.5, +0.5])
    The 4th component is reconstructed from |q|=1.
    Runtime quaternion is [X,Y,Z,W]; we return standard (W,X,Y,Z).
    """
    ca = (packed >> 22) & 0x3FF
    cb = (packed >> 12) & 0x3FF
    cc = (packed >> 2) & 0x3FF
    which = packed & 3
    
    fa = (ca - 511) / 1023.0
    fb = (cb - 511) / 1023.0
    fc = (cc - 511) / 1023.0
    
    rem = max(0.0, 1.0 - fa*fa - fb*fb - fc*fc)
    fd = _math.sqrt(rem)
    
    # Place in XYZW runtime order: stored at [which], [(which+1)%4], [(which+2)%4]
    # Reconstructed at [(which+3)%4]
    q = [0.0] * 4
    q[which] = fa
    q[(which + 1) & 3] = fb
    q[(which + 2) & 3] = fc
    q[(which + 3) & 3] = fd
    
    # Convert runtime [X,Y,Z,W] to standard (W,X,Y,Z)
    return (q[3], q[0], q[1], q[2])


def parse_nach_keyframes(chunks):
    """Parse NACH animation data including packed quaternion tables.
    
    From Ghidra: Asura_Chunk_Hierarchy_CompressedAnim::Process + GetRelativeBonePosition.
    
    On-disk layout for pre-packed (flags & 0x20, all final-build animations):
      1. Bone table: n_bone_entries × 8 bytes (int16×4: nRotKeys, firstRotIdx, nPosKeys, firstPosIdx)
      2. Quaternion table: n_unique_quats × 4 bytes (uint32 packed 10-10-10-2)
      3. Timing table: n_unique_quats × 2 bytes (uint16 normalized frame times)
      4. Position table: n_unique_pos × 12 bytes (float32 XYZ)
    
    On-disk layout for non-pre-packed (proto animations):
      1. Bone table: n_bone_entries × 16 bytes (int32×4: nRotKeys, firstRotIdx, nPosKeys, firstPosIdx)
      2. Quaternion table: n_unique_quats × 4 bytes (uint32 older packed format, converted at load)
      3. Timing table: n_unique_quats × 2 bytes (uint16 normalized frame times)
      4. Position table: n_unique_pos × 12 bytes (float32 XYZ)
    
    Returns list of {name, n_bones, quats, positions, bone_table, pos_bone_table, ...}.
    """
    animations = []
    for c in chunks:
        if c['id'] != 'NACH': continue
        d = c['content']
        flags = c['unk']
        if len(d) < 28: continue
        
        n_bones = struct.unpack_from('>I', d, 0)[0]
        field_14 = struct.unpack_from('>I', d, 4)[0]
        field_18 = struct.unpack_from('>I', d, 8)[0]
        field_1c = struct.unpack_from('>I', d, 12)[0]
        n_unique_quats = struct.unpack_from('>I', d, 16)[0]
        n_unique_pos = struct.unpack_from('>I', d, 20)[0]
        n_sound_events = struct.unpack_from('>I', d, 24)[0]
        
        # Name string at offset 28
        null = d[28:].find(b'\x00')
        name = d[28:28+null].decode('ascii', errors='replace') if null > 0 else '?'
        name_end = ((28 + null + 1) + 3) & ~3
        
        has_root_motion = bool(flags & 0x10)
        pre_packed = bool(flags & 0x20)
        n_bone_entries = (n_bones + 1) if has_root_motion else n_bones
        
        off = name_end
        bone_table = []
        pos_bone_table = []
        
        if pre_packed:
            # Interleaved 8-byte entries: (nRotKeys, firstRotIdx, nPosKeys, firstPosIdx)
            bt_size = n_bone_entries * 8
            if off + bt_size <= len(d):
                for bi in range(n_bone_entries):
                    nrk = struct.unpack_from('>h', d, off)[0]
                    fri = struct.unpack_from('>h', d, off + 2)[0]
                    npk = struct.unpack_from('>h', d, off + 4)[0]
                    fpi = struct.unpack_from('>h', d, off + 6)[0]
                    bone_table.append({'n_rot_keys': nrk, 'first_rot_idx': fri})
                    pos_bone_table.append({'n_pos_keys': npk, 'first_pos_idx': fpi})
                    off += 8
        else:
            # Non-pre-packed: 16-byte entries (int32×4)
            bt_size = n_bone_entries * 16
            if off + bt_size <= len(d):
                for bi in range(n_bone_entries):
                    nrk = struct.unpack_from('>i', d, off)[0]
                    fri = struct.unpack_from('>i', d, off + 4)[0]
                    npk = struct.unpack_from('>i', d, off + 8)[0]
                    fpi = struct.unpack_from('>i', d, off + 12)[0]
                    bone_table.append({'n_rot_keys': nrk, 'first_rot_idx': fri})
                    pos_bone_table.append({'n_pos_keys': npk, 'first_pos_idx': fpi})
                    off += 16
        
        # Quaternion table: uint32 packed (4 bytes each)
        quats = []
        quat_size = n_unique_quats * 4
        if off + quat_size <= len(d):
            for qi in range(n_unique_quats):
                packed = struct.unpack_from('>I', d, off + qi * 4)[0]
                quats.append(_decode_packed_quat(packed))
            off += quat_size
        
        # Timing table: uint16 per keyframe (2 bytes each)
        timing = []
        timing_size = n_unique_quats * 2
        if off + timing_size <= len(d):
            for ti in range(n_unique_quats):
                timing.append(struct.unpack_from('>H', d, off + ti * 2)[0])
            off += timing_size
        
        # Position table (float32 XYZ per position)
        positions = []
        if n_unique_pos > 0:
            pos_size = n_unique_pos * 12
            if off + pos_size <= len(d):
                for pi in range(n_unique_pos):
                    po = off + pi * 12
                    px, py, pz = struct.unpack_from('>fff', d, po)
                    positions.append((px, py, pz))
                off += pos_size
        
        anim = {
            'name': name, 'flags': flags, 'version': c['ver'],
            'n_bones': n_bones, 'n_unique_quats': n_unique_quats,
            'n_unique_pos': n_unique_pos, 'n_sound_events': n_sound_events,
            'loop': bool(flags & 0x02), 'root_motion': has_root_motion,
            'pre_packed': pre_packed,
            'bone_table': bone_table, 'pos_bone_table': pos_bone_table,
            'quats': quats, 'positions': positions, 'timing': timing,
            'total_rot_keys': sum(max(0, b['n_rot_keys']) for b in bone_table),
            'animated_bones': sum(1 for b in bone_table if b['n_rot_keys'] > 0),
            'data_end_offset': off,
        }
        animations.append(anim)
    return animations

# ============================================================
# Container Repack / Writer (Phase E6)
# ============================================================

def repack_chunks(chunks):
    """Reconstruct raw container data from parsed chunk list.
    Returns bytes: 'Asura   ' header + sequential chunks + 4-byte trailing pad."""
    parts = [b'Asura   ']
    for c in chunks:
        content = c['content']
        size = 16 + len(content)
        parts.append(c['id'].encode('ascii'))
        parts.append(struct.pack('>III', size, c['ver'], c['unk']))
        parts.append(content)
    parts.append(b'\x00\x00\x00\x00')  # trailing pad (matches original files)
    return b''.join(parts)

def write_container(data, path, compressed=True, backup=True):
    """Write container data to .wii file.
    
    Args:
        data: raw container bytes from repack_chunks() (starts with 'Asura   ')
        path: output file path
        compressed: True for AsuraZlb (final), False for uncompressed (proto)
        backup: if True and file exists, rename original to .bak
    
    Returns: dict with stats (original_size, output_size, etc.)
    """
    import os, shutil
    
    if backup and os.path.exists(path):
        bak = path + '.bak'
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
    
    if not compressed:
        # Uncompressed proto format: just write raw data
        with open(path, 'wb') as f:
            f.write(data)
        return {'format': 'uncompressed', 'size': len(data)}
    else:
        # AsuraZlb compressed format:
        # "AsuraZlb" (8) + flags (4, =0x01000000) + compressed_size (4) + decompressed_size (4) + zlib_data
        # CRITICAL: zlib must use wbits=13 (8KB window, CINFO=5) — game crashes with default wbits=15
        # CRITICAL: compressed payload INCLUDES the "Asura   " header
        payload = data if data[:8] == b'Asura   ' else b'Asura   ' + data
        co = zlib.compressobj(6, zlib.DEFLATED, 13)  # wbits=13 = 8KB window
        compressed_data = co.compress(payload) + co.flush()
        
        output = bytearray()
        output += b'AsuraZlb'
        output += struct.pack('>I', 0x01000000)  # flags/version
        output += struct.pack('>I', len(compressed_data))  # compressed size
        output += struct.pack('>I', len(payload))  # decompressed size
        output += compressed_data
        
        with open(path, 'wb') as f:
            f.write(bytes(output))
        return {
            'format': 'AsuraZlb', 'raw_size': len(payload),
            'compressed_size': len(output),
            'ratio': len(payload) / len(output) if output else 0,
        }

def validate_container(chunks):
    """Validate chunk integrity before writing.
    Returns list of issues (empty = valid)."""
    issues = []
    guids = set()
    for i, c in enumerate(chunks):
        # Check chunk ID is 4 ASCII chars
        if len(c['id']) != 4:
            issues.append("Chunk[{}]: invalid ID '{}'".format(i, c['id']))
        # Check content exists
        if c['content'] is None:
            issues.append("Chunk[{}]: None content".format(i))
        # Track ITNE GUIDs for uniqueness
        if c['id'] == 'ITNE' and len(c['content']) >= 4:
            guid = struct.unpack_from('>I', c['content'], 0)[0]
            if guid in guids:
                issues.append("Chunk[{}]: duplicate ITNE GUID 0x{:08X}".format(i, guid))
            guids.add(guid)
    return issues

# ── ITNE Entity Modification Helpers ──

def modify_itne_position(content, x, y, z):
    """Modify entity position in ITNE content bytes. Returns new content."""
    if len(content) < 84: return content
    ba = bytearray(content)
    struct.pack_into('>fff', ba, 72, x, y, z)
    return bytes(ba)

def modify_itne_quaternion(content, qx, qy, qz, qw):
    """Modify entity quaternion in ITNE content bytes. Returns new content."""
    if len(content) < 100: return content
    ba = bytearray(content)
    struct.pack_into('>ffff', ba, 84, qx, qy, qz, qw)
    return bytes(ba)

def create_itne_chunk(entity_type, guid, pos, quat=(0,0,0,1), template_content=None):
    """Create a new ITNE chunk for a given entity type.
    
    Args:
        entity_type: uint16 type code (e.g. 0x0007 for PhysicsObj)
        guid: uint32 unique entity ID
        pos: (x, y, z) position tuple
        quat: (qx, qy, qz, qw) quaternion tuple
        template_content: optional existing ITNE content to use as base
    
    Returns: chunk dict ready for insertion
    """
    if template_content and len(template_content) >= 104:
        ba = bytearray(template_content)
    else:
        ba = bytearray(128)  # default size
    
    struct.pack_into('>I', ba, 0, guid)
    struct.pack_into('>H', ba, 4, entity_type)
    struct.pack_into('>fff', ba, 72, *pos)
    struct.pack_into('>ffff', ba, 84, *quat)
    
    return {'id': 'ITNE', 'ver': 0, 'unk': 0, 'content': bytes(ba)}

def find_next_guid(chunks):
    """Find the next available GUID for new entities."""
    max_guid = 0
    for c in chunks:
        if c['id'] == 'ITNE' and len(c['content']) >= 4:
            guid = struct.unpack_from('>I', c['content'], 0)[0]
            if guid > max_guid: max_guid = guid
    return max_guid + 1

# ── FCSR Modification Helpers ──

def replace_fcsr_file_data(chunk, new_data):
    """Replace the file data within an FCSR chunk. Returns modified chunk.
    Preserves purpose_id, sub_type, and filename; updates file_size."""
    d = chunk['content']
    if len(d) < 12: return chunk
    purpose_id = struct.unpack_from('>I', d, 0)[0]
    sub_type = struct.unpack_from('>I', d, 4)[0]
    null = d[12:].find(b'\x00')
    if null < 0: return chunk
    filename = d[12:12+null]
    
    # Rebuild: purpose_id + sub_type + new_file_size + filename + pad + new_data
    name_padded = filename + b'\x00'
    while len(name_padded) % 4 != 0: name_padded += b'\x00'
    
    header = struct.pack('>III', purpose_id, sub_type, len(new_data))
    # Content = header(12) + name(padded) + gap + file_data
    # File data sits at content_end - file_size
    content_before_data = header + name_padded
    # Pad so total content size accommodates the data at the end
    new_content = content_before_data + new_data
    
    return {**chunk, 'content': new_content}

def create_fcsr_chunk(filename, data, sub_type=2, purpose_id=0, chunk_ver=3):
    """Create a new FCSR chunk with the given filename and data."""
    name_bytes = filename.encode('ascii') + b'\x00'
    while len(name_bytes) % 4 != 0: name_bytes += b'\x00'
    
    header = struct.pack('>III', purpose_id, sub_type, len(data))
    content = header + name_bytes + data
    
    return {'id': 'FCSR', 'ver': chunk_ver, 'unk': 0, 'content': content}

def modify_itne_bb(content, etype, bb_min, bb_max):
    """Modify bounding box in ITNE content for volume entities.
    
    Args:
        content: ITNE chunk content bytes
        etype: entity type code
        bb_min: (x, y, z) minimum corner
        bb_max: (x, y, z) maximum corner
    
    BB stored in paired format (minX,maxX,minY,maxY,minZ,maxZ) at type-dependent offsets.
    """
    ba = bytearray(content)
    # AdvVolumeTrigger: offset 60
    if etype == 0x0014 and len(ba) >= 84:
        struct.pack_into('>ffffff', ba, 60,
            bb_min[0], bb_max[0], bb_min[1], bb_max[1], bb_min[2], bb_max[2])
    # CameraVolume: offset 32
    elif etype == 0x0033 and len(ba) >= 56:
        struct.pack_into('>ffffff', ba, 32,
            bb_min[0], bb_max[0], bb_min[1], bb_max[1], bb_min[2], bb_max[2])
    # DeathVolume: offset 32 (first BB) and offset 80 (second BB)
    elif etype == 0x8016:
        if len(ba) >= 56:
            struct.pack_into('>ffffff', ba, 32,
                bb_min[0], bb_max[0], bb_min[1], bb_max[1], bb_min[2], bb_max[2])
        if len(ba) >= 104:
            struct.pack_into('>ffffff', ba, 80,
                bb_min[0], bb_max[0], bb_min[1], bb_max[1], bb_min[2], bb_max[2])
    return bytes(ba)

# ── TPL Texture Encoder (PNG → TPL) ──

def _morton_index(x, y, w, h):
    """Compute Morton/Z-order index for a pixel within a tile."""
    idx = 0; bit = 1
    tx = x; ty = y
    while tx > 0 or ty > 0:
        if tx & 1: idx |= bit
        bit <<= 1
        if ty & 1: idx |= bit
        bit <<= 1
        tx >>= 1; ty >>= 1
    return idx

def _encode_i8_block(pixels, px, py, w, h):
    """Encode an 8×4 block as I8 format (8-bit intensity)."""
    block = bytearray(32)
    for by in range(4):
        for bx in range(8):
            x = px + bx; y = py + by
            if x < w and y < h:
                r, g, b = pixels[y * w + x][:3]
                gray = int(0.299 * r + 0.587 * g + 0.114 * b)
                block[by * 8 + bx] = min(255, max(0, gray))
    return bytes(block)

def _encode_cmpr_block(pixels, px, py, w, h):
    """Encode an 8×8 CMPR block (4 DXT1 sub-blocks of 4×4)."""
    result = bytearray()
    for sby in range(2):
        for sbx in range(2):
            sx = px + sbx * 4; sy = py + sby * 4
            # Gather 4×4 pixel colors
            colors = []
            for y in range(4):
                for x in range(4):
                    cx = sx + x; cy = sy + y
                    if cx < w and cy < h:
                        colors.append(pixels[cy * w + cx][:3])
                    else:
                        colors.append((0, 0, 0))
            # Find min/max colors for palette
            min_c = list(colors[0]); max_c = list(colors[0])
            for c in colors:
                for i in range(3):
                    if c[i] < min_c[i]: min_c[i] = c[i]
                    if c[i] > max_c[i]: max_c[i] = c[i]
            # Encode as RGB565
            c0 = ((max_c[0] >> 3) << 11) | ((max_c[1] >> 2) << 5) | (max_c[2] >> 3)
            c1 = ((min_c[0] >> 3) << 11) | ((min_c[1] >> 2) << 5) | (min_c[2] >> 3)
            if c0 < c1: c0, c1 = c1, c0; max_c, min_c = min_c, max_c
            # GC CMPR: big-endian, swap bytes within each uint16
            result += struct.pack('>HH', c0, c1)
            # Palette: c0, c1, 2/3*c0+1/3*c1, 1/3*c0+2/3*c1
            pal = [max_c, min_c,
                   [(2*max_c[i]+min_c[i])//3 for i in range(3)],
                   [(max_c[i]+2*min_c[i])//3 for i in range(3)]]
            # Index each pixel to nearest palette entry
            indices = 0
            for pi, c in enumerate(colors):
                best = 0; best_d = 999999
                for ci, pc in enumerate(pal):
                    d = sum((c[i]-pc[i])**2 for i in range(3))
                    if d < best_d: best_d = d; best = ci
                indices |= (best << (30 - pi * 2))
            result += struct.pack('>I', indices)
    return bytes(result)

def encode_tpl(width, height, pixels, fmt=1):
    """Encode pixel data as TPL (Nintendo Texture Palette Library).
    
    Args:
        width, height: image dimensions
        pixels: list of (R,G,B) or (R,G,B,A) tuples, row-major
        fmt: GX format (1=I8, 14=CMPR)
    
    Returns: bytes of TPL file
    """
    # TPL header
    tpl = bytearray()
    tpl += struct.pack('>III', TPL_MAGIC, 1, 0x0C)  # magic, 1 image, table at 0x0C
    
    # Image table entry (at 0x0C): image_hdr_offset, palette_offset
    img_hdr_off = 0x14  # right after table
    tpl += struct.pack('>II', img_hdr_off, 0)  # no palette
    
    # Image header (at img_hdr_off = 0x14)
    data_off = img_hdr_off + 0x24  # header is 36 bytes
    
    # Encode pixel data
    if fmt == 1:  # I8: 8×4 blocks
        bw = (width + 7) // 8; bh = (height + 3) // 4
        pixel_data = bytearray()
        for by in range(bh):
            for bx in range(bw):
                pixel_data += _encode_i8_block(pixels, bx*8, by*4, width, height)
    elif fmt == 14:  # CMPR: 8×8 blocks
        bw = (width + 7) // 8; bh = (height + 7) // 8
        pixel_data = bytearray()
        for by in range(bh):
            for bx in range(bw):
                pixel_data += _encode_cmpr_block(pixels, bx*8, by*8, width, height)
    else:
        raise ValueError("Unsupported TPL format: {}".format(fmt))
    
    # Image header (36 bytes)
    tpl += struct.pack('>HH', height, width)
    tpl += struct.pack('>I', fmt)
    tpl += struct.pack('>I', data_off)
    tpl += struct.pack('>IIII', 0, 0, 1, 1)  # wrap_s, wrap_t, min_filter, mag_filter
    tpl += struct.pack('>f', 0.0)  # lod_bias
    tpl += struct.pack('>BBBB', 0, 0, 0, 0)  # edge_lod, min_lod, max_lod, unpacked
    
    tpl += pixel_data
    return bytes(tpl)

def png_to_tpl(png_path, fmt=1):
    """Convert a PNG file to TPL bytes.
    
    Args:
        png_path: path to PNG file
        fmt: GX format (1=I8, 14=CMPR)
    
    Returns: TPL bytes
    """
    try:
        from PIL import Image
        img = Image.open(png_path).convert('RGB')
        pixels = list(img.getdata())
        return encode_tpl(img.width, img.height, pixels, fmt)
    except ImportError:
        raise RuntimeError("PIL/Pillow required for PNG→TPL conversion")

CHUNK_HEADER_SIZES = {
    '1VAN': 0x18, 'AMDS': 0x10, 'ANRC': 0x14, 'BABL': 0x10,
    'BBSH': 0x10, 'BVRM': 0x18, 'BYKS': 0x1C, 'CATC': 0x18,
    'DDAP': 0x10, 'DNER': 0x10, 'DNSH': 0x14, 'DOME': 0x14,
    'DPHS': 0x10, 'EULB': 0x10, 'FCSR': 0x1C, ' GOF': 0x10,
    'GSMS': 0x14, 'HPDS': 0x10, 'ITNE': 0x18, 'KTNF': 0x10,
    'LBTA': 0x18, 'LFSR': 0x14, 'LFXT': 0x14, 'LKSH': 0x10,
    'LRTM': 0x14, 'MNAH': 0x10, 'MNTM': 0x14, 'MSDS': 0x10,
    'NACH': 0x2C, 'NAIU': 0x10, 'NAXT': 0x10, 'NEHP': 0x28,
    'NILM': 0x14, 'NKSH': 0x18, 'NOHP': 0x14, 'NSBS': 0x18,
    'NSHS': 0x14, 'NSIG': 0x74, 'OFNF': 0x10, 'PAHS': 0x10,
    'PMIU': 0x10, 'RHTW': 0x10, 'RTTC': 0x14, 'SIVL': 0x14,
    'SPTS': 0x40, 'STUC': 0x24, 'SUMM': 0x14, 'TATC': 0x14,
    'TAXA': 0x10, 'TBUS': 0x10, 'TEXF': 0x14, 'TLLD': 0x10,
    'TNOF': 0x10, 'TPMH': 0x14, 'TPXF': 0x14, 'TRTA': 0x10,
    'TSXF': 0x14, 'TTNE': 0x20, 'TXET': 0x14, 'TXTH': 0x14,
    'TXTL': 0x14, 'TXTP': 0x18, 'UAIU': 0x10, 'VELD': 0x10,
}

def cmd_env(args):
    """Extract StrippedEnv level geometry as OBJ mesh."""
    for path in args.input:
        print(f"\nLevel geometry: {path}")
        data = read_asura(path)
        chunks = parse_chunks(data)
        files = extract_fcsr_files(chunks)
        
        env_data = None
        for f in files:
            if f['name'] == 'StrippedEnv':
                env_data = f['data']; break
        
        if env_data is None:
            print("  No StrippedEnv found"); continue
        
        off = 0
        version = struct.unpack_from('>I', env_data, off)[0]; off += 4
        nMeshes = struct.unpack_from('>I', env_data, off)[0]; off += 4
        flags = struct.unpack_from('>I', env_data, off)[0]; off += 4
        off += 4  # nNormTable / nPositionsTotal
        off += 4  # nVerticesTotal
        has_dl = (flags & 1) != 0
        has_normals = (flags & 2) != 0
        if has_normals: off += 4
        off += 4  # dlTotalSize or nIdxMap
        
        all_pos = []
        all_tris = []
        gpo = 0  # global position offset
        
        for m in range(nMeshes):
            nPos = struct.unpack_from('>I', env_data, off)[0]; off += 4
            nVtx = struct.unpack_from('>I', env_data, off)[0]; off += 4
            nStrips = struct.unpack_from('>I', env_data, off)[0]; off += 4
            off += 24  # bounding box
            
            for i in range(nPos):
                fx = struct.unpack_from('>f', env_data, off)[0]; off += 4
                fy = struct.unpack_from('>f', env_data, off)[0]; off += 4
                fz = struct.unpack_from('>f', env_data, off)[0]; off += 4
                all_pos.append((fx, -fy, -fz))  # 180° X rotation: negate Y and Z
            
            off += nVtx * 4  # texcoords
            off += nVtx * 3  # colors
            
            for s in range(nStrips):
                if has_dl:
                    off += 4  # stripFlags
                    off += 4  # materialID
                    off += 4  # nExpandedIndices
                    dlSize = struct.unpack_from('>I', env_data, off)[0]; off += 4
                    if dlSize > 0:
                        dl = env_data[off:off+dlSize]
                        doff = 0
                        while doff < dlSize - 3:
                            cmd = dl[doff]
                            if cmd == 0x98:
                                cnt = struct.unpack_from('>H', dl, doff+1)[0]
                                if 3 <= cnt <= 65535:
                                    vd = doff + 3
                                    for stride in [7, 6]:
                                        ve = vd + cnt * stride
                                        if ve > dlSize: continue
                                        pi0 = struct.unpack_from('>H', dl, vd)[0]
                                        if pi0 >= nPos: continue
                                        pis = []
                                        ok = True
                                        for vi in range(cnt):
                                            pi = struct.unpack_from('>H', dl, vd+vi*stride)[0]
                                            if pi >= nPos: ok = False; break
                                            pis.append(pi + gpo)
                                        if ok and pis:
                                            for i in range(len(pis)-2):
                                                a,b,c = pis[i],pis[i+1],pis[i+2]
                                                if a==b or b==c or a==c: continue
                                                if i%2==0: all_tris.append((a,b,c))
                                                else: all_tris.append((a,c,b))
                                            doff = ve; break
                                    else: doff += 1
                                else: doff += 1
                            elif cmd == 0: doff += 1
                            else: doff += 1
                        off += dlSize
                else:
                    off += 4  # nIndices
                    nComp = struct.unpack_from('>I', env_data, off)[0]; off += 4
                    off += 4  # materialID
                    off += nComp * 2
            
            gpo += nPos
        
        bn = os.path.splitext(os.path.basename(path))[0]
        out = args.output or '.'
        os.makedirs(out, exist_ok=True)
        obj_path = os.path.join(out, bn + '_Env_mesh.obj')
        
        with open(obj_path, 'w') as f:
            f.write(f"# {bn} Level Geometry\n# {len(all_pos)} verts, {len(all_tris)} tris, {nMeshes} meshes\n\n")
            for x,y,z in all_pos:
                f.write(f"v {x:.6f} {y:.6f} {z:.6f}\n")
            f.write(f"\no level_geometry\ns 1\n")
            for i0,i1,i2 in all_tris:
                f.write(f"f {i0+1} {i1+1} {i2+1}\n")
        
        print(f"  {nMeshes} meshes → {len(all_pos)} verts, {len(all_tris)} tris")
        print(f"  → {obj_path}")

GSMS_OPCODE_NAMES = {
    # Asura engine messages (0x0000–0x005F)
    0x0001:'CREATE', 0x0002:'DESTROY', 0x0003:'ENABLE', 0x0004:'DISABLE',
    0x0005:'ACTIVATE', 0x0006:'DEACTIVATE', 0x0007:'RESET', 0x0008:'ATTACH',
    0x000A:'DETACH', 0x000E:'SOUND_CTRL', 0x0011:'LIGHT_CTRL', 0x0012:'SPAWN',
    0x0014:'DESPAWN', 0x0015:'LINK', 0x0018:'SET_PARENT', 0x0019:'CLEAR_PARENT',
    0x001A:'DIALOG_TRIGGER', 0x001B:'CHECKPOINT', 0x001C:'SECTION_TOGGLE',
    0x001D:'SCREEN_FADE', 0x001E:'SET_PROPERTY', 0x0026:'LOGIC_EVAL',
    0x002B:'ENV_ANIM_CTRL', 0x002C:'MUSIC_CTRL', 0x002E:'PROXY_FWD',
    0x002F:'DEBUG_MSG', 0x0030:'STOP', 0x0031:'RESUME', 0x0032:'PAUSE',
    0x0033:'VOLUME_TRIGGER', 0x0036:'NODE_LINK_A', 0x0037:'NODE_LINK_B',
    0x0038:'NODE_LINK_C', 0x0039:'GUARD_ZONE', 0x003B:'SPLINE_CTRL',
    0x003C:'GAMESCENE_SPLINE', 0x003E:'TRIGGER_GROUP',
    0x0042:'TELEPORT', 0x0043:'TELEPORT_DEST',
    0x0044:'WAYPOINT_DBG', 0x0045:'FORCE_FIELD', 0x0046:'ATTRACTOR_CTRL',
    0x0048:'CONVEYOR', 0x004C:'CONSOLE_VAR', 0x0051:'STREAM_SND_CTRL',
    0x0052:'CUTSCENE_CTRL', 0x0053:'PLAY_DIALOGUE', 0x0054:'SPLITTER',
    0x0056:'COUNTED_TRIG', 0x0057:'LOOK_AT_TRIG', 0x0058:'CLOCK_TRIG',
    0x005F:'PLAYER_JOIN_P2', 0x0060:'PLAYER_JOIN_P1',
    # Simpsons game messages (0x8000+) — from Ghidra Project_ServerMessageHandler
    0x8003:'WIN_LEVEL', 0x8004:'FAIL_LEVEL', 0x8005:'DAMAGE',
    0x8007:'SET_START_POINTS', 0x8008:'PLAYER_CTRL', 0x8009:'NPC_SPAWNER_CTRL',
    0x800B:'PHYS_IMPULSE', 0x800C:'PLAYER_ABILITY', 0x800D:'HOMER_BALL_CTRL',
    0x800E:'SHOVER_PUSH', 0x800F:'HOMER_BALL_STATE',
    0x8010:'UPDRAFT_CTRL', 0x8011:'BUNNY_CTRL', 0x8012:'TRAMPOLINE_CTRL',
    0x8013:'INTERACT_USE', 0x8014:'HOB_CTRL', 0x8015:'SET_UPGRADES',
    0x8016:'DEATH_VOLUME', 0x8017:'NPC_SPAWN', 0x8018:'HOB_PORT',
    0x8019:'AWARD_CLICHE', 0x801A:'INTERACT_TRIG', 0x801B:'SEESAW_CTRL',
    0x801D:'RECALL_MOB', 0x801E:'GUARD_ZONE_S', 0x801F:'HOMER_BALL_FOOD',
    0x8020:'STUBBORN_APE', 0x8021:'LARD_LAD', 0x8022:'OBJECTIVE',
    0x8023:'SAVE_CHECKPOINT', 0x8024:'TRANSITION', 0x8025:'GROENING',
    0x8026:'ADD_SCORE', 0x8027:'SCORE_MSG', 0x8028:'LARDLAD_FLAP',
    0x8029:'STATE_TRIGGER', 0x802A:'PARACHUTE',
    0x802B:'DESTR_OBJ_CTRL', 0x802C:'COMPLETE_LEVEL',
    0x802D:'COMPLETE_TUTORIAL', 0x802E:'LIFT_CTRL', 0x802F:'SET_SPAWN_PT',
    0x8030:'OPEN_MENU', 0x8031:'GAME_MODE_CTRL', 0x8033:'GAME_SETTING',
    0x8034:'CAMERA_CTRL', 0x8035:'HUD_CTRL', 0x8036:'LEVEL_RESET',
    0x8037:'TIMER_EVENT', 0x8038:'SCORE_EVENT', 0x8039:'HOB_FORCE',
    0x803A:'CINEMATIC_CTRL', 0x803B:'GTS_CTRL', 0x803C:'CAM_VOLUME',
    0x8048:'FORCE_INTERACT',
}

# Old cliché image filenames from Frontend.wii texture paths (DO NOT match actual in-game names)
# Image files were not renamed when clichés were reshuffled during development.
# Use CLICHE_NAMES (above) for actual in-game names from Menu_En.asrBE TXTH.
CLICHE_IMAGE_NAMES = {
    1:'Double Jump', 2:'Collectible', 3:'Trampoline', 4:'Invisible Barrier',
    5:'Obvious Weakness', 6:'Keycard', 7:'Crate', 8:'Exploding Barrel',
    9:'Switches & Levers', 10:'Pressure Pad', 11:'AI Walls', 12:'Portal',
    13:'Timed Trial', 14:'Escort', 15:'Power-up', 16:'Steep Slope',
    17:"Can't Swim", 18:'Giant Saw Blades', 19:'Combo Punch', 20:'Enemy Spawner',
    21:'Infinite Ledge Hang', 22:'Chasm Death', 23:'Evil Genius', 24:'Lava',
    25:'Flying Boat', 26:'Elemental Enemies', 27:'The Doors',
    28:'Reused Enemies', 29:'Tutorial Hell', 30:'Cracked Up', 31:'Worst Cliché',
}

def parse_gsms_messages(content, ver, flags, endian='>'):
    """Parse GSMS chunk content into list of message dicts.
    
    Based on Ghidra analysis of Asura_Chunk_StaticMessages::Process.
    Format varies by version; all known game files use version 6.
    
    Returns list of dicts: {slot, opcode, guid, delay, param, extra, name}
    """
    d = content
    e = endian
    if len(d) < 4: return [], []
    
    nSlots = _u32(d, 0, e)
    off = 4
    
    # Version >= 2: nTables field (must be 0 or 1)
    if ver >= 2:
        if off + 4 > len(d): return [], []
        off += 4  # skip nTables
    
    # Slot count table
    if off + nSlots > len(d): return [], []
    slot_counts = [d[off + i] for i in range(nSlots)]
    off += nSlots
    
    messages = []
    for slot_i in range(nSlots):
        for _ in range(slot_counts[slot_i]):
            if off + 8 > len(d): return messages, slot_counts
            opcode = _u16(d, off, e)
            name_len = _u16(d, off + 2, e)
            guid = _u32(d, off + 4, e)
            off += 8
            
            delay = param = 0.0
            extra = 0
            if ver >= 3 and off + 4 <= len(d):
                delay = _f32(d, off, e); off += 4
            if ver >= 5 and off + 4 <= len(d):
                param = _f32(d, off, e); off += 4
            if ver >= 6 and off + 4 <= len(d):
                extra = _u32(d, off, e); off += 4
            
            # Flag bit 0 padding (version > 3)
            if (flags & 1) and ver > 3 and off + 4 <= len(d):
                off += 4
            
            # Entity name string (variable length, 4-byte aligned)
            ent_name = ''
            raw_name = b''
            if name_len > 0:
                aligned = (name_len + 3) & ~3
                if off + aligned <= len(d):
                    raw_name = bytes(d[off:off + name_len])
                    raw_padded = bytes(d[off:off + aligned])
                    null = raw_name.find(b'\x00')
                    if null >= 0: check = raw_name[:null]
                    else: check = raw_name
                    try:
                        ent_name = check.decode('ascii')
                        if not all(32 <= ord(c) < 127 for c in ent_name):
                            ent_name = ''  # binary data, not a real name
                    except:
                        ent_name = ''
                    off += aligned
            
            messages.append({
                'slot': slot_i, 'opcode': opcode, 'guid': guid,
                'delay': delay, 'param': param, 'extra': extra, 'name': ent_name,
                '_raw_name': raw_name, '_name_len': name_len,
            })
    
    return messages, slot_counts


def repack_gsms(messages, slot_counts, ver=6, flags=0, nTables=None):
    """Repack GSMS messages into chunk content bytes.
    
    messages: list of dicts from parse_gsms_messages (must have _raw_name, _name_len)
    slot_counts: list of int (messages per slot)
    ver: GSMS version (always 6 in known files)
    flags: chunk unknown field (flag bits)
    nTables: nTables value (read from original chunk, or auto-detect: 1 if any ATTACH, else 0)
    """
    out = bytearray()
    nSlots = len(slot_counts)
    out += struct.pack('>I', nSlots)
    
    if ver >= 2:
        if nTables is None:
            # Auto-detect: if first slot has ATTACH messages, nTables=1
            nTables = 1 if any(m['opcode'] == 0x000E and m['slot'] == 0 for m in messages) else 0
        out += struct.pack('>I', nTables)
    
    # Slot counts (1 byte each)
    for sc in slot_counts:
        out += struct.pack('B', sc)
    
    # Messages (in slot order, matching slot_counts)
    for m in messages:
        name_len = m.get('_name_len', 0)
        raw_name = m.get('_raw_name', b'')
        
        # If name was edited (text changed), rebuild raw_name
        if m['name'] and (not raw_name or raw_name.find(b'\x00') == 0):
            raw_name = m['name'].encode('ascii') + b'\x00'
            name_len = len(raw_name)
        
        out += struct.pack('>H', m['opcode'])
        out += struct.pack('>H', name_len)
        out += struct.pack('>I', m['guid'])
        
        if ver >= 3:
            out += struct.pack('>f', m['delay'])
        if ver >= 5:
            out += struct.pack('>f', m['param'])
        if ver >= 6:
            out += struct.pack('>I', m['extra'])
        if (flags & 1) and ver > 3:
            out += struct.pack('>I', 0)  # flag padding
        
        if name_len > 0:
            aligned = (name_len + 3) & ~3
            # Pad raw_name to aligned length
            padded = raw_name + b'\x00' * (aligned - len(raw_name))
            out += padded[:aligned]
    
    return bytes(out)

def cmd_script(args):
    """Decode GSMS level script bytecode."""
    for path in args.input:
        print(f"\nScript: {path}")
        data = read_asura(path)
        chunks = parse_chunks(data)

        # Build entity GUID → type map from ITNE chunks
        entity_map = {}
        for c in chunks:
            if c['id'] != 'ITNE': continue
            d = c['content']
            if len(d) >= 8:
                eid = struct.unpack_from('>I', d, 0)[0]
                etype = struct.unpack_from('>H', d, 4)[0]
                entity_map[eid] = etype

        gsms_list = [c for c in chunks if c['id'] == 'GSMS']
        bn = os.path.splitext(os.path.basename(path))[0]
        out_dir = args.output or '.'
        os.makedirs(out_dir, exist_ok=True)

        for gi, gc in enumerate(gsms_list):
            messages, slot_counts = parse_gsms_messages(gc['content'], gc['ver'], gc['unk'], gc.get('endian', '>'))
            if not messages: continue

            csv_path = os.path.join(out_dir, f"{bn}_script_{gi}.csv")
            with open(csv_path, 'w', newline='') as f:
                w = csv.writer(f)
                w.writerow(['msg#', 'slot', 'opcode', 'opcode_name', 'entity_guid',
                            'delay', 'param', 'extra', 'entity_name'])
                for i, m in enumerate(messages):
                    opname = GSMS_OPCODE_NAMES.get(m['opcode'], f"0x{m['opcode']:04X}")
                    delay_s = f"{m['delay']:.2f}" if m['delay'] else ''
                    param_s = f"{m['param']:.4f}" if m['param'] else ''
                    extra_s = f"0x{m['extra']:08X}" if m['extra'] else ''
                    w.writerow([i, m['slot'], f"0x{m['opcode']:04X}", opname,
                               f"0x{m['guid']:08X}", delay_s, param_s, extra_s, m['name']])

            print(f"  GSMS[{gi}]: ver={gc['ver']} slots={len(slot_counts)} msgs={len(messages)} → {csv_path}")
            # Opcode summary
            from collections import Counter
            op_counts = Counter(m['opcode'] for m in messages)
            for op, cnt in op_counts.most_common(10):
                opname = GSMS_OPCODE_NAMES.get(op, '')
                print(f"    0x{op:04X} {opname:20s}: {cnt}")


# ============================================================
# TXTH — Hashed Localised Text Parser
# ============================================================

# Wii controller icon mapping — Private Use Area (PUA) characters in TXTH/NLLD text
# These reference sprites from wii_icons.tga and represent Wii controller buttons/gestures
WII_ICON_MAP = {
    0xE800: '[Stick]', 0xE801: '[A]',
    0xE900: '[🕹Stick]', 0xE901: '[D-Pad]', 0xE902: '[+Pad]',
    0xE907: '[Z]', 0xE908: '[C-Stick]', 0xE909: '[C]',
    0xE90B: '[B]', 0xE90C: '[A]', 0xE90D: '[Swing🎮]',
    0xE910: '[⚡Power]', 0xE911: '[−]', 0xE918: '[Hold A]',
    0xE91A: '[🪝Grapple]', 0xE91B: '[🔄Transform]', 0xE91C: '[👋Gesture]',
    0xE91E: '[🦅Flap]', 0xE91F: '[🟢Gummi]', 0xE920: '[💣GummiShoot]',
    0xE921: '[💥GummiBlast]', 0xE927: '[🖐Levitate]',
    0xE92A: '[👊Stomp]', 0xE92B: '[⚡Lightning]', 0xE92C: '[🎷Sax]',
    0xE92D: '[📢Recruit]', 0xE92E: '[👉Command]', 0xE92F: '[❤Revive]',
    0xE955: '[−Menu]',
}

def format_text_with_icons(text):
    """Replace PUA icon characters with readable labels for display."""
    out = []
    for ch in text:
        cp = ord(ch)
        if cp in WII_ICON_MAP:
            out.append(WII_ICON_MAP[cp])
        elif 0xE000 <= cp <= 0xF8FF:
            out.append(f'[icon:0x{cp:04X}]')
        else:
            out.append(ch)
    return ''.join(out)


# ============================================================
# STUC — Cutscene Definition Parser
# ============================================================

def _read_asura_string(d, off):
    """Read null-terminated, 4-byte stream-aligned string from content bytes."""
    if off >= len(d): return '', off
    null = d[off:].find(b'\x00')
    if null < 0: return '', len(d)
    s = d[off:off+null].decode('latin-1', errors='replace')
    aligned_off = off + ((null + 1 + 3) & ~3)
    return s, aligned_off

def _read_indexed_strings(d, off):
    """Read count + max_slots + (slot_index, string) array."""
    if off + 8 > len(d): return [], 0, off
    count = struct.unpack_from('>I', d, off)[0]; off += 4
    max_slots = struct.unpack_from('>I', d, off)[0]; off += 4
    entries = []
    for _ in range(count):
        if off + 4 > len(d): break
        idx = struct.unpack_from('>I', d, off)[0]; off += 4
        s, off = _read_asura_string(d, off)
        entries.append((idx, s))
    return entries, max_slots, off

def parse_stuc_chunk(content, ver=18):
    """Parse STUC cutscene chunk content.
    
    Decoded from Ghidra: Asura_Chunk_Cutscene::Process + Asura_Cutscene::LoadFromChunk.
    
    Format (version 18):
      Header:
        uint32    field1 (track count / cutscene type)
        uint32    field2 (event count)
        float32×3 camera position XYZ
        string    cutscene_name (null-term, 4-aligned)
        uint32    param1 (ver > 4, cutscene subtype)
        uint32    param2 (ver > 5, additional flags)
      
      Body (2 optional name strings + duration + 4 indexed string lists):
        string    display_name (usually empty)
        string    target_name (usually empty)
        float32   duration (seconds)
        actors[]:       (slot_index, model_name) per actor
        asr_paths[]:    (slot_index, asr_file_path) per actor
        anim_names[]:   (slot_index, animation_name) per animation
        anim_paths[]:   (slot_index, anim_asr_path) per animation
      
      Tail (20 bytes):
        uint32  playback_ctrl (timing/playback control flags)
        uint16  zero
        uint16  flags (0x7530-0x7538, or 0xFFFF for camera-only)
        uint32  nTrackEntries (track index array count, usually 0)
        float32 playback_speed (usually 1.0)
        uint32  extra_field (ver > 14, usually 0)
    
    Returns dict with all parsed fields, or None on failure.
    """
    d = content
    if len(d) < 24: return None
    
    # Header
    field1 = struct.unpack_from('>I', d, 0)[0]
    field2 = struct.unpack_from('>I', d, 4)[0]
    pos_x, pos_y, pos_z = struct.unpack_from('>fff', d, 8)
    name, off = _read_asura_string(d, 0x14)
    
    # Process version-dependent fields
    param1 = 0; param2 = 0
    if ver > 4 and off + 4 <= len(d):
        param1 = struct.unpack_from('>I', d, off)[0]; off += 4
    if ver > 5 and off + 4 <= len(d):
        param2 = struct.unpack_from('>I', d, off)[0]; off += 4
    
    # LoadFromChunk: 2 strings
    display_name, off = _read_asura_string(d, off)
    target_name, off = _read_asura_string(d, off)
    
    # Duration
    duration = struct.unpack_from('>f', d, off)[0] if off + 4 <= len(d) else 0.0; off += 4
    
    # 4 indexed string lists
    actors, actor_slots, off = _read_indexed_strings(d, off)
    asr_paths, asr_slots, off = _read_indexed_strings(d, off)
    anim_names, anim_slots, off = _read_indexed_strings(d, off)
    anim_paths, anim_path_slots, off = _read_indexed_strings(d, off)
    
    # Tail (20 bytes)
    playback_ctrl = struct.unpack_from('>I', d, off)[0] if off + 4 <= len(d) else 0; off += 4
    flags_raw = struct.unpack_from('>I', d, off)[0] if off + 4 <= len(d) else 0; off += 4
    flags = flags_raw & 0xFFFF
    n_track_entries = struct.unpack_from('>I', d, off)[0] if off + 4 <= len(d) else 0; off += 4
    if n_track_entries > 0 and off + n_track_entries * 4 <= len(d):
        off += n_track_entries * 4  # skip track index array
    playback_speed = struct.unpack_from('>f', d, off)[0] if off + 4 <= len(d) else 1.0; off += 4
    extra_field = 0
    if ver > 14 and off + 4 <= len(d):
        extra_field = struct.unpack_from('>I', d, off)[0]; off += 4
    
    return {
        'field1': field1, 'field2': field2,
        'position': (pos_x, pos_y, pos_z),
        'name': name, 'param1': param1, 'param2': param2,
        'display_name': display_name, 'target_name': target_name,
        'duration': duration,
        'actors': actors, 'actor_slots': actor_slots,
        'asr_paths': asr_paths, 'asr_slots': asr_slots,
        'anim_names': anim_names, 'anim_slots': anim_slots,
        'anim_paths': anim_paths, 'anim_path_slots': anim_path_slots,
        'playback_ctrl': playback_ctrl, 'flags': flags,
        'n_track_entries': n_track_entries,
        'playback_speed': playback_speed, 'extra_field': extra_field,
    }

def _asura_hash_id(s):
    """Asura_GetHashID: mul31 hash on lowercased ASCII string."""
    h = 0
    for c in s.lower().encode('ascii', errors='replace'):
        h = (h * 31 + c) & 0xFFFFFFFF
    return h

def parse_txth_chunk(content, endian='>'):
    """Parse a TXTH chunk into list of {label, hash, text}.
    
    TXTH layout:
      count             (uint32) — number of text entries
      hash_seed         (uint32) — file-level hash identifier
      total_text_bytes  (uint32) — sum of all char_count × 2
    
    Per entry (count times):
      hash              (uint32) — mul31 hash of lowercased label
      char_count        (uint32) — number of UTF-16 characters (incl null)
      text              (char_count × 2 bytes, UTF-16)
    
    Label section (at end):
      label_data_size   (uint32)
      labels            (sequential null-terminated Latin-1 strings)
    """
    d = content
    e = endian
    utf16 = 'utf-16-le' if e == '<' else 'utf-16-be'
    if len(d) < 12: return []
    
    count = _u32(d, 0, e)
    hash_seed = _u32(d, 4, e)
    total_text_bytes = _u32(d, 8, e)
    
    # Parse text entries
    entries = []
    off = 12
    for i in range(count):
        if off + 8 > len(d): break
        h = _u32(d, off, e)
        char_count = _u32(d, off+4, e)
        if char_count > 10000 or off + 8 + char_count * 2 > len(d): break
        raw_text = d[off+8:off+8+char_count*2].decode(utf16, errors='replace')
        text = raw_text.rstrip('\x00')
        entries.append({'hash': h, 'text': text, '_char_count': char_count})
        off += 8 + char_count * 2
    
    # Parse label section
    if off + 4 <= len(d):
        label_off = off + 4  # skip label_data_size field
        labels = []
        while label_off < len(d):
            null = d[label_off:].find(b'\x00')
            if null < 0: break
            label = d[label_off:label_off+null].decode('latin-1')
            if label:
                labels.append(label)
            label_off += null + 1
        
        for i, label in enumerate(labels):
            if i < len(entries):
                entries[i]['label'] = label
    
    # Fill in missing labels
    for ent in entries:
        if 'label' not in ent:
            ent['label'] = f'unknown_{ent["hash"]:08x}'
    
    return entries

def cmd_text(args):
    """Extract TXTH hashed localised text to CSV."""
    for path in args.input:
        print(f"\nText: {path}")
        data = read_asura(path)
        chunks = parse_chunks(data)
        
        txth_chunks = [c for c in chunks if c['id'] == 'TXTH']
        if not txth_chunks:
            print("  No TXTH chunks found"); continue
        
        bn = os.path.splitext(os.path.basename(path))[0]
        out_dir = args.output or '.'
        os.makedirs(out_dir, exist_ok=True)
        
        total = 0
        for ti, tc in enumerate(txth_chunks):
            entries = parse_txth_chunk(tc['content'], tc.get('endian', '>'))
            suffix = f'_{ti}' if len(txth_chunks) > 1 else ''
            csv_path = os.path.join(out_dir, f"{bn}_text{suffix}.csv")
            
            with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.DictWriter(f, ['label', 'hash', 'text'])
                w.writeheader()
                for e in entries:
                    w.writerow({
                        'label': e['label'],
                        'hash': f"0x{e['hash']:08x}",
                        'text': e['text']
                    })
            
            total += len(entries)
            
            if not args.quiet:
                for e in entries:
                    if 'DEFAULT' in e['text']: continue
                    print(f"  {e['label']:45s} \"{e['text'][:80]}\"")
            
            print(f"  TXTH[{ti}]: {len(entries)} entries → {csv_path}")
        
        print(f"  Total: {total} text entries")

# ============================================================
# ELF / DOL Parser — Executable Analysis
# ============================================================

def parse_elf(data):
    """Parse a PPC ELF (32-bit big-endian). Returns dict with sections, symbols."""
    if data[:4] != b'\x7fELF' or data[4] != 1:
        return None
    e_entry = struct.unpack_from('>I', data, 24)[0]
    e_shoff = struct.unpack_from('>I', data, 32)[0]
    e_shentsize = struct.unpack_from('>H', data, 46)[0]
    e_shnum = struct.unpack_from('>H', data, 48)[0]
    e_shstrndx = struct.unpack_from('>H', data, 50)[0]
    result = {'type':'elf','entry':e_entry,'machine':struct.unpack_from('>H',data,18)[0],
              'sections':[],'symbols':[],'has_dwarf':False,'text_size':0,'data_size':0,'bss_size':0}
    if e_shoff == 0 or e_shnum == 0:
        return result
    shstr_off = e_shoff + e_shstrndx * e_shentsize
    shstr_sh_offset = struct.unpack_from('>I', data, shstr_off + 16)[0]
    shstr_sh_size = struct.unpack_from('>I', data, shstr_off + 20)[0]
    shstrtab = data[shstr_sh_offset:shstr_sh_offset + shstr_sh_size]
    def _gs(o):
        e2 = shstrtab.find(b'\x00', o)
        return shstrtab[o:e2].decode('ascii', errors='replace') if e2 >= 0 else ''
    SHT = {0:'NULL',1:'PROGBITS',2:'SYMTAB',3:'STRTAB',4:'RELA',8:'NOBITS'}
    symtab_sec = strtab_sec = None
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        sh_name_off = struct.unpack_from('>I', data, o)[0]
        sh_type = struct.unpack_from('>I', data, o+4)[0]
        sh_flags = struct.unpack_from('>I', data, o+8)[0]
        sh_addr = struct.unpack_from('>I', data, o+12)[0]
        sh_offset = struct.unpack_from('>I', data, o+16)[0]
        sh_size = struct.unpack_from('>I', data, o+20)[0]
        sh_link = struct.unpack_from('>I', data, o+24)[0]
        sh_entsize = struct.unpack_from('>I', data, o+36)[0]
        name = _gs(sh_name_off)
        sec = {'index':i,'name':name,'type':sh_type,'type_name':SHT.get(sh_type,f'0x{sh_type:X}'),
               'flags':sh_flags,'addr':sh_addr,'offset':sh_offset,'size':sh_size,'link':sh_link,'entsize':sh_entsize}
        result['sections'].append(sec)
        if sh_type == 2: symtab_sec = sec
        if name == '.strtab': strtab_sec = sec
        if '.debug' in name: result['has_dwarf'] = True
        if name == '.text': result['text_size'] = sh_size
        if name == '.data': result['data_size'] = sh_size
        if name == '.bss': result['bss_size'] = sh_size
    if symtab_sec and strtab_sec:
        sym_off = symtab_sec['offset']; sym_ent = symtab_sec['entsize'] or 16
        nsyms = symtab_sec['size'] // sym_ent
        st_data = data[strtab_sec['offset']:strtab_sec['offset']+strtab_sec['size']]
        STT = {0:'NOTYPE',1:'OBJECT',2:'FUNC',3:'SECTION',4:'FILE'}
        STB = {0:'LOCAL',1:'GLOBAL',2:'WEAK'}
        for i in range(nsyms):
            so = sym_off + i * sym_ent
            st_n = struct.unpack_from('>I', data, so)[0]
            st_v = struct.unpack_from('>I', data, so+4)[0]
            st_sz = struct.unpack_from('>I', data, so+8)[0]
            st_info = data[so+12]; st_shndx = struct.unpack_from('>H', data, so+14)[0]
            e2 = st_data.find(b'\x00', st_n)
            nm = st_data[st_n:e2].decode('ascii',errors='replace') if e2>=0 else ''
            if not nm or st_v == 0: continue
            result['symbols'].append({'name':nm,'addr':st_v,'size':st_sz,
                'bind':STB.get(st_info>>4,f'b{st_info>>4}'),'type':STT.get(st_info&0xF,f't{st_info&0xF}'),'section':st_shndx})
    result['symbols'].sort(key=lambda s: s['addr'])
    return result

def parse_dol(data):
    """Parse a Nintendo DOL executable."""
    if len(data) < 0x100: return None
    sections = []
    for i in range(7):
        sz = struct.unpack_from('>I', data, 0x90+i*4)[0]
        if sz > 0:
            sections.append({'name':f'Text[{i}]','type_name':'TEXT',
                'file_offset':struct.unpack_from('>I',data,i*4)[0],
                'addr':struct.unpack_from('>I',data,0x48+i*4)[0],'size':sz})
    for i in range(11):
        sz = struct.unpack_from('>I', data, 0xAC+i*4)[0]
        if sz > 0:
            sections.append({'name':f'Data[{i}]','type_name':'DATA',
                'file_offset':struct.unpack_from('>I',data,0x1C+i*4)[0],
                'addr':struct.unpack_from('>I',data,0x64+i*4)[0],'size':sz})
    return {'type':'dol','entry':struct.unpack_from('>I',data,0xE0)[0],
            'bss_addr':struct.unpack_from('>I',data,0xD8)[0],'bss_size':struct.unpack_from('>I',data,0xDC)[0],
            'sections':sections,'text_size':sum(s['size'] for s in sections if 'Text' in s['name']),
            'data_size':sum(s['size'] for s in sections if 'Data' in s['name']),'symbols':[]}

def parse_symbol_map(text):
    """Parse a Ghidra/Dolphin symbol map. Returns list of symbol dicts."""
    symbols = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) < 5: continue
        try:
            addr = int(parts[0],16); size = int(parts[1],16); sec = int(parts[3]); name = parts[4]
            scope = parts[5].rstrip('\r') if len(parts) > 5 else 'Global'
        except (ValueError, IndexError): continue
        stype = 'FUNC' if sec == 4 else 'OBJECT' if sec in (1,8) else 'NOTYPE'
        symbols.append({'name':name,'addr':addr,'size':size,'bind':scope,'type':stype,'section':sec})
    symbols.sort(key=lambda s: s['addr'])
    return symbols

def demangle_ppc_name(name):
    """Basic C++ demangling for Metrowerks CodeWarrior PPC symbols."""
    if '__' not in name: return name
    # Handle __ct/__dt/__sinit specially (start with __)
    import re
    if name.startswith('__ct__'):
        rest = name[6:]
        for m in ['CF','F']:
            idx = rest.find(m)
            if idx > 0:
                cls = re.sub(r'^\d+', '', rest[:idx])
                if cls: return f'{cls}::(ctor)'
        cls = re.sub(r'^\d+', '', rest)
        return f'{cls}::(ctor)' if cls else name
    if name.startswith('__dt__'):
        rest = name[6:]
        for m in ['CF','F']:
            idx = rest.find(m)
            if idx > 0:
                cls = re.sub(r'^\d+', '', rest[:idx])
                if cls: return f'{cls}::(dtor)'
        cls = re.sub(r'^\d+', '', rest)
        return f'{cls}::(dtor)' if cls else name
    if name.startswith('__sinit_'):
        return f'static_init({name[8:]})'
    if name.startswith('@') or name.startswith('__'): return name
    parts = name.split('__', 1)
    if len(parts) != 2: return name
    func, rest = parts
    for marker in ['CF','F']:
        idx = rest.find(marker)
        if idx > 0:
            cls = re.sub(r'^\d+', '', rest[:idx])
            if cls: return f'{cls}::{func}'
    cls = re.sub(r'^\d+', '', rest)
    return f'{cls}::{func}' if cls else name

def addr_to_dol_offset(dol_info, vaddr):
    """Convert virtual address to DOL file offset."""
    for sec in dol_info['sections']:
        if sec['addr'] <= vaddr < sec['addr'] + sec['size']:
            return sec['file_offset'] + (vaddr - sec['addr'])
    return None

# ============================================================
# Debug Variables & Gecko Codes
# ============================================================

_DEBUG_VARIABLES = [
    # (name, type, default, min, max, category)
    ('s_bDevMode','bool',0,0,1,'Cheats'),('s_bInvincible','bool',0,0,1,'Cheats'),
    ('s_bImmuneToDamage','bool',0,0,1,'Cheats'),('s_bUnlimitedCalories','bool',0,0,1,'Cheats'),
    ('s_bUnlimitedGummiCalories','bool',0,0,1,'Cheats'),('s_bUnlimitedHelium','bool',0,0,1,'Cheats'),
    ('s_bFullPower','bool',0,0,1,'Cheats'),('s_bFlyCamera','bool',0,0,1,'Cheats'),
    ('s_bFlyPlayer','bool',0,0,1,'Cheats'),('s_bFlyPlayerWithoutCollisions','bool',0,0,1,'Cheats'),
    ('g_bForceEnableControls','bool',0,0,1,'Cheats'),('s_fGameTimeScale','float',1.0,0,10,'Cheats'),
    ('s_bBallOn','bool',0,0,1,'Abilities'),('s_bGummiOn','bool',0,0,1,'Abilities'),
    ('s_bHeliumOn','bool',0,0,1,'Abilities'),('s_bSlamOn','bool',0,0,1,'Abilities'),
    ('s_bBoostOn','bool',0,0,1,'Abilities'),('s_bSaxaphoneOn','bool',0,0,1,'Abilities'),
    ('s_bBullhornOn','bool',0,0,1,'Abilities'),('s_bOrdersOn','bool',0,0,1,'Abilities'),
    ('s_bHOBIsActive','bool',0,0,1,'Abilities'),('s_bHOBCanFlick','bool',0,0,1,'Abilities'),
    ('s_bHOBCanPickup','bool',0,0,1,'Abilities'),('s_bHOBCanUseLightning','bool',0,0,1,'Abilities'),
    ('s_bMaggieActive','bool',0,0,1,'Abilities'),
    ('s_bBartIsBuddy','bool',0,0,1,'Buddy'),('s_bHomerIsBuddy','bool',0,0,1,'Buddy'),
    ('s_bLisaIsBuddy','bool',0,0,1,'Buddy'),('s_bMargeIsBuddy','bool',0,0,1,'Buddy'),
    ('s_bBuddyIsFighting','bool',0,0,1,'Buddy'),('s_bBuddyIsIdle','bool',0,0,1,'Buddy'),
    ('s_bBuddyIsRunning','bool',0,0,1,'Buddy'),('s_bBuddyHealthBarActive','bool',0,0,1,'Buddy'),
    ('s_bHUDVisible','bool',1,0,1,'HUD'),('s_bPlayerHealthBarActive','bool',1,0,1,'HUD'),
    ('s_bPowerBarVisible','bool',1,0,1,'HUD'),('s_bBossHealthEnabled','bool',0,0,1,'HUD'),
    ('s_bCollectibleHUDActive','bool',1,0,1,'HUD'),('s_bDisplayTutorialText','bool',1,0,1,'HUD'),
    ('s_bDisableInterruptScreens','bool',0,0,1,'HUD'),('s_uClicheToShow','int',0,0,100,'HUD'),
    ('dvs_bShowFPS','bool',0,0,1,'Debug Render'),('dvs_bShowQAInfo','bool',0,0,1,'Debug Render'),
    ('dvs_bShowDebugInfo','bool',0,0,1,'Debug Render'),('s_bRenderDebugInfo','bool',0,0,1,'Debug Render'),
    ('s_bRenderAIGuids','bool',0,0,1,'Debug Render'),('s_bRenderAIPathLines','bool',0,0,1,'Debug Render'),
    ('s_bRenderAIBehaviourStates','bool',0,0,1,'Debug Render'),('s_bLineRenderingEnabled','bool',0,0,1,'Debug Render'),
    ('dbg_bRenderSkin','bool',1,0,1,'Debug Render'),
    ('dbg_fSimp_Gummi_Speed','float',0,0,100,'Homer Gummi'),('dbg_fSimp_Gummi_Acceleration','float',0,0,100,'Homer Gummi'),
    ('dbg_fSimp_Gummi_TurnSpeed','float',0,0,100,'Homer Gummi'),('dbg_fSimp_Homer_GummiBlastDamage','float',0,0,100,'Homer Gummi'),
    ('dbg_fSimp_Homer_GummiBlastRadius','float',0,0,100,'Homer Gummi'),('dbg_fSimp_Homer_GummiShotDamage','float',0,0,100,'Homer Gummi'),
    ('dbg_fSimp_Homer_GummiShotRadius','float',0,0,100,'Homer Gummi'),('dbg_fSimp_RechargeTime','float',0,0,60,'Homer Gummi'),
    ('dbg_fRemoteIndividualSpikeThreshold','float',0,0,10,'Wii Motion'),
    ('dbg_fNunchuckIndividualSpikeThreshold','float',0,0,10,'Wii Motion'),
    ('dbg_uMinimumRemoteShakes','int',3,3,15,'Wii Motion'),('dbg_uMinimumNunchuckShakes','int',3,3,15,'Wii Motion'),
    ('dbg_uMinimumBothShakes','int',2,2,15,'Wii Motion'),('dbg_bUseTilt','bool',0,0,1,'Wii Motion'),
    ('s_fBerserkDamageMultiplier','float',1,0,10,'Combat'),('dbg_fSlamDamageRadius','float',0,0,100,'Combat'),
    ('dbg_fSlamVelocity','float',0,0,100,'Combat'),('s_fFlickDamage','float',0,0,100,'Combat'),
    ('s_fFlickRange','float',0,0,100,'Combat'),('s_fLightningDamage','float',0,0,100,'Combat'),
    ('s_fLightningRange','float',0,0,100,'Combat'),('s_fImmuneDuration','float',0,0,60,'Combat'),
    ('s_fAudioMusicVolume','float',1.0,0,1,'Audio'),('s_fAudioSfxVolume','float',1.0,0,1,'Audio'),
    ('s_fAudioVoiceVolume','float',1.0,0,1,'Audio'),
    ('dbg_bUseLegacyCamera','bool',0,0,1,'Camera'),('dbg_fDesiredDistance','float',0,0,100,'Camera'),
    ('dbg_fFollowCamPitchLimit','float',0,0,90,'Camera'),('dbg_fSmoothingSpeed','float',0,0,100,'Camera'),
    ('s_uNumberOfLocalPlayers','int',1,1,4,'Splitscreen'),('s_iNumberOfCameras','int',1,1,4,'Splitscreen'),
    ('s_iNumSplitScreens','int',1,1,4,'Splitscreen'),
]

def get_debug_variables():
    """Return the debug variable database as list of dicts."""
    return [{'name':v[0],'type':v[1],'default':v[2],'min':v[3],'max':v[4],'category':v[5]} for v in _DEBUG_VARIABLES]

def find_symbol_address(symbols, name):
    """Find a symbol's address by name."""
    for s in symbols:
        if s['name'] == name: return s['addr']
    return None

def generate_gecko_code(addr, value, size=4):
    """Generate a Dolphin Gecko code line. Address is auto-masked (0x80 stripped)."""
    masked = addr & 0x01FFFFFF  # strip 0x80xxxxxx → 0x00xxxxxx
    pfx = {1:'00',2:'02',4:'04'}[size]
    if size==1: return f'{pfx}{masked:06X} 000000{value&0xFF:02X}'
    elif size==2: return f'{pfx}{masked:06X} 0000{value&0xFFFF:04X}'
    return f'{pfx}{masked:06X} {value&0xFFFFFFFF:08X}'

def generate_gecko_float(addr, fval):
    """Generate Gecko code for a float32."""
    masked = addr & 0x01FFFFFF
    return f'04{masked:06X} {struct.unpack(">I",struct.pack(">f",fval))[0]:08X}'

def generate_splitscreen_gecko(build, num_players=4):
    """Generate Gecko codes for splitscreen. build='proto'|'final'."""
    if build == 'proto':
        cam_base,num_cam,num_pl = 0x803D4D98,0x804ECC44,0x804EE91C
    else:
        cam_base,num_cam,num_pl = 0x8044C2D8,0x80628DDC,0x8062AE14
    codes = [('Force player count',generate_gecko_code(num_pl,num_players)),
             ('Force camera count',generate_gecko_code(num_cam,num_players))]
    layouts = {2:[(0,0,.5,1),(.5,0,.5,1)],3:[(0,0,.5,.5),(.5,0,.5,.5),(.25,.5,.5,.5)],
               4:[(0,0,.5,.5),(.5,0,.5,.5),(0,.5,.5,.5),(.5,.5,.5,.5)]}[num_players]
    for ci,(vx,vy,vw,vh) in enumerate(layouts):
        b = cam_base + ci * 0x88
        codes += [(f'Cam{ci} X',generate_gecko_float(b+0x3C,vx)),(f'Cam{ci} Y',generate_gecko_float(b+0x40,vy)),
                  (f'Cam{ci} W',generate_gecko_float(b+0x44,vw)),(f'Cam{ci} H',generate_gecko_float(b+0x48,vh))]
        if ci >= 2:
            codes.append((f'Activate cam{ci}',generate_gecko_code(b+0x84,1,1)))
    return codes

# ============================================================
# DOL Binary Patcher — Executable Modification
# ============================================================

# Known safe patches for splitscreen (verified via Ghidra + DOL scan)
_DOL_PATCHES = {
    'final': {
        'player_cap': {
            'desc': 'Player count cap 2→4 (ProcessPlayerChange)',
            'dol_offset': 0x0022F6F0,
            'vaddr': 0x802336B0,
            'original': 0x38600002,
            'patched': 0x38600004,
            'safe': True,
        },
        'controller_limit': {
            'desc': 'Allow 4 controllers (stops disconnecting Wiimotes 3-4)',
            'dol_offset': 0x0026401C,
            'vaddr': 0x80267FDC,
            'original': 0x38000002,  # li r0, 2
            'patched': 0x38000004,   # li r0, 4
            'safe': True,
        },
        'profile_relocate': {
            'desc': 'Relocate profile array to free BSS + expand 2→4',
            'compound': True,
            'safe': True,
            'new_base': 0x80490000,
            'patches': [
                (0x002C4BE0, 0x3FE08059, 0x3FE08049, 'lis r31 (Initialise)'),
                (0x002C4BE4, 0x3BFFE9D0, 0x3BFF0000, 'addi r31 (Initialise)'),
                (0x002C4C08, 0x281E0002, 0x281E0004, 'cmplwi loop 2→4'),
                (0x002C4C44, 0x3C608059, 0x3C608049, 'lis r3 (GetPlayerProfile)'),
                (0x002C4C48, 0x3863E9D0, 0x38630000, 'addi r3 (GetPlayerProfile)'),
                (0x002C4C74, 0x3C608059, 0x3C608049, 'lis r3 (__sinit)'),
                (0x002C4C7C, 0x3863E9D0, 0x38630000, 'addi r3 (__sinit)'),
                (0x002C4C8C, 0x38E00002, 0x38E00004, 'li r7 count 2→4'),
            ],
        },
    },
    'proto': {
        'player_cap': {
            'desc': 'Player count cap 2→4 (ProcessPlayerChange)',
            'dol_offset': 0x001F3EA8,
            'vaddr': 0x801F8168,
            'original': 0x38600002,
            'patched': 0x38600004,
            'safe': True,
        },
        'profile_relocate': {
            'desc': 'Relocate profile array to free BSS + expand 2→4',
            'compound': True,
            'safe': True,
            'new_base': 0x803B0000,
            'patches': [
                (0x0025ABAC, 0x3FE08045, 0x3FE0803B, 'lis r31 (Initialise)'),
                (0x0025ABB0, 0x3BFF48A8, 0x3BFF0000, 'addi r31 (Initialise)'),
                (0x0025ABD4, 0x281E0002, 0x281E0004, 'cmplwi loop 2→4'),
                (0x0025AC00, 0x3C608045, 0x3C60803B, 'lis r3 (GetPlayerProfile)'),
                (0x0025AC04, 0x386348A8, 0x38630000, 'addi r3 (GetPlayerProfile)'),
                (0x0025AC24, 0x3C608045, 0x3C60803B, 'lis r3 (__sinit)'),
                (0x0025AC2C, 0x386348A8, 0x38630000, 'addi r3 (__sinit)'),
                (0x0025AC3C, 0x38E00002, 0x38E00004, 'li r7 count 2→4'),
            ],
        },
    },
}


def get_dol_patches(build):
    """Get available DOL patches for a build ('proto' or 'final')."""
    return _DOL_PATCHES.get(build, {})


def verify_dol_patch(dol_data, patch):
    """Check if a DOL patch can be applied. Returns (can_apply, status_msg)."""
    if patch.get('compound'):
        for dol_off, orig, new, desc in patch['patches']:
            if dol_off + 4 > len(dol_data):
                return False, f"DOL too small for {desc}"
            current = struct.unpack_from('>I', dol_data, dol_off)[0]
            if current == new:
                return False, "Already patched"
            if current != orig:
                return False, f"Mismatch at 0x{dol_off:08X}: got 0x{current:08X}, expected 0x{orig:08X} ({desc})"
        return True, f"Ready ({len(patch['patches'])} instructions)"
    off = patch['dol_offset']
    if off + 4 > len(dol_data):
        return False, "DOL too small for this patch offset"
    current = struct.unpack_from('>I', dol_data, off)[0]
    if current == patch['patched']:
        return False, "Already patched"
    if current != patch['original']:
        return False, f"Unexpected instruction: 0x{current:08X} (expected 0x{patch['original']:08X})"
    return True, "Ready to patch"


def apply_dol_patch(dol_data, patch):
    """Apply a single DOL patch. Returns modified DOL data."""
    data = bytearray(dol_data)
    if patch.get('compound'):
        for dol_off, orig, new, desc in patch['patches']:
            struct.pack_into('>I', data, dol_off, new)
    else:
        struct.pack_into('>I', data, patch['dol_offset'], patch['patched'])
    return bytes(data)


def patch_dol_file(input_path, output_path, build, patch_names, backup=True):
    """Patch a DOL file. Returns list of (name, success, message)."""
    import shutil
    dol_data = open(input_path, 'rb').read()
    patches = get_dol_patches(build)
    results = []
    if backup and input_path == output_path:
        shutil.copy2(input_path, input_path + '.bak')
    modified = bytearray(dol_data)
    for name in patch_names:
        if name not in patches:
            results.append((name, False, "Unknown patch")); continue
        p = patches[name]
        can, msg = verify_dol_patch(dol_data, p)
        if not can:
            results.append((name, False, msg)); continue
        if p.get('compound'):
            for dol_off, orig, new, desc in p['patches']:
                struct.pack_into('>I', modified, dol_off, new)
            results.append((name, True, f"Applied {len(p['patches'])} patches: {p['desc']}"))
        else:
            struct.pack_into('>I', modified, p['dol_offset'], p['patched'])
            results.append((name, True, f"Applied: {p['desc']}"))
    with open(output_path, 'wb') as f:
        f.write(bytes(modified))
    return results


# ============================================================
# Ghidra Export Parsers — Decompilation & XML
# ============================================================

def parse_ghidra_c(text, progress_fn=None):
    """Parse Ghidra decompiled C export. Indexes ALL function bodies.
    Returns list of dicts: {name, class, method, start_line, end_line, signature, comment, lines}
    """
    import re
    lines = text.split('\n')
    funcs = []
    i = 0; n = len(lines)
    while i < n:
        line = lines[i].rstrip('\r')
        # Skip empty, indented, preprocessor, typedefs, structs
        if not line or line[0] in (' ','\t','{','}','#','*') or line.startswith('typedef') or line.startswith('struct') or line.startswith('enum'):
            i += 1; continue
        # Pattern 1: "// __thiscall/cdecl/stdcall ..." comment before function
        is_comment = line.startswith('// __') and '(' in line
        # Pattern 2: Direct function signature (non-indented, has parens, not declaration)
        is_sig = not is_comment and '(' in line and not line.endswith(';') and not line.startswith('//')
        if not is_comment and not is_sig:
            i += 1; continue
        if is_comment:
            comment = line
            j = i + 1
            while j < n and not lines[j].rstrip('\r'): j += 1
            if j >= n: i += 1; continue
            sig = lines[j].rstrip('\r'); sig_line = j
        else:
            comment = ''
            j = i - 1
            while j >= 0 and not lines[j].strip(): j -= 1
            if j >= 0 and lines[j].strip().startswith('//'):
                comment = lines[j].rstrip('\r')
            sig = line; sig_line = i
        # Find opening { within 5 lines
        k = sig_line + 1; found = False
        while k < min(sig_line + 6, n):
            if lines[k].rstrip('\r') == '{': found = True; break
            k += 1
        if not found: i += 1; continue
        # Find matching }
        depth = 1; m = k + 1
        while m < n and depth > 0:
            for ch in lines[m]:
                if ch == '{': depth += 1
                elif ch == '}': depth -= 1
                if depth == 0: break
            if depth > 0: m += 1
        # Extract class::method
        cls = ''; method = ''
        sm = re.search(r'([\w<>]+)::([\w~]+)\s*\(', sig)
        if sm: cls = sm.group(1); method = sm.group(2)
        else:
            fm = re.search(r'\b(\w+)\s*\(', sig)
            if fm: method = fm.group(1)
        start = i if is_comment else sig_line
        funcs.append({
            'name': f"{cls}::{method}" if cls else method, 'class': cls, 'method': method,
            'addr_hex': '', 'start_line': start + 1, 'end_line': m + 1,
            'body_start': k + 1, 'body_end': m + 1, 'lines': m - k + 1,
            'signature': sig.strip(), 'comment': comment.strip(),
        })
        i = m + 1
    return funcs


def get_ghidra_func_code(text_lines, func_info):
    """Extract the full code of a function from the .c file lines.
    text_lines: list of lines (from text.split('\\n'))
    func_info: dict from parse_ghidra_c with start_line/end_line
    """
    s = func_info['start_line'] - 1  # convert to 0-indexed
    e = func_info['end_line']
    return '\n'.join(line.rstrip('\r') for line in text_lines[s:e])


def parse_ghidra_xml(path, progress_fn=None):
    """Parse Ghidra XML export for rich function/symbol metadata.
    Returns dict with 'functions', 'symbols', 'comments', 'namespaces'.
    """
    import xml.etree.ElementTree as ET
    funcs = []; syms = []; namespaces = set()
    for event, elem in ET.iterparse(path, events=['end']):
        if elem.tag == 'FUNCTION':
            addr = int(elem.get('ENTRY_POINT', '0'), 16)
            name = elem.get('NAME', '')
            ar = elem.find('ADDRESS_RANGE')
            end_addr = int(ar.get('END', '0'), 16) if ar is not None else 0
            rt = elem.find('RETURN_TYPE')
            ti = elem.find('TYPEINFO_CMT')
            sf = elem.find('STACK_FRAME')
            # Extract params from REGISTER_VAR
            params = []
            for rv in elem.findall('REGISTER_VAR'):
                params.append({'name': rv.get('NAME',''), 'reg': rv.get('REGISTER',''),
                               'type': rv.get('DATATYPE','')})
            # Stack vars
            stack_vars = []
            if sf is not None:
                for sv in sf.findall('STACK_VAR'):
                    stack_vars.append({'name': sv.get('NAME',''), 'offset': sv.get('STACK_PTR_OFFSET',''),
                                       'type': sv.get('DATATYPE',''), 'size': sv.get('SIZE','')})
            # Parse namespace from name
            ns = ''
            if '::' in name:
                parts = name.rsplit('::', 1)
                ns = parts[0] + '::'
                short_name = parts[1]
            else:
                short_name = name
            if ns: namespaces.add(ns)
            funcs.append({
                'addr': addr, 'name': name, 'short_name': short_name, 'namespace': ns,
                'size': (end_addr - addr + 1) if end_addr > addr else 0,
                'return_type': rt.get('DATATYPE', '') if rt is not None else '',
                'signature': ti.text.strip() if ti is not None and ti.text else '',
                'params': params, 'stack_vars': stack_vars,
                'stack_size': int(sf.get('LOCAL_VAR_SIZE', '0'), 0) if sf is not None else 0,
            })
            elem.clear()
        elif elem.tag == 'SYMBOL':
            addr = int(elem.get('ADDRESS', '0'), 16)
            name = elem.get('NAME', '')
            ns = elem.get('NAMESPACE', '')
            primary = elem.get('PRIMARY', 'n')
            src = elem.get('SOURCE_TYPE', '')
            if primary == 'y' and addr > 0:
                syms.append({'addr': addr, 'name': name, 'namespace': ns, 'source': src})
                if ns and 'Global' not in ns: namespaces.add(ns)
            elem.clear()
    funcs.sort(key=lambda f: f['addr'])
    syms.sort(key=lambda s: s['addr'])
    return {'functions': funcs, 'symbols': syms, 'namespaces': sorted(namespaces)}


# ============================================================
# Main CLI
# ============================================================

def main():
    p = argparse.ArgumentParser(
        description='Asura Engine Tool — The Simpsons Game (Wii)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s info PROTO08_shad.wii FINAL08_SHAD.enBE
  %(prog)s extract PROTO08_shad.wii -o raw/
  %(prog)s textures PROTO08_shad.wii -o textures/
  %(prog)s textures FINAL08_SHAD.wii --no-palette -o raw_tex/
  %(prog)s models PROTO08_shad.wii -o models/
  %(prog)s dialogue FINAL08_SHAD.enBE
  %(prog)s audio FINAL08_SHAD.enBE -o audio/
  %(prog)s script PROTO08_shad.wii -o scripts/
  %(prog)s env PROTO08_shad.wii -o levels/
  %(prog)s text Goals_En.asrBE ProtoMenu_En.asrBE -o text/
""")
    sub = p.add_subparsers(dest='command', required=True)
    
    sp = sub.add_parser('info', help='Container summary')
    sp.add_argument('input', nargs='+')
    
    sp = sub.add_parser('extract', help='Extract raw assets')
    sp.add_argument('input', nargs='+')
    sp.add_argument('-o', '--output')
    
    sp = sub.add_parser('textures', help='Convert textures to PNG')
    sp.add_argument('input', nargs='+')
    sp.add_argument('-o', '--output')
    sp.add_argument('--no-palette', action='store_true', help='Skip Simpsons palette colorization')
    sp.add_argument('-q', '--quiet', action='store_true')
    
    sp = sub.add_parser('models', help='Convert models to OBJ')
    sp.add_argument('input', nargs='+')
    sp.add_argument('-o', '--output')
    sp.add_argument('-q', '--quiet', action='store_true')
    
    sp = sub.add_parser('dialogue', help='Extract dialogue CSV')
    sp.add_argument('input', nargs='+')
    sp.add_argument('-o', '--output')
    
    sp = sub.add_parser('audio', help='Extract audio clips')
    sp.add_argument('input', nargs='+')
    sp.add_argument('-o', '--output')
    sp.add_argument('--wav', action='store_true', help='Convert DSP ADPCM to WAV (proto files only)')
    
    sp = sub.add_parser('script', help='Decode GSMS scripts')
    sp.add_argument('input', nargs='+')
    sp.add_argument('-o', '--output')
    
    sp = sub.add_parser('env', help='Extract level geometry (StrippedEnv) as OBJ')
    sp.add_argument('input', nargs='+')
    sp.add_argument('-o', '--output')
    
    sp = sub.add_parser('text', help='Extract TXTH localised text to CSV')
    sp.add_argument('input', nargs='+')
    sp.add_argument('-o', '--output')
    sp.add_argument('-q', '--quiet', action='store_true')
    
    args = p.parse_args()
    cmds = {'info':cmd_info,'extract':cmd_extract,'textures':cmd_textures,
            'models':cmd_models,'dialogue':cmd_dialogue,'audio':cmd_audio,
            'script':cmd_script,'env':cmd_env,'text':cmd_text}
    cmds[args.command](args)

if __name__ == '__main__':
    main()


def get_animation_frame_data(mesh, skeleton, animation, frame_t, vertex_weights=None, part_meshes=None):
    """Compute complete animation frame data for rendering.
    
    Returns dict with:
        'skinned_verts': [(x,y,z), ...] — deformed mesh positions (Y/Z negated for display)
        'triangles': [(i0,i1,i2), ...] — triangle indices
        'bone_pos': [(x,y,z), ...] — bone world positions (Y/Z negated)
        'bone_links': [(parent_idx, child_idx), ...] — bone hierarchy links
        'parts': [{'verts': [...], 'tris': [...]}, ...] — split part geometry
    """
    GQR = 1.0 / 1024.0
    
    # Compute skinned mesh
    if vertex_weights is None:
        vertex_weights = parse_bone_weights(mesh)
    
    skinned = skin_character_mesh(mesh, skeleton, animation, frame_t, vertex_weights)
    
    # Get bone positions + links
    bone_pos, bone_links = get_animation_bone_positions(skeleton, animation, frame_t)
    
    # Get triangles from mesh
    triangles = mesh.get('triangles', [])
    
    # Process split parts (static geometry, not animated)
    parts = []
    if part_meshes:
        for pm in part_meshes:
            pv = pm.get('verts', [])
            pt = pm.get('tris', [])
            if pv and pt:
                parts.append({'verts': pv, 'tris': pt})
    
    return {
        'skinned_verts': skinned,
        'triangles': triangles,
        'bone_pos': bone_pos,
        'bone_links': bone_links,
        'parts': parts,
    }
