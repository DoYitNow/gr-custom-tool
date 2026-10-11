"""Gamma node/increment and 256-sample composition codec."""
import struct


def increment_word(delta):
    delta = max(0, min(240, int(delta)))
    nibbles = [0] + [(k * delta) // 16 - ((k - 1) * delta) // 16
                     for k in range(1, 16)]
    return sum(n << (4 * k) for k, n in enumerate(nibbles))

def encode_gamma(samples, transform=None):
    if len(samples) != 256:
        raise ValueError('Gamma stage requires 256 input samples')
    samples = [int(x) for x in samples]
    if any(x < 0 or x > 65535 for x in samples):
        raise ValueError('samples must fit u16')
    if transform is not None:
        if any(x >= len(transform) for x in samples):
            raise ValueError('sample outside original transform table')
        samples = [transform[x] for x in samples]
    nodes = [min(x >> 4, 1023) for x in samples]
    words = [increment_word(nodes[k + 1] - nodes[k]) for k in range(255)] + [0]
    return struct.pack('<256H', *nodes), struct.pack('<256Q', *words)

def sample_gamma_source(source, coordinate):
    """Original 0x5391E6A0 interpolation: 64-unit knots, final-knot clamp.

    Coordinate is an internal u16 value, not a proven camera pixel/sRGB value.
    """
    index, fraction = divmod(int(coordinate), 64)
    if index >= 255:
        return int(source[255])
    product = (int(source[index + 1]) - int(source[index])) * fraction
    # ARM adjusts negative products before ASR: division truncates toward zero.
    step = product // 64 if product >= 0 else -((-product) // 64)
    return (int(source[index]) + step) & 65535

def compose_gamma_sources(base, sources, source_weight=3):
    """Recovered enabled-source branch of ARM 0x5391E340.

    Returns three loaded sources and four composed intermediate curves.
    source_weight is 1/2/3; the descriptor flag/control selects this weight.
    No claim of hardware RGB order, exposure, or input/output color space.
    """
    if len(base) != 256 or len(sources) != 3 or any(len(s) != 256 for s in sources):
        raise ValueError('requires a 256-entry base and three 256-entry sources')
    if source_weight not in (1, 2, 3):
        raise ValueError('source_weight must be 1, 2 or 3')
    values = [int(x) for s in [base, *sources] for x in s]
    if any(x < 0 or x > 65535 for x in values):
        raise ValueError('all values must fit u16')
    loaded = [
        [(source_weight * int(sources[c][i]) + (3 - source_weight) * int(sources[1][i])) // 3
         for i in range(256)] if c != 1 else [int(x) for x in sources[1]]
        for c in range(3)]
    composed = [[sample_gamma_source(s, x) for x in base] for s in loaded]
    composed.append([(77 * a + 150 * b + 29 * c) >> 8
                     for a, b, c in zip(*composed)])
    return loaded, composed
