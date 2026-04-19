"""
tsg_newgen — The Simpsons Game (PS3/Xbox 360) asset extraction toolkit

Parses EA Redwood Shores containers for the new-gen (7th gen) version:
  .str   — SToc stream archives (with RefPack/dk2 decompression)
  .sbk   — EA sound banks (sbnk/sdat format)
  .snu   — EA audio streams (SNR/SNS EAAC)
  .msb   — Music/sound banks (MPF+MUS interactive music)

The new-gen version uses a completely different engine from the old-gen Asura builds.
All data is big-endian (PS3/PowerPC) or little-endian (Xbox 360).
"""

import struct, os, sys, io

# ============================================================
# RefPack (dk2/dklibs) Decompression
# ============================================================
# EA's LZ77-variant compression. Identified by 0x10FB signature.
# Used inside STR archives for section-level compression.

def refpack_decompress(data):
    """Decompress RefPack/dk2 compressed data. Returns decompressed bytes."""
    if isinstance(data, (bytes, bytearray)):
        stream = io.BytesIO(data)
    else:
        stream = data

    header = (stream.read(1)[0] << 8) | stream.read(1)[0]
    if (header & 0x1FFF) != 0x10FB:
        raise ValueError(f"Not RefPack data (header={header:#06x})")

    is_long = (header & 0x8000) != 0

    if is_long:
        b = stream.read(4)
        uncompressed_size = (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]
    else:
        b = stream.read(3)
        uncompressed_size = (b[0] << 16) | (b[1] << 8) | b[2]

    out = bytearray(uncompressed_size)
    offset = 0

    while True:
        prefix = stream.read(1)
        if not prefix:
            break
        prefix = prefix[0]

        plain_size = 0
        copy_size = 0
        copy_offset = 0
        stop = False

        if prefix < 0x80:
            extra = stream.read(1)[0]
            plain_size = prefix & 0x03
            copy_size = ((prefix & 0x1C) >> 2) + 3
            copy_offset = ((prefix & 0x60) << 3 | extra) + 1

        elif prefix < 0xC0:
            extra = stream.read(2)
            plain_size = extra[0] >> 6
            copy_size = (prefix & 0x3F) + 4
            copy_offset = ((extra[0] & 0x3F) << 8 | extra[1]) + 1

        elif prefix < 0xE0:
            extra = stream.read(3)
            plain_size = prefix & 3
            copy_size = ((prefix & 0x0C) << 6 | extra[2]) + 5
            copy_offset = (((prefix & 0x10) << 4 | extra[0]) << 8 | extra[1]) + 1

        elif prefix < 0xFC:
            plain_size = ((prefix & 0x1F) + 1) * 4

        else:
            plain_size = prefix & 3
            stop = True

        if plain_size > 0:
            out[offset:offset + plain_size] = stream.read(plain_size)
            offset += plain_size

        if copy_size > 0:
            src = offset - copy_offset
            for i in range(copy_size):
                out[offset + i] = out[src + i]
            offset += copy_size

        if stop:
            break

    return bytes(out)


# ============================================================
# SDBM Hash (New-Gen)
# ============================================================
# Different from old-gen's mul-31. Used for entity type IDs.

def sdbm_hash(s):
    """Compute SDBM hash of a string (lowercased). Used in new-gen entity system."""
    h = 0
    for ch in s:
        c = ord(ch.lower()) if ord(ch) < 0x80 else ord(ch)
        h = (c + 65599 * h) & 0xFFFFFFFF
    return h


# ============================================================
# STR Container Parser
# ============================================================
# SToc archive format — main container for new-gen game data.
# Contains sections that may be RefPack compressed, each holding
# embedded assets (0x1607) or SimGroups (0x2207).

def _guess_endian_str(data):
    """Detect endianness of an STR file from header fields."""
    if data[:4] == b'SToc':
        return '>'  # PS3 (big-endian)
    elif data[:4] == b'coTS':
        return '<'  # Xbox 360 (little-endian)
    # Fallback: try both
    off_be = struct.unpack_from('>I', data, 16)[0]
    off_le = struct.unpack_from('<I', data, 16)[0]
    if 0x20 <= off_be <= 0x100:
        return '>'
    if 0x20 <= off_le <= 0x100:
        return '<'
    return '>'

def parse_str_header(data):
    """Parse STR (SToc) container header.
    Returns dict with sections and metadata."""
    e = _guess_endian_str(data)
    u32 = lambda d, off: struct.unpack_from(e + 'I', d, off)[0]
    u8 = lambda d, off: d[off]

    magic = data[:4]
    if magic not in (b'SToc', b'coTS'):
        raise ValueError(f"Not an STR file (magic={magic})")

    version = u32(data, 4)
    n_sections = u8(data, 8)
    platform_id = u8(data, 9)
    p_stream_file = u32(data, 12)
    p_section_arr = u32(data, 16)

    # Parse stream file info
    stream_file = None
    if p_stream_file != 0 and p_stream_file + 20 <= len(data):
        sf_guid = u32(data, p_stream_file)
        # Filename is at a pointer offset
        fname_ptr = u32(data, p_stream_file + 4)
        fname = ''
        if fname_ptr != 0:
            fname_off = p_stream_file + 4 + fname_ptr  # relative pointer
            if 0 < fname_off < len(data):
                end = data[fname_off:].find(b'\x00')
                if end > 0:
                    fname = data[fname_off:fname_off + end].decode('ascii', errors='replace')

        n_parents = u8(data, p_stream_file + 8)
        n_force_load = u8(data, p_stream_file + 9)
        sf_n_sections = u8(data, p_stream_file + 10)
        sf_flags = u8(data, p_stream_file + 11)

        stream_file = {
            'guid': sf_guid, 'filename': fname,
            'n_parents': n_parents, 'n_force_load': n_force_load,
            'n_sections': sf_n_sections, 'flags': sf_flags,
        }

    # Parse section array
    sections = []
    off = p_section_arr
    for i in range(n_sections):
        if off + 24 > len(data):
            break
        mem_policy_id = u32(data, off)
        compressor_id = u32(data, off + 4)
        data_size = u32(data, off + 8)
        alloc_size = u32(data, off + 12)
        read_size = u32(data, off + 16)
        read_offset = u32(data, off + 20)
        sections.append({
            'mem_policy_id': mem_policy_id,
            'compressor_id': compressor_id,
            'data_size': data_size,
            'alloc_size': alloc_size,
            'read_size': read_size,
            'read_offset': read_offset,
        })
        off += 24

    # Data starts at 2048-byte aligned offset after header
    data_start = 20 + (n_sections * 24)
    data_start = (data_start + 0x7FF) & ~0x7FF

    return {
        'endian': e,
        'version': version,
        'n_sections': n_sections,
        'platform_id': platform_id,
        'stream_file': stream_file,
        'sections': sections,
        'data_start': data_start,
    }


def _read_rw_string(data, off):
    """Read a length-prefixed string (uint32 BE length + bytes)."""
    if off + 4 > len(data):
        return '', off + 4
    length = struct.unpack_from('>I', data, off)[0]
    off += 4
    if length == 0 or off + length > len(data):
        return '', off + length
    raw = data[off:off + length]
    end = raw.find(b'\x00')
    s = raw[:end].decode('ascii', errors='replace') if end >= 0 else raw.decode('ascii', errors='replace')
    return s, off + length


def _parse_embedded_asset(data, off):
    """Parse an embedded asset chunk (type 0x1607).
    Returns dict with resource info and file bytes, plus new offset."""
    # Skip 2 bytes padding
    off += 2
    file_size = struct.unpack_from('<I', data, off)[0]  # always LE
    off += 4
    off += 4  # skip 4 bytes

    file_start = off
    header_size = struct.unpack_from('>I', data, off)[0]
    off += 4

    if header_size == 0:
        # No header — raw data to end of chunk
        raw_data = data[off:file_start + file_size]
        return {
            'filename': '',
            'guid': None,
            'resource_type': '',
            'full_path': '',
            'data': raw_data,
        }, file_start + file_size

    # Parse header fields
    filename, off = _read_rw_string(data, off)

    # GUID: 16 bytes (4 × uint32 BE)
    if off + 16 <= len(data):
        guid = (
            struct.unpack_from('>I', data, off)[0],
            struct.unpack_from('>I', data, off + 4)[0],
            struct.unpack_from('>I', data, off + 8)[0],
            struct.unpack_from('>I', data, off + 12)[0],
        )
        off += 16
    else:
        guid = None

    resource_type, off = _read_rw_string(data, off)
    full_path, off = _read_rw_string(data, off)

    # Skip 12 bytes + read size
    off += 12
    if off + 4 <= len(data):
        size = struct.unpack_from('>I', data, off)[0]
        off += 4
    else:
        size = 0

    # File data starts at file_start + 4 + header_size
    data_off = file_start + 4 + header_size
    file_data = data[data_off:data_off + size] if data_off + size <= len(data) else b''

    return {
        'filename': filename,
        'guid': guid,
        'resource_type': resource_type,
        'full_path': full_path,
        'data': file_data,
    }, file_start + file_size


def _parse_simgroup_header(data):
    """Parse SimGroup header from raw bytes.
    Returns dict with entity count and metadata."""
    if len(data) < 44:
        return None

    off = 0
    off += 4  # skip 4
    off += 4  # skip 4
    magic = struct.unpack_from('>I', data, off)[0]
    off += 4
    if magic != 0x53696D47:  # 'SimG'
        return None

    version = struct.unpack_from('>I', data, off)[0]; off += 4
    guid = struct.unpack_from('>I', data, off)[0]; off += 4
    flags = struct.unpack_from('>I', data, off)[0]; off += 4
    n_ents = struct.unpack_from('>I', data, off)[0]; off += 4
    n_ents_dispatched = struct.unpack_from('>I', data, off)[0]; off += 4
    shared_data_size = struct.unpack_from('>I', data, off)[0]; off += 4
    dictionary_size = struct.unpack_from('>I', data, off)[0]; off += 4

    return {
        'magic': 'SimG',
        'version': version,
        'guid': guid,
        'flags': flags,
        'n_entities': n_ents,
        'n_entities_dispatched': n_ents_dispatched,
        'shared_data_size': shared_data_size,
        'dictionary_size': dictionary_size,
    }


def extract_str_assets(data):
    """Extract all embedded assets from an STR archive.
    Returns list of dicts: {filename, guid, resource_type, full_path, data, section_idx}"""
    header = parse_str_header(data)
    e = header['endian']
    u32 = lambda d, off: struct.unpack_from(e + 'I', d, off)[0]
    assets = []
    simgroups = []

    read_pos = header['data_start']

    for si, section in enumerate(header['sections']):
        if read_pos >= len(data):
            break

        # Read section data
        raw = data[read_pos:read_pos + section['read_size']]

        # Decompress if RefPack
        if section['compressor_id'] == 0xB9F0B9EC:
            try:
                section_data = refpack_decompress(raw)
            except Exception as ex:
                read_pos += section['read_size']
                continue
        elif section['compressor_id'] == 0x0EAC15C8:
            section_data = raw
        else:
            read_pos += section['read_size']
            continue

        # Parse chunks within section
        off = 0
        while off + 2 < len(section_data):
            # Check for end of meaningful data
            if section_data[off] == 0 and section_data[off + 1] == 0:
                break

            chunk_type = struct.unpack_from('>H', section_data, off)[0]

            if chunk_type == 0x10FB:
                # Nested RefPack compression
                try:
                    decompressed = refpack_decompress(section_data[off:off + section['read_size']])
                    # Recursively parse the decompressed data
                    inner_off = 0
                    while inner_off + 2 < len(decompressed):
                        inner_type = struct.unpack_from('>H', decompressed, inner_off)[0]
                        if inner_type == 0x1607:
                            inner_off += 2
                            asset, inner_off = _parse_embedded_asset(decompressed, inner_off)
                            asset['section_idx'] = si
                            assets.append(asset)
                        elif inner_type == 0x2207:
                            inner_off += 2
                            inner_off += 2  # padding
                            sg_size = struct.unpack_from('<I', decompressed, inner_off)[0]
                            inner_off += 4
                            inner_off += 4  # skip 4
                            sg_data = decompressed[inner_off:inner_off + sg_size]
                            sg_info = _parse_simgroup_header(sg_data)
                            if sg_info:
                                sg_info['section_idx'] = si
                                sg_info['raw_data'] = sg_data
                                simgroups.append(sg_info)
                            inner_off += sg_size
                        else:
                            break
                except:
                    pass
                break

            elif chunk_type == 0x1607:
                off += 2
                asset, off = _parse_embedded_asset(section_data, off)
                asset['section_idx'] = si
                assets.append(asset)

            elif chunk_type == 0x2207:
                off += 2
                off += 2  # padding
                sg_size = struct.unpack_from('<I', section_data, off)[0]
                off += 4
                off += 4  # skip 4
                sg_data = section_data[off:off + sg_size]
                sg_info = _parse_simgroup_header(sg_data)
                if sg_info:
                    sg_info['section_idx'] = si
                    sg_info['raw_data'] = sg_data
                    simgroups.append(sg_info)
                off += sg_size

            elif chunk_type == 0x2307:
                # Embedded Asset (Compact) — not implemented
                break
            else:
                break

        # Advance to next section
        if section['compressor_id'] == 0x0EAC15C8:
            read_pos += section['read_size']
        else:
            read_pos += section['read_size']

    return assets, simgroups, header


# ============================================================
# STR Info / Extract CLI
# ============================================================

def cmd_str_info(path):
    """Print info about an STR archive."""
    data = open(path, 'rb').read()
    header = parse_str_header(data)

    print(f"\n{'='*60}")
    print(f"File: {path} ({len(data):,} bytes)")
    print(f"Format: SToc (EA Stream Archive)")
    print(f"Endian: {'big (PS3)' if header['endian'] == '>' else 'little (Xbox 360)'}")
    print(f"Version: {header['version']}")
    print(f"Platform ID: {header['platform_id']}")
    print(f"Sections: {header['n_sections']}")
    print(f"Data start: {header['data_start']:#x}")

    if header['stream_file']:
        sf = header['stream_file']
        print(f"\nStream file: {sf['filename']!r}")
        print(f"  GUID: {sf['guid']:#010x}")
        print(f"  Parents: {sf['n_parents']}  ForceLoad: {sf['n_force_load']}")

    for i, sec in enumerate(header['sections']):
        comp = 'RefPack' if sec['compressor_id'] == 0xB9F0B9EC else 'none'
        print(f"\nSection [{i}]: data={sec['data_size']:,}  alloc={sec['alloc_size']:,}  "
              f"read={sec['read_size']:,}  compression={comp}")

    # Extract and count assets
    assets, simgroups, _ = extract_str_assets(data)

    if assets:
        from collections import Counter
        type_counts = Counter(a['resource_type'] for a in assets)
        print(f"\nEmbedded assets: {len(assets)}")
        for rtype, cnt in type_counts.most_common():
            print(f"  {rtype or '(unnamed)'}: {cnt}")

    if simgroups:
        total_ents = sum(sg['n_entities'] for sg in simgroups)
        print(f"\nSimGroups: {len(simgroups)} ({total_ents} total entities)")

    # Scan for entity behaviors across all SimGroups
    if simgroups:
        from collections import Counter
        behavior_counts = Counter()
        for sg in simgroups:
            raw = sg.get('raw_data', b'')
            behaviors = scan_simgroup_entities(raw)
            for b in behaviors:
                behavior_counts[b['name']] += 1

        if behavior_counts:
            print(f"\nEntity behaviors identified ({len(behavior_counts)} types):")
            for name, count in behavior_counts.most_common(15):
                print(f"  {name}: {count}")
            if len(behavior_counts) > 15:
                print(f"  ... +{len(behavior_counts) - 15} more")


def cmd_str_extract(path, output=None):
    """Extract all embedded assets from an STR archive."""
    data = open(path, 'rb').read()
    bn = os.path.splitext(os.path.basename(path))[0]
    out = output or bn + '_extract'
    os.makedirs(out, exist_ok=True)

    assets, simgroups, header = extract_str_assets(data)

    count = 0
    for a in assets:
        if not a['data']:
            continue
        # Build output path
        name = a['full_path'] or a['filename'] or a['resource_type']
        if not name:
            if a['guid']:
                name = f"{a['guid'][0]:08X}-{a['guid'][1]:08X}-{a['guid'][2]:08X}-{a['guid'][3]:08X}"
            else:
                name = f"asset_{count:04d}"

        name = name.replace('\\', '/').lstrip('/')
        fpath = os.path.join(out, name)
        os.makedirs(os.path.dirname(fpath) or '.', exist_ok=True)

        with open(fpath, 'wb') as f:
            f.write(a['data'])
        count += 1

    # Also dump SimGroup summaries
    for i, sg in enumerate(simgroups):
        sg_path = os.path.join(out, f"simgroup_{i:03d}_{sg['guid']:08X}.bin")
        with open(sg_path, 'wb') as f:
            f.write(sg.get('raw_data', b''))

    print(f"  {count} assets + {len(simgroups)} SimGroups → {out}/")
    return assets, simgroups


# ============================================================
# EA SBK Sound Bank Parser
# ============================================================
# Format: sbnk header → sdat section → EAAC streams
# Used in The Simpsons Game PS3/360 for sound effects.

def parse_sbk_header(data):
    """Parse an EA SBK soundbank header. Returns metadata dict including
    SDBM hash table for sound event identification."""
    if len(data) < 24:
        return None

    is_be = data[:4] == b'sbnk'
    is_le = data[:4] == b'knbs'
    if not is_be and not is_le:
        return None

    e = '<' if is_be else '>'  # sbnk = LE data, knbs = BE data (opposite of magic)
    u32 = lambda off: struct.unpack_from(e + 'I', data, off)[0]

    n_sounds = u32(0x04)
    sdat_size = u32(0x0C)
    sdat_offset = u32(0x10)

    if sdat_offset + sdat_size > len(data):
        return None

    sdat_magic = data[sdat_offset:sdat_offset + 4]

    result = {
        'endian': e,
        'n_sounds': n_sounds,
        'sdat_offset': sdat_offset,
        'sdat_size': sdat_size,
        'sdat_magic': sdat_magic,
    }

    if sdat_magic in (b'sdat', b'tads'):
        # EAAC format (Simpsons Game, Dead Space)
        total_streams = u32(sdat_offset + 0x04)
        result['total_streams'] = total_streams
        result['format'] = 'eaac'

        # Parse index
        entries = []
        for i in range(total_streams):
            entry_off = sdat_offset + 0x08 + i * 0x10
            if entry_off + 0x10 > len(data):
                break
            stream_idx = u32(entry_off + 0x00)
            marker = u32(entry_off + 0x04)
            stream_off = u32(entry_off + 0x08)
            entries.append({
                'index': stream_idx,
                'marker': marker,
                'offset': sdat_offset + stream_off,
            })
        result['entries'] = entries

    elif sdat_magic in (b'BNKl', b'BNKb'):
        result['format'] = 'bnk'
        result['entries'] = []
    else:
        result['format'] = 'unknown'
        result['entries'] = []

    # Extract SDBM hash table from metadata section
    # At +0x38: count of hash entries, table starts at 0x10604 (24-byte stride)
    # Each hash is a SDBM hash of the sound event's EMX filename
    n_hash_entries = u32(0x38) if len(data) > 0x3C else 0
    sound_hashes = []
    if n_hash_entries > 0 and n_hash_entries < 10000:
        hash_table_off = 0x10604
        for i in range(n_hash_entries):
            off = hash_table_off + i * 24
            if off + 4 > len(data):
                break
            h = u32(off)
            sound_hashes.append(h)
    result['sound_hashes'] = sound_hashes

    return result


# ============================================================
# EA SNU Audio Parser
# ============================================================
# .snu files contain a 16-byte prefix + SNR (header) + SNS (body) data.
# Used for dialogue/voice clips in EARS engine games.
# Format decoded from vgmstream ea_eaac.c + ea_eaac_standard.c.

EAAC_CODECS = {
    0x00: 'NONE', 0x01: 'RESERVED', 0x02: 'PCM16BE', 0x03: 'EAXMA',
    0x04: 'XAS1', 0x05: 'EALayer3_V1', 0x06: 'EALayer3_V2_PCM',
    0x07: 'EALayer3_V2_SPIKE', 0x08: 'GCADPCM', 0x09: 'EASpeex',
    0x0A: 'EATrax', 0x0B: 'EAMP3', 0x0C: 'EAOpus', 0x0D: 'EAATRAC9',
    0x0E: 'EAOpusM', 0x0F: 'EAOpusMU',
}
EAAC_TYPES = {0: 'RAM', 1: 'STREAM', 2: 'GIGASAMPLE'}


