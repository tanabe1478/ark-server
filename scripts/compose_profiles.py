"""Conservative relocation of the qualified ASE x86-64 native observation sites.

Addresses are NOT approved by exit code or build number. Four unique normalized
instruction fingerprints must match executable ELF segments, the forced call
must resolve to the ELF's abort@PLT, the startup caller must call THAT function,
and save-begin must call wcslen@PLT. Only address/relative-call operands are
masked; control flow, stack slots and the r14 game-mode convention stay fixed.
A changed compiler/layout fails closed. An update additionally needs two real
independent Saved-copy save/exit/reload cycles before promotion.
"""
import hashlib
import struct

PATTERNS = {
 'save_begin': {'length':72,'delta':19,'prefix':'f2410f1186c00b00000f57c00f298520ffffffbf',
  'zero_ranges':[(20,4),(25,4),(44,4),(63,4),(68,4)],'sha256':'40fb7d1bd9579e76a4f0aa8250d18b166ce7e102047220a9bf83d48363bc3e46'},
 'save_complete': {'length':65,'delta':31,'prefix':'488dbdc0fdffffbe',
  'zero_ranges':[(8,4),(15,4),(27,4),(44,4),(61,4)],'sha256':'202b4066ed4043f0d3dea5c4393d937abc186be71fe7fbcba81c793751db0861'},
 'force_abort': {'length':23,'delta':18,'prefix':'554889e54084ff7509c605',
  'zero_ranges':[(11,4),(19,4)],'sha256':'f8cba82b2e43648b0b8d0c695ff17e028f4fd4ed707fd797a13179f7996fe5d5'},
 'exit_caller': {'length':17,'delta':10,'prefix':'bf01000000e8',
  'zero_ranges':[(6,4),(13,4)],'sha256':'d8e6668eb0bb226e9f9bc4b6f715b3c339420589f882f9e31d6287bf08a26e6b'},
}


def matches(data, pattern):
    prefix = bytes.fromhex(pattern['prefix'])
    start = 0
    result = []
    while True:
        index = data.find(prefix, start)
        if index < 0:
            return result
        start = index + 1
        block = bytearray(data[index:index + pattern['length']])
        if len(block) != pattern['length']:
            continue
        for offset, count in pattern['zero_ranges']:
            block[offset:offset + count] = b'\0' * count
        if hashlib.sha256(block).hexdigest() == pattern['sha256']:
            result.append(index)
        if len(result) > 1000:
            raise ValueError('Ambiguous native fingerprint.')


def derive(path):
    raw = path.read_bytes()
    if (len(raw) > 512 * 1024**2 or raw[:7] != b'\x7fELF\x02\x01\x01'
            or struct.unpack_from('<HH', raw, 16) != (2, 62)):
        raise ValueError('Only bounded, non-PIE x86-64 ELF is supported.')
    phoff, shoff = struct.unpack_from('<QQ', raw, 32)
    phsize, phnum, shsize, shnum, names_index = struct.unpack_from('<HHHHH', raw, 54)
    if phsize != 56 or shsize != 64 or phnum > 128 or shnum > 4096 or names_index >= shnum:
        raise ValueError('Unsupported ELF tables.')
    segments = []
    for index in range(phnum):
        kind, flags, offset, address, physical, size, memory, alignment = struct.unpack_from('<IIQQQQQQ', raw, phoff + index * 56)
        if offset + size > len(raw):
            raise ValueError('Truncated ELF segment.')
        if kind == 1 and flags & 1:
            segments.append((address, raw[offset:offset + size]))
    sections = [struct.unpack_from('<IIQQQQIIQQ', raw, shoff + index * 64) for index in range(shnum)]
    names = sections[names_index]
    strings = raw[names[4]:names[4] + names[5]]
    def text(data, offset):
        end = data.find(b'\0', offset)
        if end < offset:
            raise ValueError('Unterminated ELF string.')
        return data[offset:end].decode('ascii')
    table = {text(strings, section[0]): section for section in sections}
    def contents(section):
        if section[4] + section[5] > len(raw):
            raise ValueError('Truncated ELF section.')
        return raw[section[4]:section[4] + section[5]]
    symbols, dynstrings = contents(table['.dynsym']), contents(table['.dynstr'])
    relocations = contents(table['.rela.plt'])
    got = {}
    for index in range(0, len(relocations), 24):
        offset, info, addend = struct.unpack_from('<QQq', relocations, index)
        name_offset, = struct.unpack_from('<I', symbols, (info >> 32) * 24)
        got[offset] = text(dynstrings, name_offset)
    plt = table['.plt']
    code = contents(plt)
    functions = {}
    for index in range(0, len(code) - 15, 16):
        if code[index:index + 2] == b'\xff\x25':
            displacement, = struct.unpack_from('<i', code, index + 2)
            target = plt[3] + index + 6 + displacement
            if target in got:
                functions[got[target]] = plt[3] + index
    if not {'abort', 'wcslen'} <= functions.keys():
        raise ValueError('Required native PLT symbols are unavailable.')
    def candidates(name):
        return [(base + offset, data[offset:offset + PATTERNS[name]['length']])
                for base, data in segments for offset in matches(data, PATTERNS[name])]
    def call_target(block, base, offset):
        if block[offset] != 0xE8:
            raise ValueError('Expected direct native call.')
        return base + offset + 5 + struct.unpack_from('<i', block, offset + 1)[0]
    aborts = [(base, block) for base, block in candidates('force_abort')
              if call_target(block, base, 18) == functions['abort']]
    begins = [(base, block) for base, block in candidates('save_begin')
              if call_target(block, base, 24) == functions['wcslen']]
    completes = candidates('save_complete')
    if len(aborts) != 1 or len(begins) != 1 or len(completes) != 1:
        raise ValueError('Native save/abort fingerprints are missing or ambiguous.')
    callers = [(base, block) for base, block in candidates('exit_caller')
               if call_target(block, base, 5) == aborts[0][0]]
    if len(callers) != 1:
        raise ValueError('Native exit caller is missing or ambiguous.')
    result = {key: candidate[0] + PATTERNS[key]['delta'] for key, candidate in
              [('save_begin', begins[0]), ('save_complete', completes[0]), ('force_abort', aborts[0]), ('exit_caller', callers[0])]}
    if not 0 < result['save_complete'] - result['save_begin'] < 65536 or len(set(result.values())) != 4:
        raise ValueError('Native save control-flow layout changed.')
    return result


def expected(root, observer_contract):
    from . import ark_shutdown as n
    binary = root / n.BINARY
    return {'schema': 2, 'profile': 'ase-linux-x86_64-relocated-v1', 'binary_sha256': n.digest(binary),
            'observer_contract': observer_contract, 'enabled': True, 'native_points': derive(binary)}
