"""SenseVoice decoding helper used by the host voice service."""


def decode(recognizer, sample_rate: int, samples) -> str:
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, samples)
    recognizer.decode_stream(stream)
    return stream.result.text