def parse_snu_header(data):
    """Parse an EA SNU audio file header with full EAAC decoding.
    
    Returns dict with codec, sample_rate, channels, num_samples, duration,
    and stream offsets for playback.
    """
    if len(data) < 0x18:
        return None

    # SNU prefix: detect endianness from body_offset field
    off_le = struct.unpack_from('<I', data, 0x08)[0]
    off_be = struct.unpack_from('>I', data, 0x08)[0]
    if 0x10 <= off_be < len(data):
        endian = '>'
        body_offset = off_be
    elif 0x10 <= off_le < len(data):
        endian = '<'
        body_offset = off_le
    else:
        return None

    # SNU prefix fields
    sr_hint = data[0]
    snu_flags = data[1]
    channels_hint = data[3]
    some_size = struct.unpack_from(endian + 'I', data, 4)[0]

    # Check if body starts with SPS block header
    is_sps = body_offset < len(data) and data[body_offset] == 0x48

    # SNR header at offset 0x10 (always big-endian)
    header1 = struct.unpack_from('>I', data, 0x10)[0]
    header2 = struct.unpack_from('>I', data, 0x14)[0]

    version      = (header1 >> 28) & 0x0F
    codec        = (header1 >> 24) & 0x0F
    channel_cfg  = (header1 >> 18) & 0x3F
    sample_rate  = (header1 >>  0) & 0x03FFFF
    atype        = (header2 >> 30) & 0x03
    loop_flag    = (header2 >> 29) & 0x01
    num_samples  = (header2 >>  0) & 0x1FFFFFFF

    channels = channel_cfg + 1
    duration = num_samples / sample_rate if sample_rate else 0.0

    result = {
        'endian': endian,
        'version': version,
        'codec': codec,
        'codec_name': EAAC_CODECS.get(codec, f'0x{codec:02X}'),
        'channels': channels,
        'channel_config': channel_cfg,
        'sample_rate': sample_rate,
        'type': EAAC_TYPES.get(atype, f'0x{atype:X}'),
        'loop': bool(loop_flag),
        'num_samples': num_samples,
        'duration': round(duration, 3),
        'snr_offset': 0x10,
        'body_offset': body_offset,
        'is_sps': is_sps,
        'data_size': len(data),
    }

    # Optional loop point
    header_size = 0x08
    if loop_flag:
        header_size += 0x04
        if 0x18 < len(data):
            result['loop_start'] = struct.unpack_from('>i', data, 0x18)[0]

    # Streamed type may have loop offset
    if atype == 1 and loop_flag:
        header_size += 0x04

    result['snr_header_size'] = header_size

    return result


# ============================================================
# SMB — Streaming Media Bank (Dialogue Audio Index)
# ============================================================
# Maps GUIDs to EXA filenames for streaming dialogue/voice clips.
# Each entry is 96 bytes with audio parameters and the EXA path
# that corresponds to .snu files on disk.
#
# Audio pipeline: CHT event → CHA alias → GUID → SMB → EXA → .snu file
#
# Entry layout (96 bytes, big-endian):
#   +0x00: uint16 type (0x000E = dialogue)
#   +0x02: uint16 flags
#   +0x04: uint32 guid_prefix (shared per bank, e.g. A1B3C062)
#   +0x08: uint32 guid_suffix (unique per clip)
#   +0x0C: uint32 guid_suffix (duplicate)
#   +0x10: uint32 guid_suffix (triplicate)
#   +0x14: float  vol_near (dB, typically -2.0)
#   +0x18: float  vol_mid  (typically 20.0)
#   +0x1C: float  vol_far  (typically 100.0)
#   +0x20: 8 bytes padding
#   +0x28: uint32 some_count
#   +0x2C: uint32 hash_1
#   +0x30: uint32 hash_2
#   +0x34: char[44] exa_filename (null-terminated, e.g. "d_apux_xxx_00047d5.exa")

VOICE_CODE_NAMES = {
    'hmrx': 'Homer', 'brtx': 'Bart', 'lisa': 'Lisa', 'marg': 'Marge',
    'apux': 'Apu', 'barn': 'Barney', 'carl': 'Carl', 'clet': 'Cletus',
    'frin': 'Frink', 'grmp': 'Grampa', 'jany': 'Janey', 'jhib': 'Hibbert',
    'kbmn': 'Comic Book Guy', 'krab': 'Krabappel', 'krus': 'Krusty',
    'ljoy': 'Lovejoy', 'lnny': 'Lenny', 'mccl': 'McCann', 'moes': 'Moe',
    'nedf': 'Ned', 'otto': 'Otto', 'ptty': 'Patty/Selma', 'slma': 'Selma',
    'snak': 'Snake', 'wili': 'Willie', 'sknn': 'Skinner', 'wigg': 'Wiggum',
    'smth': 'Smithers', 'brns': 'Burns', 'ralp': 'Ralph', 'miln': 'Milhouse',
    'nels': 'Nelson', 'todd': 'Todd', 'rdtd': 'Rod/Todd',
    'jimb': 'Jimbo', 'dph_': 'Dolphin/Frink',
}


def parse_smb(data):
    """Parse SMB streaming media bank.
    
    Returns dict with entries containing GUID, EXA filename, voice code,
    and audio parameters for each streaming dialogue clip.
    """
    if len(data) < 0x18:
        return None

    # Header
    field_08 = struct.unpack_from('>I', data, 0x08)[0]  # n_groups
    field_0c = struct.unpack_from('>I', data, 0x0C)[0]  # n_sections
    offset_1 = struct.unpack_from('>I', data, 0x10)[0]  # section 2 offset
    offset_2 = struct.unpack_from('>I', data, 0x14)[0]  # offset table start

    # Read offset table starting at offset_2
    entry_offsets = []
    pos = offset_2
    while pos + 4 <= len(data):
        off = struct.unpack_from('>I', data, pos)[0]
        if off < 0x100 or off >= len(data):
            break
        entry_offsets.append(off)
        pos += 4

    # Parse entries (96 bytes each)
    entries = []
    voice_summary = {}

    for off in entry_offsets:
        if off + 0x60 > len(data):
            continue

        entry_type = struct.unpack_from('>H', data, off)[0]
        entry_flags = struct.unpack_from('>H', data, off + 2)[0]
        guid_prefix = struct.unpack_from('>I', data, off + 4)[0]
        guid_suffix = struct.unpack_from('>I', data, off + 8)[0]

        vol_near = struct.unpack_from('>f', data, off + 0x14)[0]
        vol_mid  = struct.unpack_from('>f', data, off + 0x18)[0]
        vol_far  = struct.unpack_from('>f', data, off + 0x1C)[0]

        # EXA filename
        name_bytes = data[off + 0x34:off + 0x60]
        null_pos = name_bytes.find(b'\x00')
        exa_name = name_bytes[:null_pos].decode('ascii', errors='replace') if null_pos > 0 else ''

        # Parse voice code from filename
        voice_code = ''
        character = ''
        hex_id = ''
        if exa_name.startswith('d_'):
            parts = exa_name.replace('.exa', '').split('_')
            if len(parts) >= 4:
                voice_code = parts[1]
                hex_id = parts[3]
                character = VOICE_CODE_NAMES.get(voice_code, voice_code)

        entry = {
            'type': entry_type,
            'flags': entry_flags,
            'guid': f'{guid_prefix:08X}-{guid_suffix:08X}',
            'guid_prefix': guid_prefix,
            'guid_suffix': guid_suffix,
            'vol_near': round(vol_near, 1),
            'vol_mid': round(vol_mid, 1),
            'vol_far': round(vol_far, 1),
            'exa_name': exa_name,
            'snu_filename': exa_name.replace('.exa', '_exa.snu') if exa_name else '',
            'voice_code': voice_code,
            'character': character,
            'hex_id': hex_id,
        }
        entries.append(entry)

        if character:
            if character not in voice_summary:
                voice_summary[character] = 0
            voice_summary[character] += 1

    return {
        'n_groups': field_08,
        'n_sections': field_0c,
        'n_entries': len(entries),
        'entries': entries,
        'voice_summary': voice_summary,
    }


# ============================================================
# Known Resource Types
# ============================================================
# Combined from Unity viewer, IDA disassembly (TSG E3/Final + Dead Space),
# PDB symbols, Visceral wiki, and STR file extraction.

RESOURCE_TYPES = {
    # Mesh & Textures
    'EARS_MESH':            'EA Mesh — RenderWare RpClump (preinstanced, version-specific vertex buffers)',
    'EARS_ITXD':            'EA Texture Dictionary — Xbox 360 textures (BC1/BC2/BC3/BGRA32)',
    'EARS_RES_ITXD':        'EA Texture Dictionary — alternate identifier (same as EARS_ITXD)',
    'rwID_TEXDICTIONARY':   'RenderWare Texture Dictionary — PS3 textures',
    # Level/Scene
    'BSP':                  'Level spatial partitioning (KdTree v11, WorldAtomic/MaterialMesh/Light tables)',
    'MetaModel':            'Model definition (MMdl v10, links meshes/materials/LOD/states/parts)',
    'MM_InstanceLightingData': 'Per-instance lighting data for MetaModel',
    'LightTOC':             'Light Table of Contents',
    'OCC':                  'Occlusion culling data (OccluderListSet)',
    'StreamTOC':            'Stream Table of Contents — level streaming index',
    # Navigation & AI
    'GRAPH':                'Navigation graph v15 — AI pathfinding (nodes, connections, R-tree, properties)',
    'COVERNODETABLE':       'AI cover node positions',
    # Animation & Character
    'BNK':                  'Motion Bank v9 — character animation data (ChrCntl_MotionBankHeader_s)',
    'RCB':                  'Character Info v23 — character controller data (ChrCntl_ChrInfo_s)',
    'BBN':                  'Bone Bindings — skeleton bone name/index mappings',
    # Audio
    'SBK':                  'EA Soundbank (sbnk/sdat EAAC format)',
    'SNU':                  'EA Audio Stream (SNR/SNS)',
    'MSX':                  'Music Project — MPF+MUS music system',
    'SMB':                  'Streaming Media Bank',
    'AMX':                  'Audio Mix — global audio configuration',
    'FX':                   'Audio Effects',
    'AUC':                  'Audio Config',
    'GNX':                  'Audio Generics',
    'MIX':                  'Audio Mix',
    # Physics
    'HKO':                  'Havok Collision Objects — dynamic collision shapes (Havok 4.10)',
    'HKT':                  'Havok Collision Terrain — static terrain collision',
    'HKX':                  'Havok Extra Data',
    'HKA':                  'Havok Animation',
    'SHK':                  'Shock Profile — physics response profiles',
    # NPC & Dialogue
    'CHA':                  'Chatter Alias Bank — NPC dialogue groupings',
    'CHT':                  'Chatter Data — NPC dialogue content tables',
    'RCM':                  'Voice/Audio Script Config — character voice attenuation params',
    'ACS':                  'Attack Collision Shapes — combat hit/hurt volumes',
    # UI & Text
    'UIE':                  'UI Element — EA APT UI component',
    'UIX':                  'UI XML — EA APT UI layout (magic "uixf")',
    'UIC':                  'UI CSV — UI data tables',
    'TOB':                  'Text Overlay — subtitle/text overlay timing (magic "1BOT")',
    'LH2':                  'LocoHasho2 — localization strings (magic "2HCL", SDBM hash lookup)',
    'LHR':                  'LocoHash Reference — localization file reference',
    'TEXT':                  'Text/string resources',
    'FFN':                  'Font data',
    'FFN-LR':               'Font localization reference',
    # Scripting
    'LUA':                  'Lua source script',
    'LUAC':                 'Compiled Lua bytecode',
    'VariableDictionary':   'Script variable definitions',
    # Gameplay
    'CEC':                  'Controller Event Config — input/controller mappings',
    'ISM':                  'Impact Surface Matrix — material collision response table',
    'VFX':                  'Visual effects definitions (Alchemy particle system)',
    'BST':                  'Stream Manager resource (SMResourceHandler)',
    'TRINITY_SEQ_MASTER':   'Cinematic sequence master — IGC camera/animation',
    'HUD':                  'HUD resource',
    'GRT':                  'Gender Recognition — NPC voice gender assignment',
    'RMS':                  'Unknown (TSG-specific)',
    'Score':                'Score tracking resource',
    'SimGroup':             'Entity group — contains entity packets (SimG header)',
}

# SDBM hashes for resource type identification
# Used internally by the EARS engine to identify resource types
RESOURCE_TYPE_HASHES = {sdbm_hash(k): k for k in RESOURCE_TYPES}

# Entity type hashes (SDBM: hash = 65599 * hash + char, lowercased)
# Complete table extracted from Final build's sub_82704008 registrations (166 types)
# Verified against real SimGroup data from mob_rules.str and story_mode_design.str
ENTITY_TYPES = {
    0x7BE194EE: 'AndEventGate',       0xAE986323: 'Animated',
    0x913EA984: 'AnimatedShaderController', 0xCF929B52: 'Attacher',
    0xC17EB9A2: 'BartPlayer',         0xFFD2E5B1: 'Base',
    0x883FB8EF: 'BoomOperator',       0xB70C43C8: 'BoundingVolumeCameraModifierInfo',
    0xC20454A2: 'BusStop',            0x66F692B3: 'CameraInfo',
    0x34109553: 'CameraTrigger',      0xB6912FFB: 'ChatterAssetSet',
    0x94D8B526: 'ChatterAssetSetAdd', 0x5D7D089F: 'ChatterAssetSetRemove',
    0xF90ED1F9: 'ChatterGlobalCullingDistance', 0xDC24DB08: 'Checkpoint',
    0xC0519B92: 'CollectObjectsTuningRelay', 0x8B37409C: 'Collectible',
    0x7BA9FD12: 'CombatKnowledgeTuningRelay', 0xE4223E3F: 'ConversationPool',
    0xB251A37C: 'Counter',            0xB3CF3689: 'CrowdAudio',
    0xB390B11A: 'CSystemCommands',    0xD1FFB86C: 'DamageableProp',
    0xBCCB519D: 'DeathSequencer',     0x0A7039C8: 'Destructible',
    0xE6BE6056: 'DestructibleDynamicObject', 0x6600EFC4: 'DissolvePlatform',
    0x63C15F4E: 'Door',               0xB81224B8: 'DynamicObjectAudio',
    0x4F3368D0: 'DynamicObjectBehavior', 0x798AAF5A: 'DynamicObjectTrigger',
    0xC8C5D222: 'EnterExitTrigger',   0x38523FC3: 'Entity',
    0xB1CF0B4B: 'EntityDamageDealer', 0x5B9528A8: 'EntityDensityManager',
    0xDCBD521B: 'EntityFilter',       0x63C72601: 'EntityList',
    0x3E438286: 'EntityMeter',        0xB12EA814: 'EpisodeComplete',
    0x3EACFCDB: 'EpisodeLauncher',    0xCA5CCA5B: 'EquipCommand',
    0x87B7A547: 'EventText',          0x92F62833: 'ExecuteVFX',
    0xC6BE7A71: 'ExplodingItem',      0x70D8CD11: 'FXObject',
    0x34DE7FF9: 'FXObjectHandle',     0xF941297A: 'FadeScreenFx',
    0xB88A62D3: 'FleeArea',           0x34874396: 'Fogger',
    0xCB4B560F: 'FollowTargetRelay',  0xC0CF00BE: 'Food',
    0xDB5F2D3E: 'FreelookCameraInfo', 0x55D64C24: 'Fulcrum',
    0x2414ADFA: 'FuncConveyorBelt',   0x8F8D3C7D: 'FuncMover',
    0xE026FA0B: 'FuncPusher',         0xA018EA5F: 'FuncRotate',
    0x4B590617: 'FuncSpawn',          0x332D5A20: 'Gun',                0xCDB843CF: 'GraphMoverController',
    0x4BF5E8DE: 'GrappleSurface',     0xB4609B99: 'GummiFood',
    0x8FE80BDA: 'GummiObject',        0xC6737E8D: 'GameFlowManager',
    0x14B359CB: 'HandOfGodChaseCameraInfo',
    0x35D6758C: 'HandOfGodCursor',    0xCE702AF7: 'HandOfGodPort',
    0x16090182: 'HandOfGodTrigger',   0x977CBDB3: 'HeliumPort',
    0x0462307C: 'HoGCollectible',     0xFB841467: 'HoGDestructibleObject',
    0xDD078A1D: 'HoGPuzzleObject',    0x61F31794: 'HomerPlayer',
    0x6701E7A5: 'InGameVideoPlayer',  0x4ECFBE13: 'Item',
    0x18409298: 'ItemPlantPoint',     0xD22E242B: 'LadderSurface',
    0x70C99030: 'LazyUnlockCheck',    0x52AD8052: 'LedgeHangSurface',
    0x1FDA242A: 'LerpCameraInfo',     0x92819E77: 'LetterBoxFx',
    0x05923E4C: 'LisaPlayer',         0x72250A1C: 'LoadAemsModule',
    0x539B225A: 'LoadMusicProject',   0x18D4BB30: 'Maggie',
    0x888E55E3: 'MaggieCameraInfo',   0xA8C5FA79: 'MaggieDeployPoint',
    0x35A6789D: 'MargePlayer',        0x3E3535D1: 'MarketPlaceOffer',
    0xCC0A569F: 'MeshBehavior',       0x882120C4: 'MessageBox',
    0x120F25AA: 'MessageRelay',       0xB1079603: 'MicrophoneManager',
    0xE233DD82: 'MobInteractDamageableProp', 0x263709DE: 'MobInteractDestructible',
    0xC4830018: 'MobInteractNode',    0x979B23C6: 'MobInteractTuningRelay',
    0x1AF3C5F4: 'MobItemRandomizer',  0xB3C7A6ED: 'MobMembershipModifier',
    0x9010B9C1: 'ModalScreenEvent',   0x6DD57E43: 'MultiEventChatterBox',
    0x6DF50074: 'MultiManager',       0x087E3D6E: 'MultiRemoveTarget',
    0x6B61526F: 'MusicControlMessage', 0x18FCBF35: 'MusicEvent',
    0xE041EFE5: 'MusicMixCategoryChange', 0x31879609: 'Narrator',
    0xFE18AA12: 'NPCBase',            0x4D83DB06: 'NPCBerserker',
    0x5B264B93: 'NPCDash',            0x6FD6B250: 'NPCDashEatingContestant',
    0x738CF41E: 'NPCEatingContestant', 0xE6EE1B78: 'NPCGuardTuningRelay',
    0x00CB4767: 'NPCLardLad',         0xD3F4C653: 'NPCMelee',
    0x85CF8871: 'NPCMeleeFollower',   0xE893DE72: 'NPCMeleeGuard',
    0x17B56F7A: 'NPCMeleeRanged',     0x70EA2398: 'NPCMeleeRangedFollower',
    0xE98627B9: 'NPCMobMember',       0x1EB84ABF: 'NPCMobileRangedFloater',
    0xD2FD8889: 'NPCNinja',           0xACC5F988: 'NPCRanged',
    0xE3928D7D: 'NPCRangedGuard',     0xC3C509AA: 'NPCSelmattyLair',
    0x584DAF7B: 'NPCSelmattyShire',   0xBEFEC971: 'NPCShakespeare',
    0xDE9B461E: 'NodeController',     0x589CBC0F: 'ObjectDropRandomizer',
    0xB5ECE86C: 'OfferCheck',         0x9DC66782: 'OrEventGate',
    0x16BFC575: 'PassiveHeadTrackTarget', 0x5674FF63: 'PathfinderMusicControl',
    0x9337B5BD: 'PingPongPlatform',   0x1FB0C602: 'PlayAudioStream',
    0x20615BFB: 'PlayShockProfile',   0xB1A6D45B: 'PlaySound',
    0x383225A1: 'Player',             0x86E0ABDA: 'PlayerBoomMicrophoneOperator',
    0x76B2886D: 'PlayerModeNotify',   0x6C413C27: 'PlayerMusicMessageHandler',
    0xEAC08401: 'PlayerStart',        0x1F7DB522: 'PlayerTuningOverride',
    0x66CB6563: 'PointCameraInfo',    0x0C8A61B5: 'PointSourceMicrophone',
    0x7D017375: 'PoleSurface',        0x14064116: 'PopCamera',
    0xF2BA83F8: 'PopulationControlSpawner', 0x470093E9: 'Projectile',
    0xA46B459F: 'PushCamera',         0x54F4E44C: 'PushPopCameraPostBlendModifier',
    0x54B9F9D8: 'RPGSpecialAttack',   0xB536D238: 'RPGSystem',
    0x6B729802: 'RenderTunables',     0xA5273626: 'RotatingDoor',
    0xCE502DE7: 'ScoreObjective',     0x31714328: 'ScriptableProp',
    0x8BD0E0EB: 'ScriptedSequence',   0x6796E9D0: 'SendScoreEvent',
    0xE284BC48: 'SentientMutator',    0x48AF91A8: 'SetAudioMix',
    0xC399E229: 'SetDefaultImpactTable', 0xAE4C08F0: 'SetDspEffect',
    0xAF148344: 'SetFollowTarget',    0x00373688: 'SetMusicEventSuffix',
    0x3243A9F0: 'ShakeCameraModifierInfo', 0x753BF74F: 'SimpleChaseCameraInfo',
    0xEE0EB534: 'SimpleChatterBox',   0x41D92F27: 'SimpleObjective',
    0x0C255DD: 'SimpleTeleport',      0x376E77F4: 'SingleEventChatterBox',
    0x5B7EDE60: 'SkyboxRender',       0x463FD53C: 'SlidingDoor',
    0x7165AF5B: 'SpaceInvader',       0xD87A6D55: 'SpinPlatform',
    0xAD36EFE4: 'SplineCameraInfo',   0x2EE14270: 'StreamInterior',
    0x9C326942: 'StreamSet',          0xD1D23BDC: 'StreamWatcher',
    0xD79D9654: 'Switch',             0x667CD3CA: 'TestEntityDistance',
    0x5FEF7F11: 'TestEntityExists',   0x739D2757: 'TestLastDamage',
    0xCC6D6B17: 'TestPlayerInput',    0xF776AA00: 'TestScore',
    0xFBA585D3: 'TestVariableContainer', 0x906E602C: 'TextMenu',
    0x4C16F745: 'ThoughtBubble',      0x12785E05: 'Timer',
    0xD961EF85: 'TouchDetector',      0xEFB44A94: 'TouchDetectorHurt',
    0xD345A6AA: 'TrackingCameraInfo', 0x52ACCC3D: 'Trampoline',
    0xACBDFE47: 'TriggerAuto',        0x0B251A33: 'TriggerBox',
    0xD16A98A9: 'TriggerBase',        0xF26BB307: 'TriggerHurt',
    0x62EBE09B: 'TriggerRandom',      0x30431D5E: 'TrinityGameSequence',
    0x198D805B: 'TuningOverrideCollectible', 0xD10A7B19: 'TuningOverrideEvent',
    0x07B471D0: 'Turret',             0x50CE1857: 'UIAnimated',
    0x4F194024: 'UnlockCheck',        0x05919766: 'Updraft',
    0xA0D75880: 'VariableCollectible', 0xC7BECB2F: 'VariableCombiner',
    0x2224A569: 'VariableCompare',    0x5EE8CE40: 'VariableOperator',
    0x5555E170: 'VariableSwitch',     0x29155EC0: 'VariableWatcher',
    0xBE33B5FC: 'WireGrabSurface',    0xA206299C: 'ZoneMeshEntity',
    0x77A210A2: 'ZoneRender',         0x0CC92646: 'ZoneWorld',
    0x862623C0: 'DebugText',          0x86788021: 'WayPoint',
    # Dead Space specific (from PDB)
    0x71BC20FF: 'DeadSpaceNPC',       0x8C8FA4A1: 'PlasmaCutter',
}

