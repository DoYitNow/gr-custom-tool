"""Fixed-point color matrix arithmetic for imported resources."""


def signed(value, bits):
    value = int(value) & ((1 << bits) - 1)
    return value - (1 << bits) if value & (1 << (bits - 1)) else value

def trunc_div(value, denominator):
    return value // denominator if value >= 0 else -((-value) // denominator)

def compose_matrices(first, second):
    """Original 0x53914DA4: row-major first @ second, biased Q13 rounding."""
    if len(first) != 9 or len(second) != 9:
        raise ValueError('requires two 3x3 matrices')
    out = []
    for row in range(3):
        for column in range(3):
            total = signed(sum(signed(first[row*3+k], 16) * signed(second[k*3+column], 16)
                               for k in range(3)), 32)
            out.append(trunc_div(signed(total + 4096, 32), 8192))
    return out

def main_coefficients(first, second, saturation_factor, scale=256, preserve_rows=True):
    """Original 0x53914E20 main coefficient arithmetic and signed-12 clamp.

    saturation_factor is the original table value, not the UI setting index.
    preserve_rows corresponds to descriptor+79 == 0.
    """
    combined = compose_matrices(first, second)
    strength = min(signed(int(saturation_factor) * int(scale), 32) >> 8, 8192) & 65535
    values = [signed(trunc_div(signed(v * strength, 32), 4096), 16) for v in combined]
    if preserve_rows:
        for row in range(3):
            diagonal = row*3+row
            values[diagonal] = signed(2*strength - sum(values[row*3+c] for c in range(3) if c != row), 16)
    return [max(-2048, min(2047, v)) for v in values]
