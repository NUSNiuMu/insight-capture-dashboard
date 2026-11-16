"""Bounded-memory ZIP delivery of one recovered Take, without temporary copies."""
from zipfile import ZIP_STORED, ZipFile, ZipInfo


class _Chunks:
    def __init__(self):
        self.offset = 0
        self.pending = []

    def write(self, data):
        self.offset += len(data)
        if data:
            self.pending.append(data)
        return len(data)

    def tell(self):
        return self.offset

    def flush(self):
        pass

    def drain(self):
        pending, self.pending = self.pending, []
        return pending


def recovered_bundle(files, *, chunk_size=1024 * 1024):
    """Keep original filenames together under a unique Take directory."""
    sink = _Chunks()
    with ZipFile(sink, 'w', compression=ZIP_STORED, allowZip64=True) as archive:
        for path in files:
            # DOS archive dates have narrower bounds than filesystem timestamps.
            info = ZipInfo.from_file(
                path, arcname=path.parent.name + '/' + path.name, strict_timestamps=False,
            )
            with path.open('rb') as source, archive.open(info, 'w', force_zip64=True) as target:
                yield from sink.drain()
                while data := source.read(chunk_size):
                    target.write(data)
                    yield from sink.drain()
            yield from sink.drain()
    yield from sink.drain()