# Vertex element types (from PDB: EARS::Shader::VertexDecl::VertexElementType)
VERTEX_ELEMENT_TYPES = {
    'VE_TYPE_FLOAT1':  'float32 × 1',
    'VE_TYPE_FLOAT2':  'float32 × 2 (UVs)',
    'VE_TYPE_FLOAT3':  'float32 × 3 (positions, normals)',
    'VE_TYPE_FLOAT4':  'float32 × 4',
    'VE_TYPE_HALF2':   'float16 × 2',
    'VE_TYPE_HALF4':   'float16 × 4',
    'VE_TYPE_UBYTE4':  'uint8 × 4 (colors, bone indices)',
    'VE_TYPE_DEC3N':   'packed 10-10-10-2 normal',
    'VE_TYPE_COLOR':   'RGBA color',
    'VE_TYPE_BSPOS':   'blend-shape position',
    'VE_TYPE_UNUSED':  'unused/padding',
}

# Shader name → SDBM hash mapping (from Unity viewer MeshLoader.cs)
SHADER_HASHES = {}
_SHADER_NAMES = [
    'simpsons_chocolate', 'simpsons_vfx_rigid_textured',
    'simpsons_rigid_normalmap', 'simpsons_rigid_multitone',
    'simpsons_projtex', 'simpsons_rigid_dualtextured_uv',
    'simpsons_skin_dualtextured_uv', 'simpsons_uv',
    'simpsons_skin_flipbook', 'simpsons_flipbook',
    'simpsons_sky', 'simpsons_skin_gloss',
    'simpsons_rigid_gloss', 'simpsons_rigid_dualtextured',
    'simpsons_skin_dualtextured', 'simpsons_rigid_textured',
    'simpsons_skin_textured', 'simpsons_aa_col',
    'simpsons_aa_row', 'simpsons_edgeAA',
    'simpsons_aa', 'simpsons_edge',
    'simpsons_rigid', 'simpsons_skin',
]
for _name in _SHADER_NAMES:
    SHADER_HASHES[sdbm_hash(_name)] = _name

# ============================================================
# Binary Format Version Constants (from DS IDA + TSG IDA)
# ============================================================
# These version numbers are checked at load time by the engine.

FORMAT_VERSIONS = {
    'MetaModel':    {'TSG': 0x0A, 'DS': 0x0A},   # MMdl header version
    'SimGroup':     {'TSG': 1,    'DS': 1},        # SimG header version
    'BSP_KdTree':   {'TSG': 11,   'DS': 11},       # KdTree spatial data
    'NavGraph':     {'TSG': None, 'DS': 15},        # Navigation graph (TSG version TBD)
    'BNK_MotionBank': {'TSG': None, 'DS': 9},       # ChrCntl_MotionBankHeader_s
    'RCB_CharInfo':   {'TSG': None, 'DS': 23},      # ChrCntl_ChrInfo_s
    'STR_Container':  {'TSG': 7,  'DS': 7},         # SToc header version
}

# RWS Stream Chunk Type IDs (from DS IDA SimManager)
RWS_CHUNK_TYPES = {
    0x0700: 'Reset',                # MainLoop::Reset
    0x0704: 'Unknown_704',
    0x0705: 'Unknown_705',
    0x070B: 'StartSystem',          # MainLoop::StartSystem
    0x070C: 'StopSystem',           # MainLoop::StopSystem
    0x070D: 'Unknown_70D',
    0x070E: 'Unknown_70E',
    0x071B: 'Unknown_71B',
    0x071D: 'Init',                 # MainLoop::Init
    0x0720: 'UpdateEntityAttributes',
    0x0721: 'CompactEntData',       # SimManager::RegCompactEntData
    0x0722: 'SimGroup',             # SimManager::LoadSimGroupResource
    0x1607: 'Resource',             # Embedded asset (no compression)
    0x2207: 'SimGroupData',         # Entity data blob (in STR sections)
}


# ============================================================
# MetaModel Parser (MMdl)
# ============================================================

def parse_metamodel(data):
    """Parse a MetaModel (MMdl v10) resource.
    
    Complete struct layout from Unity viewer (TSGFileViewer) + DS IDA:
    Header (88 bytes / 0x58):
      +0x00: "MMdl" magic
      +0x04: uint32 version (10)
      +0x08: uint32 size
      +0x0C: uint32 flags
      +0x10: Guid128 (16 bytes)
      +0x20: uint32 m_uName (SDBM hash)
      +0x24: uint32 m_uPath (SDBM hash)
      +0x28: uint32 m_uSourcePath (SDBM hash)
      +0x2C: int32 pAssets offset
      +0x30: int32 pStates offset
      +0x34: int32 pVariables offset
      +0x38: int32 pAttributes offset
      +0x3C: int32 pPredicates offset
      +0x40: int32 pParts offset
      +0x44: int32 pAttrData offset
      +0x48: int32 pUserData offset
      +0x4C: uint16 nAssets
      +0x4E: uint16 nStates
      +0x50: uint16 nVariables
      +0x52: uint16 nAttributes
      +0x54: uint16 nPredicates
      +0x56: uint16 nParts
    
    MM_Asset (32 bytes): uName(4) + uPath(4) + typeID(4) + GUID(16) + pUserData(4)
    MM_ValueType: BOOL=1, UINT32=2, FLOAT=4, STRING=8, ASSET=0x10, MATRIX=0x20
    MM_ObjectType: STATE=1, SIMPLE_PART=2, METAMODEL_PART=4, ENTITY_PART=8
    """
    if len(data) < 0x58 or data[:4] != b'MMdl':
        return None

    version = struct.unpack_from('>I', data, 4)[0]
    if version != 10:
        return {'magic': 'MMdl', 'version': version, 'error': f'unsupported version {version}'}

    result = {
        'magic': 'MMdl',
        'version': version,
        'size': struct.unpack_from('>I', data, 8)[0],
        'flags': struct.unpack_from('>I', data, 12)[0],
        'guid': tuple(struct.unpack_from('>IIII', data, 0x10)),
        'name_hash': struct.unpack_from('>I', data, 0x20)[0],
        'path_hash': struct.unpack_from('>I', data, 0x24)[0],
        'source_path_hash': struct.unpack_from('>I', data, 0x28)[0],
    }
    result['guid_str'] = '-'.join(f'{g:08X}' for g in result['guid'])

    # Section offsets and counts
    off_assets = struct.unpack_from('>i', data, 0x2C)[0]
    off_states = struct.unpack_from('>i', data, 0x30)[0]
    off_variables = struct.unpack_from('>i', data, 0x34)[0]
    off_attributes = struct.unpack_from('>i', data, 0x38)[0]
    off_predicates = struct.unpack_from('>i', data, 0x3C)[0]
    off_parts = struct.unpack_from('>i', data, 0x40)[0]
    result['attr_data_off'] = struct.unpack_from('>i', data, 0x44)[0]
    result['user_data_off'] = struct.unpack_from('>i', data, 0x48)[0]

    n_assets = struct.unpack_from('>H', data, 0x4C)[0]
    n_states = struct.unpack_from('>H', data, 0x4E)[0]
    n_variables = struct.unpack_from('>H', data, 0x50)[0]
    n_attributes = struct.unpack_from('>H', data, 0x52)[0]
    n_predicates = struct.unpack_from('>H', data, 0x54)[0]
    n_parts = struct.unpack_from('>H', data, 0x56)[0]

    result['counts'] = {
        'assets': n_assets, 'states': n_states, 'variables': n_variables,
        'attributes': n_attributes, 'predicates': n_predicates, 'parts': n_parts,
    }

    # Parse assets (32 bytes each: uName(4) + uPath(4) + typeID(4) + GUID(16) + pUserData(4))
    assets = []
    for i in range(n_assets):
        aoff = off_assets + i * 32
        if aoff + 32 > len(data):
            break
        type_id = struct.unpack_from('>I', data, aoff + 8)[0]
        asset_guid = tuple(struct.unpack_from('>IIII', data, aoff + 12))
        type_name = RESOURCE_TYPES.get(type_id, f'0x{type_id:08X}')
        assets.append({
            'name_hash': struct.unpack_from('>I', data, aoff)[0],
            'path_hash': struct.unpack_from('>I', data, aoff + 4)[0],
            'type_id': type_id,
            'type_name': type_name,
            'guid': asset_guid,
            'guid_str': '-'.join(f'{g:08X}' for g in asset_guid),
        })
    result['assets'] = assets

    return result


# ============================================================
# SimGroup Entity Scanner
# ============================================================

def scan_simgroup_entities(raw_data):
    """Scan SimGroup raw data for entity behavior hashes.
    Returns list of identified behaviors and their offsets."""
    found = []
    for i in range(0, len(raw_data) - 3, 4):
        val = struct.unpack_from('>I', raw_data, i)[0]
        if val in ENTITY_TYPES:
            found.append({
                'offset': i,
                'hash': val,
                'name': ENTITY_TYPES[val],
            })
    return found


def parse_simgroup_entities(raw_data, n_entities):
    """Parse SimGroup entity packets using the known binary format.
    
    Entity packet layout (all big-endian):
        +0x00: 8 bytes  - padding (zeros)
        +0x08: 1 byte   - behavior_count
        +0x09: 1 byte   - flags
        +0x0A: 1 byte   - unknown
        +0x0B: 1 byte   - reference_count
        +0x0C: 4 bytes  - unknown
        +0x10: 4 bytes  - name_ref (relative offset into shared/dict data)
        +0x14: 4 bytes  - BEHAVIOR HASH (entity type / class)
        +0x18+: N × 4 bytes - attribute refs
        trailing: 8 bytes zeros
    
    Returns list of parsed entity info dicts.
    """
    # Find the SimG header
    simG_pos = raw_data.find(b'SimG')
    if simG_pos < 0:
        return []
    
    # Read header offsets
    offset_table_start = struct.unpack_from('>I', raw_data, simG_pos + 32)[0]
    
    entities = []
    for i in range(n_entities):
        ptr_off = simG_pos + offset_table_start + i * 4
        if ptr_off + 4 > len(raw_data):
            break
        entity_off = struct.unpack_from('>I', raw_data, ptr_off)[0]
        abs_off = simG_pos + entity_off
        
        if abs_off + 0x18 > len(raw_data):
            continue
        
        # Calculate entity size from next offset
        if i + 1 < n_entities:
            next_entity_off = struct.unpack_from('>I', raw_data, ptr_off + 4)[0]
            entity_size = next_entity_off - entity_off
        else:
            entity_size = -1  # unknown for last entity
        
        # Parse packet header
        beh_count = raw_data[abs_off + 8]
        flags = raw_data[abs_off + 9]
        unk_byte = raw_data[abs_off + 10]
        ref_count = raw_data[abs_off + 11]
        name_ref = struct.unpack_from('>i', raw_data, abs_off + 0x10)[0]  # signed
        beh_hash = struct.unpack_from('>I', raw_data, abs_off + 0x14)[0]
        
        # Read attribute references
        attr_refs = []
        for j in range(ref_count):
            ref_off = abs_off + 0x18 + j * 4
            if ref_off + 4 > len(raw_data):
                break
            attr_refs.append(struct.unpack_from('>i', raw_data, ref_off)[0])
        
        entity_info = {
            'index': i,
            'offset': entity_off,
            'size': entity_size,
            'behavior_count': beh_count,
            'flags': flags,
            'ref_count': ref_count,
            'name_ref': name_ref,
            'behavior_hash': beh_hash,
            'behavior_name': ENTITY_TYPES.get(beh_hash, f'UNKNOWN_{beh_hash:#010x}'),
            'attr_refs': attr_refs,
        }
        entities.append(entity_info)
    
    return entities


# ============================================================
# LH2 — LocoHasho2 Localization Parser
# ============================================================
# Binary localization format used by the EARS engine. Contains
# SDBM-hashed string IDs with multi-language support.
# Decoded from Dead Space IDA: LocoHasho2_File::Init, FindHashID, GetTextByIndex.
#
# Header (32 bytes):
#   +0x00: "2HCL" magic
#   +0x04: file size (uint32 BE)
#   +0x08: version (bit 31 = UTF-16 wide chars, lower bits = format version)
#   +0x0C: language codes relative offset (0 = no language names)
#   +0x10: n_strings
#   +0x14: n_languages
#   +0x18: runtime pointer (zeroed in file)
#   +0x1C: runtime pointer (zeroed in file)
#
# Data (after header at offset 0x20):
#   hash_ids[n_strings]:     uint32 BE, sorted ascending (for binary search)
#   offsets[n_languages][n_strings]: uint32 BE, absolute offsets to string data
#   string_data:             null-terminated strings (ASCII or UTF-16-BE)

def parse_lh2(data):
    """Parse a LocoHasho2 (LH2) localization resource.
    
    Returns dict with keys:
        n_strings, n_languages, is_wide, version,
        hashes (list of uint32),
        strings (list of lists: strings[lang_idx][str_idx])
    Returns None if data is not a valid LH2 file.
    """
    if len(data) < 0x20 or data[:4] != b'2HCL':
        return None
    
    file_size = struct.unpack_from('>I', data, 4)[0]
    version_raw = struct.unpack_from('>I', data, 8)[0]
    lang_codes_off = struct.unpack_from('>I', data, 12)[0]
    n_strings = struct.unpack_from('>I', data, 16)[0]
    n_languages = struct.unpack_from('>I', data, 20)[0]
    
    is_wide = (version_raw & 0x80000000) != 0
    version = version_raw & 0x7FFFFFFF
    
    if n_strings == 0 or n_strings > 100000 or n_languages > 50:
        return None
    
    # Hash IDs at offset 0x20
    hash_start = 0x20
    hashes = []
    for i in range(n_strings):
        off = hash_start + i * 4
        if off + 4 > len(data):
            break
        hashes.append(struct.unpack_from('>I', data, off)[0])
    
    # Offset table: n_languages × n_strings entries
    offset_start = hash_start + n_strings * 4
    
    strings_by_lang = []
    for lang in range(n_languages):
        lang_strings = []
        for si in range(n_strings):
            off_pos = offset_start + (lang * n_strings + si) * 4
            if off_pos + 4 > len(data):
                lang_strings.append('')
                continue
            str_off = struct.unpack_from('>I', data, off_pos)[0]
            if str_off == 0 or str_off >= len(data):
                lang_strings.append('')
                continue
            
            if is_wide:
                end = data.find(b'\x00\x00', str_off)
                if end > str_off:
                    try:
                        lang_strings.append(data[str_off:end].decode('utf-16-be'))
                    except:
                        lang_strings.append('')
                else:
                    lang_strings.append('')
            else:
                end = data.find(b'\x00', str_off)
                if end > str_off:
                    try:
                        lang_strings.append(data[str_off:end].decode('utf-8'))
                    except:
                        lang_strings.append('')
                else:
                    lang_strings.append('')
        strings_by_lang.append(lang_strings)
    
    # Language code names (if present)
    lang_names = []
    if lang_codes_off > 0 and lang_codes_off < len(data):
        for li in range(n_languages):
            code_off = lang_codes_off + li * 8
            if code_off + 8 <= len(data):
                end = data.find(b'\x00', code_off, code_off + 8)
                if end > code_off:
                    lang_names.append(data[code_off:end].decode('ascii', errors='replace'))
    
    return {
        'n_strings': n_strings,
        'n_languages': n_languages,
        'is_wide': is_wide,
        'version': version,
        'hashes': hashes,
        'strings': strings_by_lang,
        'lang_names': lang_names,
    }


# ============================================================
# EARS_ITXD — Xbox 360 Texture Dictionary Parser
# ============================================================
# Decoded from Unity viewer (TSGFileViewer/EARS_ITXD.cs).
# Xbox 360 texture dictionaries with tiled/swizzled BCn textures.
#
# Header:
#   +0x00: uint16 DeviceID (0x757A = Xbox 360/PS3)
#   +0x02: uint16 Version
#   +0x04: uint16 nDictPlugins
#   +0x06: uint16 nTexPlugins
#   +0x18: uint32 firstTexInfo offset
#   +0x1C: uint32 lastTexInfo offset
#
# Texture entries (256 bytes each, linked list):
#   +0x00: uint32 next, +0x04: uint32 prev
#   +0x08: char[64] TextureName
#   +0x7C: uint32 Width, +0x80: uint32 Height
#   +0xB4: uint32 TexSize, +0xB8: uint32 TexOffset
#   +0xBC: uint32 Format
#
# Format IDs: 0x1A200152=BC1/DXT1, 0x1A200153=BC2/DXT3,
#             0x1A200154=BC3/DXT5, 0x18280186=BGRA32, 0x28000102=A8

