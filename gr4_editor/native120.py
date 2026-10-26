"""Render current authored native sources against explicitly reviewed bindings.

Generated C/headers live in a build directory; there is one maintained source.
Unreviewed address candidates cannot silently enter an ARM build. Structural
field/owner offsets require separate, explicit call-site overrides.
"""
from hashlib import sha256
from pathlib import Path
import re


TOKENS = re.compile(r'/\*[\s\S]*?\*/|//[^\n]*|(?<![\w])0x[0-9a-fA-F]+[uUlL]*(?![\w])')


def render_native_text(text, bindings, *, literal_overrides=None):
    overrides = dict(literal_overrides or {})
    used = {}

    def replace(match):
        token = match.group()
        if token.startswith('/'):
            return token
        number = re.match(r'0x[0-9a-fA-F]+', token).group()
        original = int(number, 16)
        if original in overrides:
            translated = overrides[original]
            reason = 'explicit structural binding'
        elif 0x53000000 <= original < 0x56000000:
            translated = bindings.resolve(original)
            reason = 'reviewed address binding'
        else:
            return token
        if type(translated) is not int or not 0 <= translated <= 0xFFFFFFFF:
            raise ValueError('native binding is outside a 32-bit field')
        used[original] = {'old': hex(original), 'new': hex(translated), 'kind': reason}
        return hex(translated) + token[len(number):]

    rendered = TOKENS.sub(replace, text)
    return rendered, list(used.values())


def render_native_sources(sources, directory, bindings, *, literal_overrides=None):
    """Copy/render current sources and their local headers into an output folder."""
    sources = [Path(p).resolve() for p in sources]
    directory = Path(directory).resolve()
    if any(p.parent == directory for p in sources):
        raise ValueError('generated sources cannot overwrite maintained sources')
    files = {p.name: p for p in sources}
    for parent in {p.parent for p in sources}:
        for path in parent.glob('*.h'):
            previous = files.get(path.name)
            if previous is not None and previous != path:
                raise ValueError('native header filename collision')
            files[path.name] = path
    # Finish all binding checks before writing compiler inputs.
    generated, receipts = {}, {}
    for name, path in files.items():
        original = path.read_text(encoding='utf-8')
        rendered, used = render_native_text(original, bindings, literal_overrides=literal_overrides)
        generated[name] = rendered
        receipts[name] = {'source_path': str(path),
            'source_sha256': sha256(path.read_bytes()).hexdigest(),
            'rendered_sha256': sha256(rendered.encode('utf-8')).hexdigest(), 'bindings': used}
    directory.mkdir(parents=True, exist_ok=True)
    for name, rendered in generated.items():
        (directory / name).write_text(rendered, encoding='utf-8')
    return [directory / p.name for p in sources], receipts
