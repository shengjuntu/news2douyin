"""Isolated TTS subprocess; parent controls cancellation and time limits."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from pathlib import Path
import sys
import wave


def espeak_to_wav(text, output, voice='cmn', rate=170):
    # Public ABI: https://github.com/espeak-ng/espeak-ng/blob/master/src/include/espeak-ng/speak_lib.h
    import espeakng_loader
    lib = espeakng_loader.load_library()
    if lib is None:
        raise RuntimeError('无法加载 eSpeak NG 动态库')
    lib.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.espeak_Initialize.restype = ctypes.c_int
    sample_rate = lib.espeak_Initialize(2, 0, str(Path(espeakng_loader.get_data_path()).parent).encode(), 0x8000)
    if sample_rate <= 0:
        raise RuntimeError('eSpeak NG 初始化失败')
    callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)
    audio = bytearray()
    @callback_type
    def receive(samples, count, events):
        if samples and count > 0:
            audio.extend(ctypes.string_at(samples, count * 2))
        return 0
    lib.espeak_SetSynthCallback.argtypes = [callback_type]
    lib.espeak_SetSynthCallback(receive)
    lib.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
    lib.espeak_SetParameter.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.espeak_Synth.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_int,
                                ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    try:
        if lib.espeak_SetVoiceByName(voice.encode()) != 0:
            raise ValueError('eSpeak NG 语音不存在: ' + voice)
        lib.espeak_SetParameter(1, rate, 0)
        data = text.encode('utf-8') + b'\0'
        if lib.espeak_Synth(data, len(data), 0, 1, 0, 1 | 0x1000, None, None) != 0:
            raise RuntimeError('eSpeak NG 合成失败')
        lib.espeak_Synchronize()
        if not audio:
            raise RuntimeError('配音为空')
        if sys.byteorder != 'little':
            import array
            pcm = array.array('h', audio)
            pcm.byteswap()
            audio = pcm.tobytes()
        with wave.open(str(output), 'wb') as wav:
            wav.setparams((1, 2, sample_rate, 0, 'NONE', 'not compressed'))
            wav.writeframes(audio)
    finally:
        lib.espeak_Terminate()


async def edge_to_file(text, output, voice, rate):
    import edge_tts
    await edge_tts.Communicate(text=text, voice=voice, rate=f'{rate:+d}%',
                              connect_timeout=10, receive_timeout=60).save(str(output))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', choices=['espeak', 'edge'], required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--voice', required=True)
    parser.add_argument('--rate', type=int, default=0)
    args = parser.parse_args()
    text = Path(args.input).read_text(encoding='utf-8')
    if args.backend == 'espeak':
        espeak_to_wav(text, Path(args.output), args.voice, round(170 * (1 + args.rate / 100)))
    else:
        asyncio.run(edge_to_file(text, Path(args.output), args.voice, args.rate))


if __name__ == '__main__':
    main()