ITXD_FORMATS = {
    0x1A200152: 'BC1/DXT1',
    0x1A200153: 'BC2/DXT3',
    0x1A200154: 'BC3/DXT5',
    0x18280186: 'BGRA32',
    0x28000102: 'A8',
}

DEVICE_IDS = {
    0x40EC: 'PS2',
    0x503E: 'Wii/GC',
    0xFECD: 'Xbox',
    0x757A: 'Xbox360/PS3',
}

def parse_ears_itxd(data):
    """Parse an EARS_ITXD texture dictionary resource.
    Returns dict with device_id, version, and list of texture entries."""
    if len(data) < 0x20:
        return None

    device_id = struct.unpack_from('>H', data, 0)[0]
    version = struct.unpack_from('>H', data, 2)[0]
    n_dict_plugins = struct.unpack_from('>H', data, 4)[0]
    n_tex_plugins = struct.unpack_from('>H', data, 6)[0]

    first_off = struct.unpack_from('>I', data, 0x18)[0]
    last_off = struct.unpack_from('>I', data, 0x1C)[0]

    if first_off == 0 or first_off >= len(data):
        return {'device': DEVICE_IDS.get(device_id, f'0x{device_id:04X}'),
                'version': version, 'textures': []}

    count = ((last_off - first_off) // 256) + 1 if last_off >= first_off else 0

    textures = []
    for i in range(count):
        toff = first_off + i * 256
        if toff + 256 > len(data):
            break

        name_bytes = data[toff + 8:toff + 8 + 64]
        end = name_bytes.find(b'\x00')
        name = name_bytes[:end].decode('ascii', errors='replace') if end > 0 else ''

        width = struct.unpack_from('>I', data, toff + 0x7C)[0]
        height = struct.unpack_from('>I', data, toff + 0x80)[0]
        tex_size = struct.unpack_from('>I', data, toff + 0xB4)[0]
        tex_offset = struct.unpack_from('>I', data, toff + 0xB8)[0]
        fmt = struct.unpack_from('>I', data, toff + 0xBC)[0]

        textures.append({
            'name': name,
            'width': width,
            'height': height,
            'size': tex_size,
            'offset': tex_offset,
            'format': fmt,
            'format_name': ITXD_FORMATS.get(fmt, f'0x{fmt:08X}'),
        })

    return {
        'device': DEVICE_IDS.get(device_id, f'0x{device_id:04X}'),
        'version': version,
        'n_textures': len(textures),
        'textures': textures,
    }


# ============================================================
# TOB — Text Overlay File Parser
# ============================================================
# Decoded from Unity viewer (TSGFileViewer/TextOverlayFile.cs).
# Subtitle/text overlay timing data for dialogue sequences.
#
# Header (16 bytes):
#   uint32 ChunkID, uint32 FileSizeBytes, uint32 Version, uint32 NumEntries
# Entries (16 bytes each):
#   uint32 HashID (SDBM), uint32 StartTimeMS, uint32 DurationMS, int32 UserData

def parse_tob(data):
    """Parse a TOB (TextOverlayFile) resource.
    Returns dict with entries list containing hash, timing, and user data."""
    if len(data) < 16:
        return None

    chunk_id = struct.unpack_from('>I', data, 0)[0]
    file_size = struct.unpack_from('>I', data, 4)[0]
    version = struct.unpack_from('>I', data, 8)[0]
    n_entries = struct.unpack_from('>I', data, 12)[0]

    entries = []
    for i in range(n_entries):
        off = 16 + i * 16
        if off + 16 > len(data):
            break
        hash_id = struct.unpack_from('>I', data, off)[0]
        start_ms = struct.unpack_from('>I', data, off + 4)[0]
        duration_ms = struct.unpack_from('>I', data, off + 8)[0]
        user_data = struct.unpack_from('>i', data, off + 12)[0]
        entries.append({
            'hash': hash_id,
            'start_ms': start_ms,
            'duration_ms': duration_ms,
            'user_data': user_data,
        })

    return {
        'chunk_id': chunk_id,
        'version': version,
        'n_entries': len(entries),
        'entries': entries,
    }


# ============================================================
# GRAPH — Navigation/AI Graph Parser
# ============================================================
# Decoded from Unity viewer (TSGFileViewer/Graph.cs).
# Navigation graphs for pathfinding and AI waypoints.
#
# Header (28 bytes):
#   +0x00: 4 bytes skip
#   +0x04: 4 bytes skip
#   +0x08: uint32 version
#   +0x0C: uint16 nNode
#   +0x0E: uint16 nConnection
#   +0x10: Guid128 GraphGuid (16 bytes)
#   +0x20: int32 nodeArrayOffset
#   +0x24: int32 connectionArrayOffset
#
# GraphNode (28 bytes):
#   float pos[3], float radius
#   uint16 iConnection, int16 propertyInstance
#   uint8 nConnection, uint8 nNeighborConnection
#   uint16 iNeighborConnection, int16 nodeFlags, int16 iLightSetID
#   uint8 nExternalConnection, uint16 iUserData, uint8 padding
#
# GraphConnection (16 bytes):
#   float length, int16 dstIndex, int16 srcIndex
#   int16 propertyInstance, int16 connectionFlags
#   uint8 blockedCount, uint8×3 padding

def parse_graph(data):
    """Parse a navigation/AI graph resource.
    Returns dict with nodes and connections arrays."""
    if len(data) < 0x28:
        return None

    version = struct.unpack_from('>I', data, 8)[0]
    n_node = struct.unpack_from('>H', data, 0x0C)[0]
    n_connection = struct.unpack_from('>H', data, 0x0E)[0]
    guid = tuple(struct.unpack_from('>IIII', data, 0x10))
    node_off = struct.unpack_from('>I', data, 0x20)[0]
    conn_off = struct.unpack_from('>I', data, 0x24)[0]

    # Parse nodes (28 bytes each)
    nodes = []
    for i in range(n_node):
        off = node_off + i * 28
        if off + 28 > len(data):
            break
        nodes.append({
            'pos': struct.unpack_from('>fff', data, off),
            'radius': struct.unpack_from('>f', data, off + 12)[0],
            'iConnection': struct.unpack_from('>H', data, off + 16)[0],
            'propertyInstance': struct.unpack_from('>h', data, off + 18)[0],
            'nConnection': data[off + 20],
            'nNeighborConnection': data[off + 21],
            'iNeighborConnection': struct.unpack_from('>H', data, off + 22)[0],
            'nodeFlags': struct.unpack_from('>h', data, off + 24)[0],
        })

    # Parse connections (16 bytes each)
    connections = []
    for i in range(n_connection):
        off = conn_off + i * 16
        if off + 16 > len(data):
            break
        connections.append({
            'length': struct.unpack_from('>f', data, off)[0],
            'dstIndex': struct.unpack_from('>h', data, off + 4)[0],
            'srcIndex': struct.unpack_from('>h', data, off + 6)[0],
            'propertyInstance': struct.unpack_from('>h', data, off + 8)[0],
            'connectionFlags': struct.unpack_from('>h', data, off + 10)[0],
            'blockedCount': data[off + 12],
        })

    return {
        'version': version,
        'guid': guid,
        'guid_str': '-'.join(f'{g:08X}' for g in guid),
        'n_nodes': len(nodes),
        'n_connections': len(connections),
        'nodes': nodes,
        'connections': connections,
    }


# ============================================================
# EARS_MESH — RenderWare Mesh with EA Extensions
# ============================================================
# Decoded from Unity viewer (TSGFileViewer/EARSMesh.cs).
#
# Top-level: standard RenderWare Clump (chunk 0x10) containing
# a Geometry (0x0F) with an Extension (0x03) chain that includes
# the EARSMesh chunk (0xEA33). Standard RW chunks are little-endian
# (12-byte header: ID, size, lib version), but EARSMesh internals
# are big-endian.
#
# EARSMesh chunk layout:
#   +0x00: 20 bytes header
#   +0x14: uint32 dataBlockOffset (BE)
#   +0x18: uint32 dataBlockSize (BE)
#   +0x1C: 4 bytes skip
#   +0x20: uint32 tableEntryCount (BE)
#   +0x24: uint32 submeshCount (BE)
#   +0x28: tableEntries (8 bytes each × tableEntryCount)
#   +...:  SubmeshHeaders (12 bytes each × submeshCount)
#            uint32 skip, uint32 size, uint32 offset

# RW chunk type IDs (LE int32 in section header)
RW_CHUNK_NAMES = {
    0x01: 'Struct',         0x02: 'String',         0x03: 'Extension',
    0x06: 'Texture',        0x07: 'Material',       0x08: 'MaterialList',
    0x0E: 'FrameList',      0x0F: 'Geometry',       0x10: 'Clump',
    0x14: 'Atomic',         0x15: 'TextureNative',  0x16: 'TextureDictionary',
    0x1A: 'GeometryList',   0x1B: 'Animation',      0x50E: 'BinMeshPLG',
    0x510: 'NativeDataPLG', 0x116: 'SkinPLG',       0x11D: 'HAnimPLG',
    0x120: 'RightToRender',
    0xEA01: 'EARSTexture',  0xEA02: 'EARSData',
    0xEA15: 'EARSPlugin1',  0xEA16: 'EARSPlugin2', 0xEA33: 'EARSMesh',
}

# Container chunks that have nested children
RW_CONTAINERS = {0x01, 0x03, 0x10, 0x1A, 0x0F}


def _walk_rw(data):
    """Yield (chunk_id, data_offset, size, payload) tuples for RW sections."""
    off = 0
    while off + 12 <= len(data):
        cid = struct.unpack_from('<I', data, off)[0]
        size = struct.unpack_from('<I', data, off + 4)[0]
        if off + 12 + size > len(data):
            break
        yield (cid, off + 12, size, data[off+12:off+12+size])
        off += 12 + size


def _find_rw_section(data, target_id):
    """Recursively find first RW section with the given chunk ID."""
    for cid, _, _, sd in _walk_rw(data):
        if cid == target_id:
            return sd
        if cid in RW_CONTAINERS:
            r = _find_rw_section(sd, target_id)
            if r is not None:
                return r
    return None


def _find_rw_section_offset(data, target_id, base=0):
    """Recursively find first RW section with given chunk ID, return its absolute offset."""
    for cid, doff, size, sd in _walk_rw(data):
        abs_off = base + doff
        if cid == target_id:
            return abs_off
        if cid in RW_CONTAINERS:
            r = _find_rw_section_offset(sd, target_id, abs_off)
            if r is not None:
                return r
    return None


# ---- D3D9 Vertex Element Decoding (from EARSMesh.cs) ----

# D3DDeclType IDs used by EARS
_D3D_TYPES = {
    0x2A23B9: ('FLOAT3',  12), 0x2C23A5: ('FLOAT2',   8),
    0x2C83A4: ('FLOAT1',   4), 0x1A23A6: ('FLOAT4',  16),
    0x182886: ('D3DCOLOR', 4), 0x1A2286: ('UBYTE4',   4),
    0x1A2086: ('UBYTE4N',  4), 0x1A2386: ('BYTE4',    4),
    0x1A2186: ('BYTE4N',   4), 0x2A2187: ('DEC3N',    4),
    0x2A2387: ('DEC3',     4), 0x2C2359: ('SHORT2',   4),
    0x1A235A: ('SHORT4',   8), 0x2C235F: ('FLOAT16_2',4),
    0xFFFFFFFF: ('UNUSED', 0),
}

# D3DDeclUsage
_D3D_USAGE = {
    0: 'POSITION', 1: 'BLENDWEIGHT', 2: 'BLENDINDICES', 3: 'NORMAL',
    4: 'PSIZE', 5: 'TEXCOORD', 6: 'TANGENT', 7: 'BINORMAL', 10: 'COLOR',
}


def _decode_dec3n(packed):
    """Decode DEC3N: packed 10-10-10-2 signed normalized (XYZ in bits 2-31)."""
    z = (packed >> 22) & 0x3FF
    y = (packed >> 12) & 0x3FF
    x = (packed >>  2) & 0x3FF
    if z & 0x200: z -= 0x400
    if y & 0x200: y -= 0x400
    if x & 0x200: x -= 0x400
    return (x / 511.0, y / 511.0, z / 511.0)


def _read_vertex_element(data, offset, type_id):
    """Read a vertex element value at offset using the D3D type."""
    info = _D3D_TYPES.get(type_id)
    if not info or info[0] == 'UNUSED':
        return None
    name, size = info
    if offset + size > len(data):
        return None
    if name == 'FLOAT3':
        return struct.unpack_from('>3f', data, offset)
    elif name == 'FLOAT2':
        return struct.unpack_from('>2f', data, offset)
    elif name == 'FLOAT1':
        return struct.unpack_from('>f', data, offset)
    elif name == 'FLOAT4':
        return struct.unpack_from('>4f', data, offset)
    elif name == 'D3DCOLOR':
        return tuple(data[offset + i] / 255.0 for i in range(4))
    elif name == 'UBYTE4':
        return tuple(data[offset + i] for i in range(4))
    elif name == 'UBYTE4N':
        return tuple(data[offset + i] / 255.0 for i in range(4))
    elif name in ('DEC3N', 'DEC3'):
        return _decode_dec3n(struct.unpack_from('>I', data, offset)[0])
    elif name == 'SHORT2':
        return struct.unpack_from('>2h', data, offset)
    elif name == 'SHORT4':
        return struct.unpack_from('>4h', data, offset)
    elif name == 'FLOAT16_2':
        # Half-float decode
        import array
        raw = struct.unpack_from('>2H', data, offset)
        def _half_to_float(h):
            s = (h >> 15) & 1; e = (h >> 10) & 0x1F; m = h & 0x3FF
            if e == 0: return (-1)**s * 2**(-14) * (m / 1024.0)
            if e == 31: return float('-inf') if s else float('inf')
            return (-1)**s * 2**(e - 15) * (1.0 + m / 1024.0)
        return tuple(_half_to_float(r) for r in raw)
    return None


def _tristrip_to_triangles(indices):
    """Convert tristrip index list to triangle list. 0xFFFF = strip restart."""
    triangles = []
    strip = []
    for idx in indices:
        if idx == 0xFFFF:
            if len(strip) >= 3:
                _emit_strip(strip, triangles)
            strip = []
        else:
            strip.append(idx)
    if len(strip) >= 3:
        _emit_strip(strip, triangles)
    return triangles


def _emit_strip(strip, out):
    """Convert a single tristrip to triangles with correct winding."""
    flipped = True
    for i in range(len(strip) - 2):
        a, b, c = strip[i], strip[i+1], strip[i+2]
        if a != b and b != c and a != c:  # skip degenerate
            if flipped:
                out.append((c, b, a))
            else:
                out.append((b, c, a))
        flipped = not flipped


def parse_ears_mesh(data):
    """Parse EARS_MESH resource with full vertex/index extraction.
    
    Returns dict with submeshes, each containing:
      positions: list of (x,y,z) float tuples
      normals:   list of (x,y,z) float tuples (if present)
      uvs:       dict of channel→[(u,v)] (if present)
      colors:    list of (r,g,b,a) float tuples (if present)
      triangles: list of (a,b,c) index tuples
      blend_indices: list of (b0,b1,b2,b3) tuples (if skinned)
      blend_weights: list of (w0,w1,w2,w3) tuples (if skinned)
    
    All internal offsets are relative to the EARSMesh section data,
    with a +12 byte base (sub-section RW header).
    Based on Unity viewer EARSMesh.cs (DataInSection mode).
    """
    # Find EARSMesh section data inside the RW tree
    em = _find_rw_section(data, 0xEA33)
    if em is None or len(em) < 0x28:
        return None
    
    # EARSMesh header
    data_block_offset = struct.unpack_from('>I', em, 0x14)[0]
    data_block_size   = struct.unpack_from('>I', em, 0x18)[0]
    table_count       = struct.unpack_from('>I', em, 0x20)[0]
    submesh_count     = struct.unpack_from('>I', em, 0x24)[0]
    
    # +12 base for all internal pointer dereferences (RW sub-header)
    BASE = 12
    
    # Skip table entries (8 bytes each)
    cur = 0x28 + table_count * 8
    
    submeshes = []
    for i in range(submesh_count):
        if cur + 12 > len(em):
            break
        cur += 4  # skip constant
        sm_size   = struct.unpack_from('>I', em, cur)[0]; cur += 4
        sm_offset = struct.unpack_from('>I', em, cur)[0]; cur += 4
        
        sm = {'info_size': sm_size, 'info_offset': sm_offset}
        
        # ---- SubmeshInfo at em[sm_offset + 12] ----
        ofs = sm_offset + BASE
        if ofs + 24 > len(em):
            submeshes.append(sm); continue
        
        version = struct.unpack_from('>I', em, ofs)[0]
        sm['version'] = version
        if version != 0x00030002:
            sm['error'] = f'Unsupported version {version:#x}'
            submeshes.append(sm); continue
        
        sm['shader_hash']   = struct.unpack_from('>I', em, ofs + 4)[0]
        sm['flags']         = struct.unpack_from('>I', em, ofs + 8)[0]
        p_geometry          = struct.unpack_from('>I', em, ofs + 12)[0]
        sm['n_primitives']  = struct.unpack_from('>I', em, ofs + 16)[0]
        p_draw_indexed      = struct.unpack_from('>I', em, ofs + 20)[0]
        
        # ---- Geometry block at em[p_geometry + 12] ----
        g = p_geometry + BASE
        if g + 32 > len(em):
            submeshes.append(sm); continue
        
        sz_vdata = struct.unpack_from('>I', em, g)[0]
        bpv      = struct.unpack_from('>I', em, g + 4)[0]
        n_ve     = struct.unpack_from('>I', em, g + 8)[0]
        p_ve     = struct.unpack_from('>I', em, g + 12)[0]
        p_vdata  = struct.unpack_from('>I', em, g + 16)[0]
        sz_idata = struct.unpack_from('>I', em, g + 20)[0]
        fmt_idx  = struct.unpack_from('>I', em, g + 24)[0]
        p_idata  = struct.unpack_from('>I', em, g + 28)[0]
        
        n_verts = sz_vdata // bpv if bpv else 0
        sm['vertex_count']      = n_verts
        sm['bytes_per_vertex']  = bpv
        sm['size_vertex_data']  = sz_vdata
        sm['size_index_data']   = sz_idata
        
        # ---- Vertex element declarations at em[p_ve + 12] ----
        elements = []
        ve_abs = p_ve + BASE
        for j in range(n_ve):
            o = ve_abs + j * 12
            if o + 12 > len(em):
                break
            etype = struct.unpack_from('>I', em, o + 4)[0]
            if etype == 0xFFFFFFFF:
                break  # end sentinel
            elements.append({
                'stream':    struct.unpack_from('>H', em, o)[0],
                'offset':    struct.unpack_from('>H', em, o + 2)[0],
                'type':      etype,
                'type_name': _D3D_TYPES.get(etype, ('?', 0))[0],
                'method':    em[o + 8],
                'usage':     em[o + 9],
                'usage_name': _D3D_USAGE.get(em[o + 9], f'usage_{em[o+9]}'),
                'usage_idx': em[o + 10],
            })
        sm['vertex_elements'] = elements
        
        # Only stream 0 elements for vertex reading
        active_elements = [e for e in elements if e['stream'] == 0]
        
        # ---- Read vertex data from em[p_vdata + 12] ----
        vdata_abs = p_vdata + BASE
        positions = []
        normals = []
        uvs = [[] for _ in range(4)]
        colors = []
        blend_indices = []
        blend_weights = []
        tangents = []
        
        for vi in range(n_verts):
            voff = vdata_abs + vi * bpv
            if voff + bpv > len(em):
                break
            
            for elem in active_elements:
                eoff = voff + elem['offset']
                val = _read_vertex_element(em, eoff, elem['type'])
                if val is None:
                    continue
                
                usage = elem['usage']
                uidx = elem['usage_idx']
                
                if usage == 0 and uidx == 0:    # POSITION
                    positions.append((val[0], val[1], -val[2]))  # negate Z
                elif usage == 3 and uidx == 0:   # NORMAL
                    normals.append((val[0], val[1], -val[2]))
                elif usage == 6 and uidx == 0:   # TANGENT
                    tangents.append(val[:3])
                elif usage == 5:                 # TEXCOORD
                    if uidx < 4:
                        uvs[uidx].append((val[0], 1.0 - val[1]))
                elif usage == 10 and uidx == 0:  # COLOR
                    colors.append(val)
                elif usage == 2 and uidx == 0:   # BLENDINDICES
                    blend_indices.append(val)
                elif usage == 1 and uidx == 0:   # BLENDWEIGHT
                    blend_weights.append(val)
        
        sm['positions'] = positions
        if normals:   sm['normals'] = normals
        if tangents:  sm['tangents'] = tangents
        if colors:    sm['colors'] = colors
        if blend_indices: sm['blend_indices'] = blend_indices
        if blend_weights: sm['blend_weights'] = blend_weights
        sm['uvs'] = {ch: uvs[ch] for ch in range(4) if uvs[ch]}
        
        # ---- DrawIndexedPrimitive structs at em[p_draw_indexed + 12] ----
        dip_abs = p_draw_indexed + BASE
        draw_prims = []
        for j in range(sm['n_primitives']):
            dp = dip_abs + j * 36
            if dp + 36 > len(em):
                break
            draw_prims.append({
                'atomic_material': struct.unpack_from('>I', em, dp)[0],
                'material_index':  struct.unpack_from('>I', em, dp + 4)[0],
                'geometry_index':  struct.unpack_from('>I', em, dp + 8)[0],
                'primitive_type':  struct.unpack_from('>I', em, dp + 12)[0],
                'base_vertex':     struct.unpack_from('>i', em, dp + 16)[0],
                'start_index':     struct.unpack_from('>I', em, dp + 20)[0],
                'index_count':     struct.unpack_from('>I', em, dp + 24)[0],
                'n_bone_batches':  struct.unpack_from('>I', em, dp + 28)[0],
                'p_bone_batches':  struct.unpack_from('>I', em, dp + 32)[0],
            })
        sm['draw_primitives'] = draw_prims
        
        # ---- Read index data from em[p_idata + 12] ----
        idata_abs = p_idata + BASE
        all_triangles = []
        for dp in draw_prims:
            start = dp['start_index']
            count = dp['index_count']
            indices = []
            for k in range(count):
                idx_off = idata_abs + (start + k) * 2
                if idx_off + 2 > len(em):
                    break
                indices.append(struct.unpack_from('>H', em, idx_off)[0])
            
            tris = _tristrip_to_triangles(indices)
            dp['triangles'] = tris
            all_triangles.extend(tris)
        
        sm['triangles'] = all_triangles
        
        # Bounding sphere (4 floats after geometry header)
        bs_off = g + 32
        if bs_off + 16 <= len(em):
            bsx, bsy, bsz, bsr = struct.unpack_from('>4f', em, bs_off)
            sm['bounding_sphere'] = {'center': (bsx, bsy, bsz), 'radius': bsr}
        
        submeshes.append(sm)
    
    return {
        'data_block_offset': data_block_offset,
        'data_block_size': data_block_size,
        'submesh_count': submesh_count,
        'submeshes': submeshes,
    }


def export_ears_mesh_obj(parsed, filename='mesh'):
    """Export parsed EARS_MESH to OBJ format string.
    
    Returns (obj_text, mtl_text) tuple.
    """
    if not parsed or not parsed.get('submeshes'):
        return None, None
    
    obj_lines = [f'# EARS_MESH export', f'# {filename}', f'mtllib {filename}.mtl', '']
    mtl_lines = [f'# EARS_MESH materials', '']
    
    vert_offset = 1  # OBJ is 1-indexed
    
    for si, sm in enumerate(parsed['submeshes']):
        positions = sm.get('positions', [])
        normals = sm.get('normals', [])
        uvs = sm.get('uvs', {}).get(0, [])
        triangles = sm.get('triangles', [])
        
        if not positions or not triangles:
            continue
        
        group_name = f'{filename}_submesh{si}'
        mat_name = f'material_{si}'
        
        obj_lines.append(f'g {group_name}')
        obj_lines.append(f'usemtl {mat_name}')
        
        for x, y, z in positions:
            obj_lines.append(f'v {x:.6f} {y:.6f} {z:.6f}')
        
        for nx, ny, nz in normals:
            obj_lines.append(f'vn {nx:.6f} {ny:.6f} {nz:.6f}')
        
        for u, v in uvs:
            obj_lines.append(f'vt {u:.6f} {v:.6f}')
        
        has_n = len(normals) == len(positions)
        has_uv = len(uvs) == len(positions)
        
        for a, b, c in triangles:
            a1 = a + vert_offset
            b1 = b + vert_offset
            c1 = c + vert_offset
            if has_uv and has_n:
                obj_lines.append(f'f {a1}/{a1}/{a1} {b1}/{b1}/{b1} {c1}/{c1}/{c1}')
            elif has_n:
                obj_lines.append(f'f {a1}//{a1} {b1}//{b1} {c1}//{c1}')
            elif has_uv:
                obj_lines.append(f'f {a1}/{a1} {b1}/{b1} {c1}/{c1}')
            else:
                obj_lines.append(f'f {a1} {b1} {c1}')
        
        obj_lines.append('')
        vert_offset += len(positions)
        
        # Material entry
        mtl_lines.append(f'newmtl {mat_name}')
        mtl_lines.append(f'Kd 0.8 0.8 0.8')
        mtl_lines.append(f'Ka 0.2 0.2 0.2')
        mtl_lines.append(f'Ks 0.1 0.1 0.1')
        mtl_lines.append(f'# shader_hash: 0x{sm.get("shader_hash", 0):08X}')
        mtl_lines.append('')
    
    return '\n'.join(obj_lines), '\n'.join(mtl_lines)


# ============================================================
# Havok Packfile — HKO/HKT Physics Files
# ============================================================
# Decoded from Unity viewer (HavokReader/) + real HKO/HKT analysis.
#
# TSG wraps standard Havok packfiles in a 16-byte prefix. Inside:
# - 64-byte PackfileHeader (big-endian):
#     2×int32 magic (0x57E0E057 / 0x10C0C010)
#     int32 userTag, int32 fileVersion (usually 4 or 9)
#     byte bytesInPointer, bool littleEndian, bool reusePadding, bool emptyBase
#     int32 numSections, int32 contentsSectionIndex, int32 contentsSectionOffset
#     int32 contentsClassNameSectionIndex, int32 contentsClassNameSectionOffset
#     16-byte contentsVersion string + 8 bytes padding
# - N × 48-byte PackfileSectionHeader:
#     19-byte tag + null
#     int32 absoluteDataStart, int32 localFixupsOffset, int32 globalFixupsOffset
#     int32 virtualFixupsOffset, int32 exportsOffset, int32 importsOffset
#     int32 endOffset
#
# Sections: __classnames__ (class name pool), __data__ (serialized objects),
#           __types__ (type descriptors, often empty in shipping files)
#
# Virtual fixups in __data__ map (dataOffset, _, classNameOffset) → instance,
# terminated by dataOffset == 0xFFFFFFFF.

HAVOK_PACKFILE_MAGIC = 0x57E0E057


# ============================================================
# TRINITY_SEQ_MASTER — Cutscene Sequence Format
# ============================================================
# Decoded from real TRINITY_SEQ_MASTER data (igc_11_folderstream.str)
# + E3 IDA strings (TrinityActor, TrinityCamera, TrinityVfx, TrinityScript).
#
# Layout:
#   seqm header (16 bytes): 'seqm' + uint32 totalSize + uint32 nSequences + uint32 dataOffset
#   padding (0xBB fill to 0x400 boundary)
#   seqb block per sequence:
#     'seqb' + uint32 nTracks + uint32 seqHash
#     transform data (24 bytes: position + scale)
#     sequence name (64 bytes null-padded)
#     track blocks (operations with GUIDs, keyframes, timing)
#
# Track types (from TrinityCreationInterface in IDA):
#   TrinityActor — entity reference + animation
#   TrinityCamera — camera position/rotation keyframes
#   TrinityVfx — visual effect spawning + timing
#   TrinityScript — Lua script callbacks

# ============================================================
# BSP — KD-Tree Level Geometry
# ============================================================
# Decoded from DS IDA (ImportBSPData) + real BSP files.
# Version 11, header with 17 relative-offset pointer fields
# for KD-tree nodes, vertices, triangles, materials, etc.

def parse_bsp(data):
    """Parse BSP level geometry resource. Returns header info + section offsets."""
    if len(data) < 0x70:
        return None
    version = struct.unpack_from('>I', data, 0)[0]
    if version != 11:
        return None
    flags = struct.unpack_from('>I', data, 4)[0]
    n_nodes = struct.unpack_from('>I', data, 8)[0]
    p_nodes = struct.unpack_from('>I', data, 0x0C)[0]
    # Packed counts at +0x14, +0x18, +0x1C
    pk1 = struct.unpack_from('>I', data, 0x14)[0]
    pk2 = struct.unpack_from('>I', data, 0x18)[0]
    pk3 = struct.unpack_from('>I', data, 0x1C)[0]

    # Read all 17 section pointers starting at +0x28
    sections = []
    for i in range(17):
        off = 0x28 + i * 4
        if off + 4 <= len(data):
            sections.append(struct.unpack_from('>I', data, off)[0])

    return {
        'version': version, 'flags': flags,
        'n_nodes': n_nodes, 'p_nodes': p_nodes,
        'n_verts': (pk1 >> 16) & 0xFFFF,
        'n_tris': pk1 & 0xFFFF,
        'n_indices': (pk2 >> 16) & 0xFFFF,
        'sections': sections,
        'data_size': len(data),
    }


# ============================================================
# FFN — Font Description ("FONT" magic)
# ============================================================
# Font metric files. Contains glyph dimensions and character mappings
# but NOT the actual glyph bitmaps (those are in separate textures).

def parse_ffn(data):
    """Parse FFN font resource. Returns basic font info."""
    if len(data) < 0x20 or data[:4] != b'FONT':
        return None
    file_size = struct.unpack_from('>I', data, 4)[0]
    # +0x08: packed version/flags
    # +0x0C: glyph data offset
    # +0x14: n_glyphs
    # +0x18: glyph_table_size
    n_glyphs = struct.unpack_from('>I', data, 0x14)[0]
    glyph_table_size = struct.unpack_from('>I', data, 0x18)[0]
    return {
        'magic': 'FONT',
        'file_size': file_size,
        'n_glyphs': n_glyphs,
        'glyph_table_size': glyph_table_size,
    }


# ============================================================
# VariableDictionary — Global Lua Variables
# ============================================================
# Per-level variable storage accessible from simpsons_gameflow.lua.
# Contains named variables (hash + string) used by game logic.

def parse_variable_dict(data):
    """Parse VariableDictionary resource. Returns list of variable names."""
    if len(data) < 12:
        return None
    n_vars = struct.unpack_from('>I', data, 0)[0]
    n_fields = struct.unpack_from('>I', data, 4)[0]
    str_table_off = struct.unpack_from('>I', data, 8)[0]

    # Extract string names from the string table
    names = []
    if str_table_off < len(data):
        cur = bytearray()
        for i in range(str_table_off, len(data)):
            b = data[i]
            if 32 <= b < 127:
                cur.append(b)
            else:
                if b == 0 and len(cur) >= 2:
                    names.append(cur.decode('ascii'))
                cur = bytearray()
    return {
        'n_vars': n_vars, 'n_fields': n_fields,
        'str_table_off': str_table_off,
        'variable_names': names,
    }


# ============================================================
# StreamTOC — Level Stream Hierarchy
# ============================================================
# Decoded from DS IDA (StreamTOC::InplaceFixup, version 8-9).
# Maps the complete streaming hierarchy: zones, IGC cutscene shots,
# challenge modes, and story mode sub-streams per level.

def parse_stream_toc(data):
    """Parse StreamTOC resource. Returns stream hierarchy paths."""
    if len(data) < 12:
        return None
    hash_val = struct.unpack_from('>I', data, 0)[0]
    version = struct.unpack_from('>I', data, 4)[0]

    # Extract all embedded path strings
    paths = []
    cur = bytearray()
    for i in range(12, len(data)):
        b = data[i]
        if 32 <= b < 127:
            cur.append(b)
        else:
            if b == 0 and len(cur) >= 4:
                s = cur.decode('ascii')
                # Filter out garbage strings (must contain a letter)
                if any(c.isalpha() for c in s) and not all(c in '!>}' for c in s):
                    paths.append(s)
            cur = bytearray()
    return {
        'hash': hash_val, 'version': version,
        'paths': paths,
    }


# ============================================================
# UIX — Scaleform Flash UI Container ("uixf" magic)
# ============================================================
# Flash/SWF-based UI system using EA's Scaleform (APT) integration.
# Contains UI layout, texture references, and widget definitions.

def parse_uix(data):
    """Parse UIX Scaleform UI resource. Returns basic info + texture refs."""
    if len(data) < 16 or data[:4] != b'uixf':
        return None
    file_size = struct.unpack_from('>I', data, 4)[0]
    # title tag at +0x08
    title_tag = data[8:12].decode('ascii', errors='replace')

    # Extract title name and embedded texture references
    strings = []
    cur = bytearray()
    for i in range(12, len(data)):
        b = data[i]
        if 32 <= b < 127:
            cur.append(b)
        else:
            if b == 0 and len(cur) >= 4:
                s = cur.decode('ascii')
                if any(c.isalpha() for c in s):
                    strings.append(s)
            cur = bytearray()

    # Separate title from texture refs
    title = strings[0] if strings else ''
    textures = [s for s in strings if s.endswith('.tga') or s.endswith('.png')]
    return {
        'magic': 'uixf', 'file_size': file_size,
        'title': title, 'texture_refs': textures,
        'n_strings': len(strings),
    }


# ============================================================
# AMB — Ambient Sound Bank (ABKC container)
# ============================================================
# Decoded from char_sa_bart.amb / char_sa_lisa.amb samples.
# Standalone audio bank file (NOT inside STR — found on disc alongside STR).
#
# Layout:
#   Outer header (64 bytes):
#     +0x00: uint32 version (9)
#     +0x04: uint32 total_audio_size
#     +0x08: uint32 n_entries (sound clip count)
#     +0x0C: uint32 total_audio_size (duplicate)
#     +0x14: uint32 SDBM_hash (category identifier)
#     +0x18: float32 volume (1.0), float32 unk (2.0), float32 max_distance (100.0)
#     +0x24: uint32 flags
#   ABKC block (at +0x40):
#     "ABKC" magic + version bytes + codec/type info + sizes + offsets
#     Entry table (n_entries × entry_size bytes)
#     Audio sample data (EALayer3 / XAS codec)

def parse_amb(data):
    """Parse AMB ambient sound bank file. Returns header info and entry count."""
    if len(data) < 0x80:
        return None
    version = struct.unpack_from('>I', data, 0)[0]
    total_size = struct.unpack_from('>I', data, 4)[0]
    n_entries = struct.unpack_from('>I', data, 8)[0]
    sdbm_hash = struct.unpack_from('>I', data, 0x14)[0]
    volume = struct.unpack_from('>f', data, 0x18)[0]
    max_distance = struct.unpack_from('>f', data, 0x20)[0]
    flags = struct.unpack_from('>I', data, 0x24)[0]

    # ABKC block at 0x40
    if data[0x40:0x44] != b'ABKC':
        return None
    abkc_ver = f'{data[0x44]}.{data[0x45]}.{data[0x46]}.{data[0x47]}'
    codec = data[0x49]
    header_size = struct.unpack_from('>I', data, 0x58)[0]
    entry_size = struct.unpack_from('>I', data, 0x5C)[0]
    data_size = struct.unpack_from('>I', data, 0x64)[0]

    return {
        'version': version,
        'n_entries': n_entries,
        'total_audio_size': total_size,
        'sdbm_hash': sdbm_hash,
        'volume': volume,
        'max_distance': max_distance,
        'flags': flags,
        'abkc_version': abkc_ver,
        'codec': codec,
        'entry_size': entry_size,
        'header_size': header_size,
        'data_size': data_size,
    }


# ============================================================
# CHA — Chatter Alias Bank (Audio Event → GUID Mapping)
# ============================================================
# AliasBank format from Unity viewer (AliasBank.cs).
# Maps chatter bank ID hashes to audio asset GUIDs + subtitle hashes.
# Paired with CHT templates that define the event name strings.
#
# Layout:
#   +0x00: uint16 hashes_per_entry (typically 6)
#   +0x02: uint16 entry_count
#   +0x04: uint32 list_offset
#   At list_offset, entries of 24 bytes each:
#     +0x00: uint32 chatter_bank_id_hash (SDBM hash of the bank name)
#     +0x04: 16 bytes GUID (4 × uint32 BE)
#     +0x14: uint32 subtitle_hash

def parse_cha(data):
    """Parse CHA chatter alias bank (AliasBank format).
    
    Returns entries with bank hash, GUID, and subtitle hash for each
    dialogue clip mapping.
    """
    if len(data) < 8:
        return None

    hashes_per_entry = struct.unpack_from('>H', data, 0)[0]
    n_entries = struct.unpack_from('>H', data, 2)[0]
    list_offset = struct.unpack_from('>I', data, 4)[0]

    entries = []
    for i in range(n_entries):
        off = list_offset + i * 24
        if off + 24 > len(data):
            break

        bank_hash = struct.unpack_from('>I', data, off)[0]
        g1, g2, g3, g4 = struct.unpack_from('>IIII', data, off + 4)
        subtitle_hash = struct.unpack_from('>I', data, off + 20)[0]

        entries.append({
            'bank_hash': bank_hash,
            'guid': f'{g1:08X}-{g2:08X}-{g3:08X}-{g4:08X}',
            'guid_prefix': g1,
            'guid_suffix': g2,
            'subtitle_hash': subtitle_hash,
        })

    return {
        'hashes_per_entry': hashes_per_entry,
        'n_entries': n_entries,
        'entries': entries,
    }


# ============================================================
# CHT — Chatter Template (Event Name Definitions)
# ============================================================
# Defines chatter event strings like "mobmember_go_destroy_object",
# "mobmember_currently_attacking_enemy", etc. These are the event
# names that trigger audio barks during gameplay.

def parse_cht(data):
    """Parse CHT chatter template resource. Returns event name strings."""
    if len(data) < 4:
        return None
    version = struct.unpack_from('>H', data, 0)[0]
    count = struct.unpack_from('>H', data, 2)[0]

    # Extract all embedded event name strings
    events = []
    cur = bytearray()
    for i in range(8, len(data)):
        b = data[i]
        if 32 <= b < 127:
            cur.append(b)
        else:
            if b == 0 and len(cur) >= 6:
                s = cur.decode('ascii')
                # Filter: real event names have underscores and lowercase
                if '_' in s and any(c.islower() for c in s):
                    events.append(s)
            cur = bytearray()

    return {
        'version': version,
        'count': count,
        'events': events,
    }


# ============================================================
# BNK — EA Sound Bank (Animation Audio)
# ============================================================
# Version 9 audio bank containing character animation sound effects.
# Each BNK has multiple sound groups with animation-linked audio clips
# and EMX file references (the actual sound event identifiers).
#
# EMX naming convention:
#   fs_  = footstep sounds       (fs_m_ape_run_mtls.emx)
#   d_   = dialogue/voice        (d_ape_damage_hvy_01.emx)
#   char_= character effects     (char_ape_hit_chest_01.emx)
#   sx_  = general SFX           (sx_vox_herc_fatigue_01.emx)
#   ui_  = UI sounds             (ui_cheat_laugh.emx)
#
# Header layout (version 9):
#   +0x00: uint32 total_size
#   +0x04: uint32 version (9)
#   +0x08: Guid128 (16 bytes)
#   +0x18: pad (4 bytes)
#   +0x1C: uint32 flags
#   +0x20: uint32 n_groups
#   +0x24: uint32 n_sounds
#   +0x28+: section offsets

def parse_bnk(data):
    """Parse BNK sound bank. Returns header info, animation events, and EMX references."""
    if len(data) < 0x30:
        return None
    total_size = struct.unpack_from('>I', data, 0)[0]
    version = struct.unpack_from('>I', data, 4)[0]
    if version != 9:
        return None
    guid = struct.unpack_from('>4I', data, 8)
    n_groups = struct.unpack_from('>I', data, 0x20)[0]
    n_sounds = struct.unpack_from('>I', data, 0x24)[0]

    # Extract animation event names (strings with '_' that appear in the body)
    anim_events = []
    emx_refs = []
    cur = bytearray()
    for i in range(0x40, len(data)):
        b = data[i]
        if 32 <= b < 127:
            cur.append(b)
        else:
            if b == 0 and len(cur) >= 6:
                s = cur.decode('ascii')
                if '.emx' in s:
                    emx_refs.append(s)
                elif '_' in s and any(c.islower() for c in s):
                    # Strip leading underscores/padding
                    clean = s.lstrip(' _\t')
                    if len(clean) >= 4 and clean not in anim_events:
                        anim_events.append(clean)
            cur = bytearray()

    return {
        'version': version,
        'total_size': total_size,
        'guid': '-'.join(f'{g:08X}' for g in guid),
        'n_groups': n_groups,
        'n_sounds': n_sounds,
        'anim_events': anim_events,
        'emx_refs': emx_refs,
    }


def parse_trinity(data):
    """Parse TRINITY_SEQ_MASTER cutscene sequence resource.
    Returns dict with sequence info, track count, and embedded strings."""
    if len(data) < 16 or data[:4] != b'seqm':
        return None

    total_size = struct.unpack_from('>I', data, 4)[0]
    n_sequences = struct.unpack_from('>I', data, 8)[0]

    sequences = []
    # Find seqb blocks by scanning for the magic
    pos = 0
    while True:
        pos = data.find(b'seqb', pos)
        if pos < 0:
            break

        if pos + 0x70 > len(data):
            break

        n_tracks = struct.unpack_from('>I', data, pos + 4)[0]
        seq_hash = struct.unpack_from('>I', data, pos + 8)[0]

        # Sequence name at +0x30 from seqb, 64 bytes null-padded
        name_raw = data[pos + 0x30:pos + 0x70]
        nul = name_raw.find(b'\x00')
        seq_name = (name_raw[:nul] if nul >= 0 else name_raw).decode('ascii', errors='replace')

        # Extract all embedded strings in this sequence's data
        # (actor names, sound bank refs, animation names)
        strings = []
        scan_start = pos + 0x70
        # Find end of this seqb (next seqb or end of data)
        next_seqb = data.find(b'seqb', pos + 4)
        scan_end = next_seqb if next_seqb > 0 else len(data)

        cur = bytearray()
        for i in range(scan_start, scan_end):
            b = data[i]
            if 32 <= b < 127:
                cur.append(b)
            else:
                if b == 0 and len(cur) >= 4:
                    s = cur.decode('ascii')
                    if not all(c == '\xbb' for c in s):
                        strings.append(s)
                cur = bytearray()

        sequences.append({
            'name': seq_name,
            'hash': seq_hash,
            'n_tracks': n_tracks,
            'offset': pos,
            'strings': strings,
        })
        pos += 4

    return {
        'magic': 'seqm',
        'total_size': total_size,
        'n_sequences': n_sequences,
        'sequences': sequences,
    }


# ============================================================
# Havok Packfile — HKO/HKT Physics Files
# ============================================================
def parse_havok(data):
    """Parse Havok packfile (.hko/.hkt/.hkx) and return structure info.
    Returns dict with version, section list, and class instance counts.
    Returns None if not a recognizable Havok file."""
    if len(data) < 128:
        return None

    # TSG adds a 16-byte wrapper prefix; strip it
    body = data[16:]

    magic0 = struct.unpack_from('>I', body, 0)[0]
    if magic0 != HAVOK_PACKFILE_MAGIC:
        return None

    file_version = struct.unpack_from('>I', body, 12)[0]
    bytes_in_ptr = body[16]
    n_sections = struct.unpack_from('>I', body, 20)[0]
    vs_raw = body[40:56]
    nul = vs_raw.find(b'\x00')
    version_str = (vs_raw[:nul] if nul >= 0 else vs_raw).decode('ascii', errors='replace')

    # Parse section headers (48 bytes each, starting at 64)
    sections = []
    off = 64
    for i in range(n_sections):
        if off + 48 > len(body):
            break
        tag = body[off:off + 19].rstrip(b'\x00').decode('ascii', errors='replace')
        abs_start = struct.unpack_from('>I', body, off + 20)[0]
        local_fix = struct.unpack_from('>I', body, off + 24)[0]
        global_fix = struct.unpack_from('>I', body, off + 28)[0]
        virt_fix = struct.unpack_from('>I', body, off + 32)[0]
        exports = struct.unpack_from('>I', body, off + 36)[0]
        imports = struct.unpack_from('>I', body, off + 40)[0]
        end = struct.unpack_from('>I', body, off + 44)[0]
        sections.append({
            'tag': tag,
            'abs_data_start': abs_start,
            'data_size': local_fix,
            'virtual_fixups_off': virt_fix,
            'virtual_fixups_size': exports - virt_fix,
            'end': end,
        })
        off += 48

    # Build class instance list via virtual fixups in __data__
    cn_sec = next((s for s in sections if s['tag'] == '__classnames__'), None)
    data_sec = next((s for s in sections if s['tag'] == '__data__'), None)
    class_instances = []
    class_counts = {}

    if cn_sec and data_sec and data_sec['virtual_fixups_size']:
        vf_abs = data_sec['abs_data_start'] + data_sec['virtual_fixups_off']
        n_vfs = data_sec['virtual_fixups_size'] // 12
        cn_start = cn_sec['abs_data_start']

        for i in range(n_vfs):
            entry_off = vf_abs + i * 12
            if entry_off + 12 > len(body):
                break
            data_off = struct.unpack_from('>I', body, entry_off)[0]
            if data_off == 0xFFFFFFFF:
                break
            cn_off = struct.unpack_from('>I', body, entry_off + 8)[0]
            name_abs = cn_start + cn_off
            if 0 <= name_abs < len(body):
                end_ptr = body.find(b'\x00', name_abs)
                if end_ptr > name_abs:
                    name = body[name_abs:end_ptr].decode('ascii', errors='replace')
                    class_instances.append({'data_off': data_off, 'class_name': name})
                    class_counts[name] = class_counts.get(name, 0) + 1

    return {
        'version': version_str,
        'file_version': file_version,
        'bytes_in_ptr': bytes_in_ptr,
        'n_sections': n_sections,
        'sections': sections,
        'class_instances': class_instances,
        'class_counts': class_counts,
    }


# ============================================================
# Attribute Command Map
# ============================================================
# Maps behavior class name → {attr_index: data_type}
# Extracted from Unity viewer (TSGFileViewer) attribute handlers.
# Data types: 'string' (relative offset to null-term string),
#             'Guid128' (relative offset to 16-byte GUID),
#             'matrix' (relative offset to 64-byte RwMatrixTag),
#             'float', 'int32', 'uint32', 'uint16' (direct value).

# ============================================================
# Component Hash Table — TSG Component System
# ============================================================
# Decoded from Unity viewer KnownHashedStrings + brute-force matching
# against E3 build's CClassFactory::RegisterClass calls (sub_82755140).
# 58/80 E3 components decoded; remaining 22 are TSG-internal names that
# don't appear as strings in any public source.
#
# Key fields per registration: hash, factory, size, flags, alignment.
# Flags 1 = singleton/abstract, 3 = instantiable.
# Align 16 = SIMD-aligned (math/physics/graphics).

COMPONENT_HASHES = {
    # NPC AI components
    0x011F6822: 'NPCAlertComponent',
    0x4D7526AE: 'NPCJumpComponent',
    0x58FBC0CA: 'NPCDashComponent',
    0x5E2170FA: 'NPCBackUpComponent',
    0x8CE7F650: 'NPCPatrolComponent',
    0x970ED1B9: 'NPCGuardComponent',
    0xB7B26811: 'NPCBlockComponent',
    0xD1DBC536: 'NPCFleeComponent',
    0xE4765FE7: 'NPCDodgeComponent',
    0xEED6412E: 'StrafeComponent',

    # Mob system (Marge mob control)
    0x49AB9943: 'MobMemberComponent',
    0x7CB9B634: 'MobLeaderComponent',
    0x204AA107: 'MobInteractComponent',

    # Combat & damage
    0x0CC8C337: 'ObjectDetectionComponent',
    0x1A4C5E20: 'TeamComponent',
    0x1B90C46E: 'DamageComponent',
    0xA00E4F17: 'CombatEventComponent',
    0xCA979A09: 'DeathComponent',
    0xD3AC1493: 'AttackCollisionComponent',
    0xDC51EBB1: 'StandardDamageComponent',

    # Player/HUD/UI
    0x8BD28E67: 'PlayerHUDComponent',
    0xBD55956B: 'ScoreComponent',
    0xF22FEE61: 'InventoryComponent',

    # Movement & character
    0x6E592245: 'CharacterMovementComponent',
    0xED5F61B5: 'HeadTrackingTargetComponent',
    0x756A6E07: 'StateMachineComponent',
    0x9AADAEE2: 'ScriptableStateMachineComponent',
    0x7EA0FF17: 'TunableScriptableSMComponent',

    # Audio
    0xEA432AB4: 'ChatterComponent',

    # Gameplay
    0xF3B23EA0: 'TrampolineComponent',
}


ATTR_CMD_MAP = {
    'AndEventGate': {
        0: 'string',  # m_inputEvent01
        1: 'string',  # m_inputEvent02
        2: 'string',  # m_inputEvent03
        3: 'string',  # m_inputEvent04
        4: 'string',  # m_outputEvent
        5: 'float',   # m_delayTime
        6: 'string',  # m_resetEvent
    },
    'Animated': {
        0: 'string',  # animation state name
        1: 'int32',
        2: 'int32',
    },
    'CSystemCommands': {
        0: 'int32',
        1: 'matrix',  # m_matrix (transform)
        2: 'int32', 3: 'int32', 4: 'int32',
    },
    'ChatterAssetSet': {
        0: 'string', 1: 'string', 2: 'int32',
    },
    'DebugText': {
        0: 'string',  # m_targetName
        1: 'string',  # m_pDebugStr
        2: 'float',   # m_displayTime
        3: 'uint32',  # m_options
    },
    'EnterExitTrigger': {
        0: 'string',  # m_enterTarget
        1: 'string',  # m_exitTarget
        2: 'string',  # m_insideTarget
        3: 'float',   # m_insideTargetWait
        4: 'string',  # m_activate
        5: 'string',  # m_deactivate
        6: 'uint32',  # m_flags
    },
    'Entity': {
        0: 'uint32',  # m_flags
        1: 'Guid128', # MetaModel GUID
    },
    'EntityDamageDealer': {
        0: 'Guid128', # m_entityGUID
        1: 'string',  # m_damageMsg
        2: 'float',   # m_fDamageAmount
        3: 'uint32',  # m_damageLevel
        4: 'uint32',  # m_damageType
        5: 'uint32',  # m_flags
    },
    'EventText': {
        0: 'string', 1: 'string', 2: 'int32', 3: 'int32',
    },
    'ExecuteVFX': {
        0: 'string',  # m_targetName
        1: 'string',  # m_deleteName
        2: 'Guid128', # m_VFXGUID
        4: 'uint32',  # m_Flags
        5: 'Guid128', # m_guidTarget
        6: 'string',  # m_boneNameHash
        8: 'int32',   # m_vBindOffset
    },
    'FuncRotate': {
        0: 'Guid128', # entity to rotate
        1: 'uint32',  # axis enum (0-5: ±X, ±Y, ±Z)
        2: 'float',   # rotation speed (radians)
        3: 'float', 4: 'float', 5: 'uint32',
        6: 'float', 7: 'uint32',
        8: 'string', 9: 'string', 10: 'string', 11: 'string', 12: 'string',
    },
    'FuncSpawn': {
        0: 'string',  # m_targetName (event to listen for)
        1: 'string',  # m_entityCreated (event fired on spawn)
        2: 'Guid128', # m_spawnTarget
        3: 'uint32',  # m_funcSpawnFlags
    },
    'GraphMoverController': {
        0: 'Guid128', # m_graphGuid
        1: 'float',   # m_arrivalSpeed
        2: 'float',   # m_accelerationTime
        3: 'float',   # m_turnDistance
        4: 'float',   # m_arrivalTolerance
        5: 'uint32',  # m_flags
    },
    'IMover': {
        0: 'string',  # m_travelMsg
        1: 'Guid128', # m_startControllerGuid
        2: 'uint32', 3: 'uint32',  # m_motionState
    },
    'IMoverController': {
        0: 'Guid128', # m_moverGuid
        1: 'string',  # m_callMsg
        2: 'string',  # m_setDestinationMsg
        3: 'string',  # m_arrivalMsg
    },
    'LoadMusicProject': {
        0: 'string',  # m_targetName
        1: 'Guid128', # m_musicProjectGuid
    },
    'MultiManager': {
        0: 'string',  # m_targetName
        1: 'int32',   # m_nResponses
        2: 'uint16',  # m_multimanagerFlags
        # 3-18 alternate: string event name, float event time (8 events)
        3: 'string', 4: 'float', 5: 'string', 6: 'float',
        7: 'string', 8: 'float', 9: 'string', 10: 'float',
        11: 'string', 12: 'float', 13: 'string', 14: 'float',
        15: 'string', 16: 'float', 17: 'string', 18: 'float',
    },
    'PlaySound': {
        0: 'uint32',  # m_playSoundFlags
        1: 'string',  # m_playTargetMsg
        2: 'string',  # m_stopTargetMsg
        3: 'Guid128', 4: 'Guid128',  # script GUIDs
        5: 'Guid128', # m_guidTarget
        6: 'string',  # m_attachTargetMsg
        7: 'uint32',  # m_location
    },
    'ScriptedSequence': {
        0: 'string',  # m_targetName
        1: 'string',  # m_target
        2: 'string',  # m_interruptTarget
        3: 'Guid128', # m_hScriptTarget
        4: 'string',  # m_actionAnimationString
        5: 'string',  # m_idleAnimationString
        6: 'string',  # m_runAnimationString
        7: 'string',  # m_waitAnimationString
        8: 'uint32',  # m_ssCrouchCtrlFlags
        9: 'uint32',  # m_nMoveToType
        10: 'Guid128', # m_hOrientationTargetGUID
        11: 'uint32',  # m_settingsFlags
        12: 'uint32',  # m_actionCount
        13: 'Guid128', # m_hLocationOverrideGUID
        14: 'Guid128', # m_refObj
        15: 'string',
        16: 'float',   # m_cosAttractorAngle
    },
    'SetAudioMix': {
        0: 'string',  # m_targetName
        1: 'string',  # m_stopTargetName
        2: 'Guid128', # m_mixAssetGuid
        3: 'uint32',  # m_bCombine
        4: 'uint32',  # m_bConditional
    },
    'SlidingDoor': {
        0: 'float',   # m_slideDistance
        1: 'uint32',  # m_eSlidingDoorAxis (0=X, 1=Y, 2=Z)
    },
    'StreamSet': {
        0: 'string',  # m_targetName
        1: 'string',  # m_target
        2: 'uint32',  # m_options
        3: 'Guid128', # m_igcStream
        4: 'Guid128', # m_skyboxStream
        # 5-14: additional stream refs (Guid128)
        5: 'Guid128', 6: 'Guid128', 7: 'Guid128', 8: 'Guid128', 9: 'Guid128',
        10: 'Guid128', 11: 'Guid128', 12: 'Guid128', 13: 'Guid128', 14: 'Guid128',
    },
    'TestEntityExists': {
        0: 'string',  # m_targetName
        1: 'Guid128', # m_queryEntityGuid
        2: 'string',  # m_existTarget
        3: 'string',  # m_noExistTarget
    },
    'TestPlayerInput': {
        0: 'string',  # m_inputTargetName
        1: 'string',  # m_outputTargetName
        2: 'uint32',  # m_PlayerInputPattern01
        3: 'float',   # m_waitTime
    },
    'TestScore': {
        0: 'uint32',  # m_scoreNameHash
        1: 'float',   # m_operand
        2: 'uint32',  # m_flags
        3: 'string',  # m_receiveMsg
        4: 'string',  # m_successMsg
        5: 'string',  # m_failMsg
    },
    'TriggerAuto': {
        0: 'string',  # m_targetName
        1: 'float',   # m_delay
        2: 'uint32',  # m_options
    },
    'TriggerBase': {
        0: 'string',  # m_targetName
        1: 'string',  # m_activate
        2: 'string',  # m_deactivate
        3: 'uint32',  # m_primitive (0=box, 1=sphere)
        4: 'string',  # m_target
        5: 'float',   # m_delay
        6: 'int32',   # m_count
        7: 'string',  # m_reverseTarget
        8: 'uint32',  # m_options
        9: 'uint32',  # touch tracking
        10: 'float',  # m_wait
        11: 'float',  # speed threshold (mph→m/s)
    },
    'TriggerHurt': {
        0: 'float',   # m_damageAmount
        1: 'uint32',  # m_damageType
        2: 'uint32',  # m_damageLevel
        3: 'uint32',  # m_triggerHurtFlags
    },
    'VariableOperator': {
        0: 'string',  # m_targetName
        1: 'int32',   # m_variableHandle
        2: 'int32',   # m_iOperator
        3: 'int32',   # m_iOperand
    },
    'VariableSwitch': {
        0: 'string',  # m_targetName
        1: 'uint32',  # m_variableID
        3: 'string',  # m_defaultTarget
        4: 'float',   # m_fDefaultDelay
    },
    'VariableWatcher': {
        0: 'string',  # m_targetName
        1: 'string',  # m_deactivate
        2: 'string',  # m_target
        3: 'float',   # m_fDelay
        4: 'uint32',  # m_variableId
        5: 'uint32',  # m_uiCondition
        6: 'int32',   # m_iThreshold
        7: 'uint32',  # m_options
        8: 'int32',   # m_conditionMetThreshold
    },
}


# ============================================================
# Entity Position Extraction (from CSystemCommands)
# ============================================================
# In compact entity packets, CSystemCommands (0xB390B11A) attr[1]
# is a transform matrix (RwMatrixTag). The uint32 value is a relative
# offset from its storage position to the 64-byte matrix data.
# Matrix layout: right(16) + up(16) + at(16) + pos(16).
# Decoded from Dead Space IDA: CAttributeCommand::GetCommandData(RwMatrixTag*).

CSYSTEMCOMMANDS_HASH = 0xB390B11A

def extract_entity_positions(raw_data, n_entities):
    """Extract entity positions from compact SimGroup data.
    
    Parses CSystemCommands behavior packets to find transform matrices,
    returning entity type, position (x,y,z), and rotation matrix for
    entities that have explicit spatial placement.
    
    Returns list of dicts with keys:
        entity_type, entity_hash, position (x,y,z),
        rotation_right, rotation_up, rotation_at (3-tuples),
        has_position (bool)
    """
    simG_off = 8
    if len(raw_data) < simG_off + 0x30 + n_entities * 4:
        return []
    
    entity_offsets = []
    for i in range(n_entities):
        off = simG_off + 0x30 + i * 4
        entity_offsets.append(struct.unpack_from('>I', raw_data, off)[0])
    
    results = []
    for ei in range(n_entities):
        eoff = entity_offsets[ei]
        abs_off = simG_off + eoff
        pkt = abs_off + 8  # skip 8-byte overhead
        
        if pkt + 16 > len(raw_data):
            continue
        
        m_flags = raw_data[pkt]
        if not (m_flags & 1):  # compact only
            continue
        
        n_attached = raw_data[pkt + 2]
        n_behaviors = raw_data[pkt + 3]
        class_hash = struct.unpack_from('>I', raw_data, pkt + 12)[0]
        class_name = ENTITY_TYPES.get(class_hash, f'UNKNOWN_{class_hash:#010x}')
        
        entity_result = {
            'entity_type': class_name,
            'entity_hash': class_hash,
            'position': (0.0, 0.0, 0.0),
            'rotation_right': (1.0, 0.0, 0.0),
            'rotation_up': (0.0, 1.0, 0.0),
            'rotation_at': (0.0, 0.0, 1.0),
            'has_position': False,
        }
        
        beh_table_off = pkt + 16 + n_attached * 4
        
        for j in range(n_behaviors):
            boff_pos = beh_table_off + j * 4
            if boff_pos + 4 > len(raw_data):
                break
            boff = struct.unpack_from('>i', raw_data, boff_pos)[0]
            abs_beh = boff_pos + boff
            
            if abs_beh < 0 or abs_beh + 6 > len(raw_data):
                continue
            
            beh_hash = struct.unpack_from('>I', raw_data, abs_beh)[0]
            if beh_hash != CSYSTEMCOMMANDS_HASH:
                continue
            
            n_attrs = struct.unpack_from('>H', raw_data, abs_beh + 4)[0]
            if n_attrs < 2:
                continue
            
            bitvec_off = abs_beh + 6
            bitvec_size = (n_attrs + 7) // 8
            if bitvec_off + bitvec_size > len(raw_data):
                continue
            bitvec = raw_data[bitvec_off:bitvec_off + bitvec_size]
            data_cursor = (bitvec_off + bitvec_size + 3) & ~3
            
            # Walk to attr[1] (transform command)
            cursor = data_cursor
            for a in range(2):
                is_zero = (bitvec[a >> 3] >> (a & 7)) & 1
                if a == 1:
                    if not is_zero and cursor + 4 <= len(raw_data):
                        val = struct.unpack_from('>I', raw_data, cursor)[0]
                        sval = val if val < 0x80000000 else val - 0x100000000
                        target = cursor + sval
                        
                        if 0 <= target and target + 64 <= len(raw_data):
                            # RwMatrixTag: right(12+4) + up(12+4) + at(12+4) + pos(12+4)
                            rx, ry, rz = struct.unpack_from('>3f', raw_data, target)
                            ux, uy, uz = struct.unpack_from('>3f', raw_data, target + 16)
                            ax, ay, az = struct.unpack_from('>3f', raw_data, target + 32)
                            px, py, pz = struct.unpack_from('>3f', raw_data, target + 48)
                            
                            entity_result['position'] = (px, py, pz)
                            entity_result['rotation_right'] = (rx, ry, rz)
                            entity_result['rotation_up'] = (ux, uy, uz)
                            entity_result['rotation_at'] = (ax, ay, az)
                            entity_result['has_position'] = True
                if not is_zero:
                    cursor += 4
            break  # found CSystemCommands
        
        results.append(entity_result)
    
    return results


# ============================================================
# Entity-to-Asset Resolution
# ============================================================
# Entities reference assets via two mechanisms:
# 1. Entity behavior attr[1]: GUID → MetaModel (visual model definition)
# 2. Attached resources (OffsetGUID_t): GUID → EARS_MESH, BNK, RCB, etc.
# Decoded from DS IDA: CAttachResourceIterator, OffsetGUID_t::operator guid128_t.

ENTITY_CLASS_HASH = 0x38523FC3  # Entity behavior class

def build_guid_lookup(assets):
    """Build GUID→asset lookup from extracted STR assets.
    Returns dict mapping hex GUID string to (resource_type, filename)."""
    lookup = {}
    for a in assets:
        g = a.get('guid')
        if g and isinstance(g, tuple) and len(g) == 4:
            guid_hex = struct.pack('>4I', *g).hex()
            lookup[guid_hex] = (a.get('resource_type', '?'), a.get('filename', '?'))
    return lookup


def dump_entity(raw_data, entity_idx, n_entities):
    """Dump a single entity with all behaviors and attributes fully decoded.
    
    Uses ATTR_CMD_MAP to interpret each attribute value according to its
    declared type (string, Guid128, matrix, int32, uint32, uint16, float).
    Unknown attributes are returned as raw uint32.
    
    Returns a dict:
        {
            'index': entity_idx,
            'class': class_name, 'class_hash': uint32,
            'flags': uint8, 'n_attached': uint8, 'n_behaviors': uint8,
            'behaviors': [
                {
                    'hash': uint32, 'name': str, 'n_attrs': int,
                    'bitvec_hex': str,
                    'attrs': {attr_index: (type_str, decoded_value), ...}
                }, ...
            ]
        }
    """
    simG_off = 8
    if simG_off + 0x30 + n_entities * 4 > len(raw_data):
        return None
    
    ptr_off = simG_off + 0x30 + entity_idx * 4
    eoff = struct.unpack_from('>I', raw_data, ptr_off)[0]
    abs_off = simG_off + eoff
    pkt = abs_off + 8
    
    if pkt + 16 > len(raw_data):
        return None
    
    m_flags = raw_data[pkt]
    n_attached = raw_data[pkt + 2]
    n_behaviors = raw_data[pkt + 3]
    class_hash = struct.unpack_from('>I', raw_data, pkt + 12)[0]
    class_name = ENTITY_TYPES.get(class_hash, f'UNKNOWN_{class_hash:#010x}')
    
    result = {
        'index': entity_idx,
        'class': class_name, 'class_hash': class_hash,
        'flags': m_flags, 'n_attached': n_attached, 'n_behaviors': n_behaviors,
        'behaviors': [],
    }
    
    beh_table_off = pkt + 16 + n_attached * 4
    
    for j in range(n_behaviors):
        boff_pos = beh_table_off + j * 4
        if boff_pos + 4 > len(raw_data):
            break
        boff = struct.unpack_from('>i', raw_data, boff_pos)[0]
        abs_beh = boff_pos + boff
        
        if abs_beh < 0 or abs_beh + 6 > len(raw_data):
            continue
        
        beh_hash = struct.unpack_from('>I', raw_data, abs_beh)[0]
        # Look up behavior name. First try BEHAVIOR_HASHES, then fall back
        # to ENTITY_TYPES (many entity classes double as their own behavior)
        # and COMPONENT_HASHES.
        beh_name = BEHAVIOR_HASHES.get(beh_hash)
        if beh_name is None:
            beh_name = ENTITY_TYPES.get(beh_hash)
        if beh_name is None:
            beh_name = COMPONENT_HASHES.get(beh_hash)
        if beh_name is None:
            beh_name = f'UNKNOWN_{beh_hash:#010x}'
        n_attrs = struct.unpack_from('>H', raw_data, abs_beh + 4)[0]
        
        # Bitvec: read as raw bytes, then test bit LSB-first per byte
        # (uint16 if n_attrs <= 16, uint32 otherwise)
        if n_attrs <= 16:
            if abs_beh + 8 > len(raw_data): continue
            bitvec_bytes = bytes(raw_data[abs_beh + 6:abs_beh + 8])
            bitvec_size = 2
        else:
            if abs_beh + 10 > len(raw_data): continue
            bitvec_bytes = bytes(raw_data[abs_beh + 6:abs_beh + 10])
            bitvec_size = 4
        
        # Data cursor aligned to 4 bytes after bitvec
        data_cursor = (abs_beh + 6 + bitvec_size + 3) & ~3
        
        attrs = {}
        cursor = data_cursor
        attr_map = ATTR_CMD_MAP.get(beh_name, {})
        
        for a in range(n_attrs):
            # bit 1 == "attribute is zero/skipped", bit 0 == "present"
            byte_i = a >> 3
            if byte_i >= len(bitvec_bytes):
                break  # bitvec exhausted — assume remaining attrs absent
            is_zero = (bitvec_bytes[byte_i] >> (a & 7)) & 1
            if is_zero:
                continue
            
            if cursor + 4 > len(raw_data):
                break
            
            attr_type = attr_map.get(a, '?')
            raw_val = struct.unpack_from('>I', raw_data, cursor)[0]
            
            if attr_type == 'int32':
                sval = raw_val if raw_val < 0x80000000 else raw_val - 0x100000000
                attrs[a] = ('int32', sval)
            elif attr_type == 'uint32':
                attrs[a] = ('uint32', raw_val)
            elif attr_type == 'uint16':
                attrs[a] = ('uint16', raw_val & 0xFFFF)
            elif attr_type == 'float':
                attrs[a] = ('float', struct.unpack_from('>f', raw_data, cursor)[0])
            elif attr_type == 'string':
                sval = raw_val if raw_val < 0x80000000 else raw_val - 0x100000000
                target = cursor + sval
                if 0 <= target < len(raw_data):
                    end = raw_data.find(b'\x00', target)
                    if 0 <= target < end < target + 256:
                        attrs[a] = ('string', raw_data[target:end].decode('ascii', errors='replace'))
                    else:
                        attrs[a] = ('?string', f'off={sval:+d}')
                else:
                    attrs[a] = ('?string', f'off={sval:+d} oob')
            elif attr_type == 'Guid128':
                sval = raw_val if raw_val < 0x80000000 else raw_val - 0x100000000
                target = cursor + sval
                if 0 <= target and target + 16 <= len(raw_data):
                    guid = struct.unpack_from('>4I', raw_data, target)
                    attrs[a] = ('Guid128', '-'.join(f'{g:08X}' for g in guid))
                else:
                    attrs[a] = ('?Guid128', f'off={sval:+d}')
            elif attr_type == 'matrix':
                sval = raw_val if raw_val < 0x80000000 else raw_val - 0x100000000
                target = cursor + sval
                if 0 <= target and target + 64 <= len(raw_data):
                    right = struct.unpack_from('>3f', raw_data, target)
                    up = struct.unpack_from('>3f', raw_data, target + 16)
                    at = struct.unpack_from('>3f', raw_data, target + 32)
                    pos = struct.unpack_from('>3f', raw_data, target + 48)
                    attrs[a] = ('matrix', {'right': right, 'up': up, 'at': at, 'pos': pos})
                else:
                    attrs[a] = ('?matrix', f'off={sval:+d}')
            else:
                attrs[a] = ('raw', f'{raw_val:#010x}')
            
            cursor += 4
        
        result['behaviors'].append({
            'hash': beh_hash, 'name': beh_name, 'n_attrs': n_attrs,
            'bitvec_hex': bitvec_bytes.hex(), 'attrs': attrs,
        })
    
    return result


def extract_all_entities(raw_data, n_entities):
    """Extract all entities in a SimGroup with full attribute decoding.
    Returns list of dicts produced by dump_entity()."""
    results = []
    for i in range(n_entities):
        ent = dump_entity(raw_data, i, n_entities)
        if ent is not None:
            results.append(ent)
    return results


def resolve_entity_assets(raw_data, n_entities, guid_lookup):
    """Resolve entity MetaModel and attached resource references.
    
    For each entity, extracts:
    - metamodel: (type, filename) from Entity behavior attr[1] GUID
    - attached_resources: list of (type, filename) from OffsetGUID_t array
    
    Returns list of dicts (same length as n_entities) with keys:
        entity_type, entity_hash, metamodel, attached_resources
    """
    simG_off = 8
    if len(raw_data) < simG_off + 0x30 + n_entities * 4:
        return []
    
    entity_offsets = []
    for i in range(n_entities):
        off = simG_off + 0x30 + i * 4
        entity_offsets.append(struct.unpack_from('>I', raw_data, off)[0])
    
    results = []
    for ei in range(n_entities):
        eoff = entity_offsets[ei]
        abs_off = simG_off + eoff
        pkt = abs_off + 8
        
        if pkt + 16 > len(raw_data):
            continue
        
        m_flags = raw_data[pkt]
        if not (m_flags & 1):
            continue
        
        n_attached = raw_data[pkt + 2]
        n_behaviors = raw_data[pkt + 3]
        class_hash = struct.unpack_from('>I', raw_data, pkt + 12)[0]
        class_name = ENTITY_TYPES.get(class_hash, f'UNKNOWN_{class_hash:#010x}')
        
        entity_result = {
            'entity_type': class_name,
            'entity_hash': class_hash,
            'metamodel': None,
            'attached_resources': [],
        }
        
        # Resolve attached resources (OffsetGUID_t array at pkt+16)
        for ri in range(n_attached):
            res_off_pos = pkt + 16 + ri * 4
            if res_off_pos + 4 > len(raw_data):
                break
            offset_val = struct.unpack_from('>i', raw_data, res_off_pos)[0]
            if offset_val != 0:
                guid_addr = res_off_pos + offset_val
                if 0 <= guid_addr < len(raw_data) - 16:
                    guid_hex = raw_data[guid_addr:guid_addr + 16].hex()
                    asset = guid_lookup.get(guid_hex)
                    if asset:
                        entity_result['attached_resources'].append(asset)
        
        # Resolve MetaModel from Entity behavior attr[1]
        beh_table_off = pkt + 16 + n_attached * 4
        for j in range(n_behaviors):
            boff_pos = beh_table_off + j * 4
            if boff_pos + 4 > len(raw_data):
                break
            boff = struct.unpack_from('>i', raw_data, boff_pos)[0]
            abs_beh = boff_pos + boff
            if abs_beh < 0 or abs_beh + 6 > len(raw_data):
                continue
            beh_hash = struct.unpack_from('>I', raw_data, abs_beh)[0]
            if beh_hash != ENTITY_CLASS_HASH:
                continue
            n_attrs = struct.unpack_from('>H', raw_data, abs_beh + 4)[0]
            if n_attrs < 2:
                continue
            bitvec_off = abs_beh + 6
            bitvec_size = (n_attrs + 7) // 8
            if bitvec_off + bitvec_size > len(raw_data):
                continue
            bitvec = raw_data[bitvec_off:bitvec_off + bitvec_size]
            data_cursor = (bitvec_off + bitvec_size + 3) & ~3
            cursor = data_cursor
            for a in range(2):
                is_zero = (bitvec[a >> 3] >> (a & 7)) & 1
                if a == 1 and not is_zero and cursor + 4 <= len(raw_data):
                    val = struct.unpack_from('>I', raw_data, cursor)[0]
                    sval = val if val < 0x80000000 else val - 0x100000000
                    target = cursor + sval
                    if 0 <= target < len(raw_data) - 16:
                        guid_hex = raw_data[target:target + 16].hex()
                        asset = guid_lookup.get(guid_hex)
                        if asset:
                            entity_result['metamodel'] = asset
                if not is_zero:
                    cursor += 4
            break
        
        results.append(entity_result)
    
    return results


# ============================================================
# Behavior Hash Constants (decoded from DS IDA + PDB + Unity viewer)
# ============================================================
# These are class hashes that appear in entity attribute packets.
# They identify which HandleAttributes function processes the data.
# NOTE: Many overlap with entity type hashes (e.g., Entity, TriggerBase, FuncSpawn).
# Behavior packets can stack: a TriggerBox entity has CSystemCommands + Entity + TriggerBase packets.
BEHAVIOR_HASHES = {
    # --- Core framework (DS IDA) ---
    0xFFD2E5B1: 'Base',                  # Base entity class (no attributes)
    0x8A157691: 'CAttributeHandler',     # Base class attribute handling
    0xB390B11A: 'CSystemCommands',       # Transform matrix (position/rotation)
    0x38523FC3: 'Entity',                # Entity: attr[0]=flags, attr[1]=MetaModel GUID
    0xAE986323: 'Animated',              # Animated: attr[0]=animation state name (string)
    0x48848DDB: 'ComponentList',         # Component list reference
    0x4E330172: 'launchObject',          # Object launch parameters
    # --- Trigger system (DS IDA + Unity viewer) ---
    0xD16A98A9: 'TriggerBase',           # 12 attrs: target, activate, deactivate, primitive, delay, count...
    0x23039DD0: 'TriggerFilter',         # Trigger filtering
    0xC8C5D222: 'EnterExitTrigger',      # Enter/exit trigger
    0xACBDFE47: 'TriggerAuto',           # Auto-trigger
    0xF26BB307: 'TriggerHurt',           # Damage trigger
    0x62EBE09B: 'TriggerRandom',         # Random trigger
    # --- Entity behaviors (Unity viewer) ---
    0x4B590617: 'FuncSpawn',             # Spawn: attr[0]=targetName, attr[1]=entityCreated, attr[2]=spawnGUID
    0xA018EA5F: 'FuncRotate',            # Rotate: attr[0]=entityGUID, attr[1]=axis, attr[2]=speed
    0x463FD53C: 'SlidingDoor',           # Door: attr[0]=slideDistance, attr[1]=axis
    0xB1A6D45B: 'PlaySound',             # Sound: attr[0]=flags, attr[1-2]=events, attr[3-5]=GUIDs
    0xCDB843CF: 'GraphMoverController',  # Graph mover: attr[0]=graphGUID, attr[1]=speed, attr[2]=accel
    0xA92A22F4: 'IMoverController',      # Mover controller interface
    0x6ACD17D8: 'IMover',                # Mover interface
    0x9C326942: 'StreamSet',             # Stream set: attr[0-1]=events, attr[2]=options, attr[3-14]=stream GUIDs
    0x5555E170: 'VariableSwitch',        # Variable switch
    0x5EE8CE40: 'VariableOperator',      # Variable operator
    0x29155EC0: 'VariableWatcher',       # Variable watcher
    0x6DF50074: 'MultiManager',          # Multi-target manager
    0x087E3D6E: 'MultiRemoveTarget',     # Multi-remove target
    0xB1CF0B4B: 'EntityDamageDealer',    # Damage dealer
    0x5FEF7F11: 'TestEntityExists',      # Test if entity exists
    0xF776AA00: 'TestScore',             # Test score condition
    0xCC6D6B17: 'TestPlayerInput',       # Test player input
    0x87B7A547: 'EventText',             # Event text display
    0x862623C0: 'DebugText',             # Debug text overlay
    0x77A210A2: 'ZoneRender',            # Zone render settings
    0x92F62833: 'ExecuteVFX',            # Execute VFX
    0x3243A9F0: 'ShakeCameraModifierInfo', # Camera shake
    0x48AF91A8: 'SetAudioMix',           # Audio mix settings
    0x539B225A: 'LoadMusicProject',      # Music project loader
    0x7BE194EE: 'AndEventGate',          # AND event gate logic
    0x8BD0E0EB: 'ScriptedSequence',      # Scripted animation sequence: 17 attrs (targetName, animations, etc.)
    0xB6912FFB: 'ChatterAssetSet',       # Chatter bank switch: attr[0]=name, attr[1]=bank, attr[2]=flags
    0x6796E9D0: 'SendScoreEvent',        # Send score event
    0xF941297A: 'FadeScreenFx',          # Screen fade effect
    0x92819E77: 'LetterBoxFx',           # Letterbox effect
    0x8FE80BDA: 'GummiObject',           # Gummi object (Homer's gummi form target)
    0x383225A1: 'Player',                # Player base class
    0xEAC08401: 'PlayerStart',           # Player spawn point
    0xDC24DB08: 'Checkpoint',            # Checkpoint
    0x0B251A33: 'TriggerBox',            # Box-shaped trigger volume
    # --- Physics (DS IDA) ---
    0xE794215B: 'MessageRelayBase',      # Message relay base
    0xBAA03366: 'RigidBodyWrapper',      # Physics rigid body (DS)
    0xDF0A26E8: 'CollisionTuner',        # Collision tuning (DS)
    0x4F3368D0: 'DynamicObjectBehavior', # Dynamic object physics
}


# ============================================================
# Main CLI
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description='TSG New-Gen (PS3/Xbox 360) extraction tool')
    sub = parser.add_subparsers(dest='command')

    p_info = sub.add_parser('info', help='Show STR archive info')
    p_info.add_argument('input', nargs='+')

    p_extract = sub.add_parser('extract', help='Extract STR archive contents')
    p_extract.add_argument('input', nargs='+')
    p_extract.add_argument('-o', '--output')

    p_sbk = sub.add_parser('sbk-info', help='Show SBK soundbank info')
    p_sbk.add_argument('input', nargs='+')

    p_sg = sub.add_parser('simgroup-info', help='Analyze SimGroup entity behaviors in an STR file')
    p_sg.add_argument('input', nargs='+')

    p_mm = sub.add_parser('metamodel-info', help='Show MetaModel resource info from extracted files')
    p_mm.add_argument('input', nargs='+')

    p_pos = sub.add_parser('entity-positions', help='Extract entity positions from STR SimGroup data')
    p_pos.add_argument('input', nargs='+')

    p_lh2 = sub.add_parser('dialogue', help='Extract dialogue/text from LH2 localization resources in STR files')
    p_lh2.add_argument('input', nargs='+')

    p_ed = sub.add_parser('entity-dump', help='Dump all entities with decoded attributes from STR SimGroups')
    p_ed.add_argument('input', nargs='+')
    p_ed.add_argument('--class-filter', dest='class_filter', default=None,
                      help='Only dump entities of this class (e.g. TriggerBox)')
    p_ed.add_argument('--limit', type=int, default=0,
                      help='Maximum entities to dump per SimGroup (0 = all)')

    p_hk = sub.add_parser('havok-info', help='Parse Havok HKO/HKT physics files in STR archives')
    p_hk.add_argument('input', nargs='+')

    p_cec = sub.add_parser('cec-info', help='Parse CEC controller configuration files')
    p_cec.add_argument('input', nargs='+')

    p_hud = sub.add_parser('hud-info', help='Parse hud.bin HUD layout scene graph files')
    p_hud.add_argument('input', nargs='+')
    p_hud.add_argument('--nodes', action='store_true', help='List all nodes')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if args.command == 'info':
        for path in args.input:
            cmd_str_info(path)

    elif args.command == 'extract':
        for path in args.input:
            print(f"\nExtracting: {path}")
            cmd_str_extract(path, args.output)

    elif args.command == 'sbk-info':
        for path in args.input:
            data = open(path, 'rb').read()
            info = parse_sbk_header(data)
            if info:
                print(f"\n{path}: SBK ({info['format']})")
                print(f"  Endian: {'LE' if info['endian'] == '<' else 'BE'}")
                print(f"  SDAT offset: {info['sdat_offset']:#x}  size: {info['sdat_size']:,}")
                if 'total_streams' in info:
                    print(f"  Streams: {info['total_streams']}")
                    for e in info['entries'][:10]:
                        print(f"    [{e['index']}] offset={e['offset']:#x}")
            else:
                print(f"\n{path}: not a valid SBK file")

    elif args.command == 'simgroup-info':
        from collections import Counter
        for path in args.input:
            print(f"\n{'='*60}")
            print(f"File: {path}")
            data = open(path, 'rb').read()
            assets, simgroups, header = extract_str_assets(data)
            total_ents = sum(sg['n_entities'] for sg in simgroups)
            print(f"SimGroups: {len(simgroups)}  Entities: {total_ents}")
            behavior_counts = Counter()
            for sg in simgroups:
                raw = sg.get('raw_data', b'')
                behaviors = scan_simgroup_entities(raw)
                for b in behaviors:
                    behavior_counts[b['name']] += 1
            print(f"\nBehavior types ({len(behavior_counts)}):")
            for name, count in behavior_counts.most_common():
                print(f"  {name:35s} {count:5d}")

    elif args.command == 'metamodel-info':
        for path in args.input:
            data = open(path, 'rb').read()
            mm = parse_metamodel(data)
            if mm:
                print(f"\n{path}: MetaModel v{mm['version']}")
                print(f"  GUID: {mm['guid_str']}")
                print(f"  Data size: {mm['data_size']}")
                if 'lod_distance' in mm:
                    print(f"  LOD distance: {mm['lod_distance']:.4f}")
                if 'section_offsets' in mm:
                    print(f"  Section offsets: {mm['section_offsets']}")
            else:
                print(f"\n{path}: not a MetaModel file")

    elif args.command == 'entity-positions':
        for path in args.input:
            data = open(path, 'rb').read()
            assets, simgroups, header = extract_str_assets(data)
            guid_lookup = build_guid_lookup(assets)
            
            for si, sg in enumerate(simgroups):
                if sg['n_entities'] == 0:
                    continue
                raw = sg['raw_data']
                n_ents = sg['n_entities']
                
                positions = extract_entity_positions(raw, n_ents)
                asset_info = resolve_entity_assets(raw, n_ents, guid_lookup)
                
                positioned = sum(1 for p in positions if p['has_position'])
                modeled = sum(1 for a in asset_info if a.get('metamodel'))
                
                print(f"\n{path} SimGroup[{si}]: {n_ents} entities, {positioned} positioned, {modeled} with models")
                print(f"{'Type':30s} {'Pos X':>8s} {'Pos Y':>8s} {'Pos Z':>8s}  {'MetaModel':s}")
                print('-' * 85)
                
                for i in range(n_ents):
                    if i >= len(positions) or i >= len(asset_info):
                        break
                    p = positions[i]
                    a = asset_info[i]
                    if not p['has_position'] and not a.get('metamodel'):
                        continue
                    
                    px, py, pz = p['position']
                    mm = a['metamodel'][1] if a.get('metamodel') else ''
                    pos_str = f"{px:8.2f} {py:8.2f} {pz:8.2f}" if p['has_position'] else '       -        -        -'
                    print(f"{p['entity_type']:30s} {pos_str}  {mm}")

    elif args.command == 'dialogue':
        for path in args.input:
            data = open(path, 'rb').read()
            assets, simgroups, header = extract_str_assets(data)
            lh2_assets = [a for a in assets if a.get('resource_type') == 'LH2']
            
            if not lh2_assets:
                print(f"\n{path}: no LH2 localization resources found")
                continue
            
            total_strings = 0
            for a in lh2_assets:
                result = parse_lh2(a.get('data', b''))
                if not result:
                    continue
                n = result['n_strings']
                total_strings += n
                lang_str = f"{result['n_languages']} lang{'s' if result['n_languages'] != 1 else ''}"
                wide_str = ' (UTF-16)' if result['is_wide'] else ''
                print(f"\n{a['filename']}: {n} strings, {lang_str}{wide_str}")
                
                if result['strings']:
                    for i in range(min(n, 5)):
                        h = result['hashes'][i] if i < len(result['hashes']) else 0
                        s = result['strings'][0][i] if i < len(result['strings'][0]) else ''
                        print(f"  [{i:3d}] {h:#010x}: {s[:70]}")
                    if n > 5:
                        print(f"  ... ({n - 5} more)")
            
            print(f"\nTotal: {total_strings} strings across {len(lh2_assets)} LH2 files")

    elif args.command == 'entity-dump':
        for path in args.input:
            print(f"\n{'='*70}")
            print(f"File: {path}")
            data = open(path, 'rb').read()
            assets, simgroups, _ = extract_str_assets(data)
            for sg_i, sg in enumerate(simgroups):
                n = sg.get('n_entities', 0)
                if not n:
                    continue
                ents = extract_all_entities(sg.get('raw_data', b''), n)
                print(f"\nSimGroup {sg_i}: {len(ents)} entities")

                if args.class_filter:
                    ents = [e for e in ents if e['class'] == args.class_filter]
                    print(f"  Filtered to class={args.class_filter!r}: {len(ents)} remaining")

                limit = args.limit if args.limit > 0 else len(ents)
                for e in ents[:limit]:
                    print(f"\n  Entity #{e['index']}: {e['class']} "
                          f"(flags={e['flags']:#x} "
                          f"behaviors={e['n_behaviors']} "
                          f"attached={e['n_attached']})")
                    for b in e['behaviors']:
                        if not b['attrs']:
                            continue
                        print(f"    {b['name']}:")
                        for ai, (t, v) in sorted(b['attrs'].items()):
                            if t == 'matrix':
                                p = v['pos']
                                v = f"pos=({p[0]:+.2f}, {p[1]:+.2f}, {p[2]:+.2f})"
                            elif isinstance(v, str) and len(v) > 80:
                                v = v[:80] + '...'
                            print(f"      [{ai:2d}] {t:>10s}  {v}")

    elif args.command == 'havok-info':
        from collections import Counter
        for path in args.input:
            print(f"\n{'='*70}")
            print(f"File: {path}")
            data = open(path, 'rb').read()
            assets, _, _ = extract_str_assets(data)
            hk_assets = [a for a in assets if a.get('resource_type') in ('HKO', 'HKT')]
            if not hk_assets:
                print(f"  No Havok resources found")
                continue

            print(f"  {len(hk_assets)} Havok resources")
            totals = Counter()
            for a in hk_assets[:20]:
                r = parse_havok(a.get('data', b''))
                if not r:
                    continue
                cc = r['class_counts']
                classes_str = ', '.join(f"{n}×{c}" for c, n in sorted(cc.items(), key=lambda x: -x[1])[:3])
                print(f"  {a['filename']:<40s} {r['version']:<20s}  {classes_str}")
                for c, n in cc.items():
                    totals[c] += n
            if len(hk_assets) > 20:
                for a in hk_assets[20:]:
                    r = parse_havok(a.get('data', b''))
                    if r:
                        for c, n in r['class_counts'].items():
                            totals[c] += n

            print(f"\n  Totals across all {len(hk_assets)} Havok files:")
            for c, n in totals.most_common():
                print(f"    {n:5d} × {c}")

    elif args.command == 'cec-info':
        for path in args.input:
            data = open(path, 'rb').read()
            cec = parse_cec(data)
            if not cec:
                print(f"\n{path}: not a valid CEC file")
                continue
            print(f"\n{'='*70}")
            print(f"CEC: {path} ({len(data):,} bytes)")
            print(f"  Version: {cec['version']}")
            print(f"  Config name: {cec['config_name']}")
            print(f"  Source path: {cec['source_path']}")
            print(f"  Actions: {cec['n_actions']}")
            print(f"\n  {'#':>3s}  {'Action':30s} {'Category':12s} {'Flags':>5s} {'Btn':>3s} {'Thr1':>6s} {'Axis':>4s} {'Thr2':>6s}")
            print(f"  {'-'*72}")
            for a in cec['actions']:
                print(f"  {a['index']:3d}  {a['name']:30s} {a['category']:12s} "
                      f"{a['flags']:5d} {a['btn_index']:3d} {a['threshold1']:6d} "
                      f"{a['axis_index']:4d} {a['threshold2']:6d}")

    elif args.command == 'hud-info':
        for path in args.input:
            data = open(path, 'rb').read()
            hud = parse_hud_bin(data)
            if not hud:
                print(f"\n{path}: not a valid hud.bin file")
                continue
            print(f"\n{'='*70}")
            print(f"HUD: {path} ({hud['file_size']:,} bytes)")
            print(f"  Magic: {hud['magic']}")
            print(f"  Resolution configs: {hud['n_configs'] + 1}")
            for i, c in enumerate(hud['configs']):
                print(f"    [{i}] {c['width']}×{c['height_a']} (flags={c['flags']})")
            
            from collections import Counter
            tc = Counter(n['type'] for n in hud['nodes'])
            print(f"\n  Nodes: {hud['n_nodes']} total")
            for t, name in sorted(hud['type_names'].items()):
                print(f"    Type {t} ({name}): {tc.get(t, 0)}")
            
            print(f"\n  Texture atlases ({len(hud['texture_atlases'])}):")
            for a in hud['texture_atlases']:
                print(f"    {a}")
            
            print(f"\n  Sound refs ({len(hud['sounds'])}):")
            for s in hud['sounds']:
                print(f"    {s}")
            
            print(f"\n  Button refs ({len(hud['buttons'])}):")
            for b in hud['buttons']:
                print(f"    {b}")
            
            print(f"\n  Text/localization refs ({len(hud['text_refs'])}):")
            for t in hud['text_refs']:
                print(f"    {t}")
            
            if hasattr(args, 'nodes') and args.nodes:
                print(f"\n  All nodes:")
                for n in hud['nodes']:
                    tname = hud['type_names'].get(n['type'], '?')
                    extra = ''
                    if n['texture_atlas']:
                        extra += f' atlas={n["texture_atlas"]}'
                    print(f"    {n['offset']:#08x} [{tname:9s}] {n['name']}{extra}")


# ============================================================
# CEC (Controller Event Configuration) Parser
# ============================================================
# .cec.XEN files define controller input → game action mappings.
# Fixed 128-byte entries after a variable-length header.

def parse_cec(data):
    """Parse a CEC controller configuration file. Returns dict with header + actions."""
    if len(data) < 0xC5:
        return None
    
    # Header
    version = data[0]
    header_hash = data[1:9].hex()
    
    # Config name (null-terminated at offset 9)
    name_end = data.index(0, 9) if 0 in data[9:40] else 9
    config_name = data[9:name_end].decode('ascii', errors='replace')
    
    # Source path (null-terminated at offset 0x2A)
    path_end = data.index(0, 0x2A) if 0 in data[0x2A:0xBC] else 0x2A
    source_path = data[0x2A:path_end].decode('ascii', errors='replace')
    
    # Pre-entry bytes
    pre_bytes = data[0xBC:0xC5].hex()
    
    # Entries start at 0xC5, stride 128 bytes
    ENTRY_START = 0xC5
    ENTRY_SIZE = 0x80
    
    n_entries = (len(data) - ENTRY_START) // ENTRY_SIZE
    actions = []
    
    for i in range(n_entries):
        off = ENTRY_START + i * ENTRY_SIZE
        entry = data[off:off + ENTRY_SIZE]
        if len(entry) < ENTRY_SIZE:
            break
        
        # Action name (null-terminated, 26 bytes max)
        try:
            name_end_idx = entry.index(0)
        except ValueError:
            name_end_idx = 26
        action_name = entry[:name_end_idx].decode('ascii', errors='replace')
        
        # Category (null-terminated, starts at +0x1A)
        try:
            cat_end = entry.index(0, 0x1A)
        except ValueError:
            cat_end = 0x27
        category = entry[0x1A:cat_end].decode('ascii', errors='replace')
        
        # Verify "!!!!" marker at +0x27
        marker = entry[0x27:0x2B]
        has_marker = marker == b'!!!!'
        
        # Button mapping data after marker
        flags = entry[0x2B]
        btn_index = entry[0x2C]
        threshold1 = struct.unpack_from('>h', entry, 0x2D)[0]
        axis_index = entry[0x2F]
        threshold2 = struct.unpack_from('>h', entry, 0x30)[0]
        
        # Raw data section (for detailed view)
        raw_data = entry[0x2B:0x40].hex()
        
        # Trailing hash (last 5 bytes)
        trail_hash = entry[0x7B:0x80].hex()
        
        actions.append({
            'index': i,
            'name': action_name,
            'category': category,
            'has_marker': has_marker,
            'flags': flags,
            'btn_index': btn_index,
            'threshold1': threshold1,
            'axis_index': axis_index,
            'threshold2': threshold2,
            'raw_data': raw_data,
            'trail_hash': trail_hash,
        })
    
    return {
        'version': version,
        'header_hash': header_hash,
        'config_name': config_name,
        'source_path': source_path,
        'pre_bytes': pre_bytes,
        'n_actions': len(actions),
        'actions': actions,
    }


# ============================================================
# HUD.BIN (HUD Layout Scene Graph) Parser
# ============================================================
# .hud.bin files define the complete HUD layout as a hierarchical
# scene graph. Contains 4 resolution variants (640x320, 1280x640,
# 480x480, 720x720) with nodes for UI elements, health bars,
# power meters, character-specific HUDs, animations, and sounds.

def parse_hud_bin(data):
    """Parse a hud.bin scene graph file. Returns dict with header + node tree."""
    if len(data) < 0x50:
        return None
    
    # Header
    magic = data[:4]
    n_configs = struct.unpack_from('>I', data, 4)[0]
    
    # Resolution configs (16 bytes each)
    configs = []
    off = 8
    for i in range(min(n_configs + 1, 8)):
        if off + 16 > len(data):
            break
        vals = struct.unpack_from('>IIII', data, off)
        configs.append({
            'flags': vals[0],
            'width': vals[1],
            'height_a': vals[2],
            'height_b': vals[3],
        })
        off += 16
    
    # After configs: 8 bytes (padding + total_size)
    if off + 8 <= len(data):
        total_size_field = struct.unpack_from('>I', data, off + 4)[0]
    else:
        total_size_field = 0
    off += 8  # skip to node data
    
    # Scan for nodes using the header pattern:
    # 4-byte ref + 2-byte name_len + 2-byte node_type
    # Name types: 1=container, 2=sprite/texture_ref, 3=leaf/effect
    
    # First, collect all readable strings with their positions
    strings = []
    i = 0
    while i < len(data):
        if 32 <= data[i] < 127:
            end = i
            while end < len(data) and 32 <= data[end] < 127:
                end += 1
            s = data[i:end].decode('ascii', errors='replace')
            if len(s) >= 2 and end < len(data) and data[end] == 0:
                strings.append((i, s))
            i = end + 1
        else:
            i += 1
    
    # Match strings to node headers
    nodes = []
    seen_offsets = set()
    
    for str_off, s in strings:
        if str_off < 8:
            continue
        
        pre8 = data[str_off - 8:str_off]
        name_len = struct.unpack_from('>H', pre8, 4)[0]
        node_type = struct.unpack_from('>H', pre8, 6)[0]
        
        actual_len = len(s) + 1
        header_off = str_off - 8
        
        if (node_type in (1, 2, 3) and 
            name_len >= actual_len and name_len <= actual_len + 16 and
            header_off not in seen_offsets):
            
            ref = struct.unpack_from('>I', pre8, 0)[0]
            seen_offsets.add(header_off)
            
            # Determine what follows the name
            post_off = str_off + name_len
            texture_atlas = None
            text_content = None
            sound_ref = None
            button_ref = None
            
            # For type 1 nodes, look for texture atlas name or text content
            if node_type == 1 and post_off + 20 < len(data):
                # Scan for next string after name
                j = post_off
                while j < min(post_off + 30, len(data)) and data[j] < 32:
                    j += 1
                if j < len(data) and 32 <= data[j] < 127:
                    k = j
                    while k < len(data) and 32 <= data[k] < 127:
                        k += 1
                    next_str = data[j:k].decode('ascii', errors='replace')
                    # Filter: atlas names are identifiers (letters/digits/underscore)
                    # Exclude: $text refs, [button] refs, .emx sounds, float junk
                    if (len(next_str) >= 3 and 
                        next_str[0].isalpha() and
                        all(c.isalnum() or c in '_' for c in next_str) and
                        not next_str.endswith('.emx')):
                        texture_atlas = next_str
            
            # Check if the name itself is a text/localization reference
            if s.startswith('$'):
                text_content = s
            elif s.startswith('[') and s.endswith(']'):
                button_ref = s
            elif s.endswith('.emx'):
                sound_ref = s
            
            # Try to extract position floats
            pos_x = pos_y = scale = None
            # Look for float patterns in the post-name data area
            scan_start = post_off
            scan_end = min(post_off + 60, len(data) - 4)
            for foff in range(scan_start, scan_end, 4):
                fval = struct.unpack_from('>f', data, foff)[0]
                if fval == 1.0:
                    scale = fval
                    break
            
            nodes.append({
                'offset': header_off,
                'ref': ref,
                'name_len': name_len,
                'type': node_type,
                'name': s,
                'texture_atlas': texture_atlas,
                'text_content': text_content,
                'sound_ref': sound_ref,
                'button_ref': button_ref,
            })
    
    # Sort by offset
    nodes.sort(key=lambda n: n['offset'])
    
    # Categorize nodes
    type_names = {1: 'container', 2: 'sprite', 3: 'leaf'}
    
    # Collect unique texture atlases (from type 1 follow-on + type 2 node names)
    KNOWN_ATLASES = {
        'CommonHud', 'CommonHud2', 'BartHud', 'HomerHud', 'LisaHud',
        'MargeHud', 'MargeHud2', 'MaggieHud', 'NQHud', 'BSHFFHud',
        '321Gun', 'Num_1', 'Num_2', 'Num_3', 'event_cloud',
        'sparks', 'subtitleBg', 'thoughtBubble',
    }
    atlases = set()
    for n in nodes:
        if n['texture_atlas']:
            atlases.add(n['texture_atlas'])
        # Type 2 nodes whose names match known atlas patterns
        if n['type'] == 2 and n['name'] in KNOWN_ATLASES:
            atlases.add(n['name'])
    
    # Collect unique sounds
    sounds = set()
    for n in nodes:
        if n['sound_ref']:
            sounds.add(n['sound_ref'])
        if n['name'].endswith('.emx'):
            sounds.add(n['name'])
    
    # Collect button references
    buttons = set()
    for n in nodes:
        if n['button_ref']:
            buttons.add(n['button_ref'])
        if n['name'].startswith('[') and n['name'].endswith(']'):
            buttons.add(n['name'])
    
    # Collect localized text refs
    text_refs = set()
    for n in nodes:
        if n['text_content']:
            text_refs.add(n['text_content'])
        if n['name'].startswith('$'):
            text_refs.add(n['name'])
    
    # Build a simplified tree by identifying major sections
    # (nodes between the 4 resolution variant copies)
    
    return {
        'magic': magic.hex(),
        'n_configs': n_configs,
        'configs': configs,
        'total_size_field': total_size_field,
        'file_size': len(data),
        'n_nodes': len(nodes),
        'nodes': nodes,
        'type_names': type_names,
        'texture_atlases': sorted(atlases),
        'sounds': sorted(sounds),
        'buttons': sorted(buttons),
        'text_refs': sorted(text_refs),
    }


if __name__ == '__main__':
    main()
