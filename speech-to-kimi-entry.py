"""speech-to-kimi-code 启动入口。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 speech-to-kimi-code contributors（本文件为 speech-to-kimi-code fork 原创）

项目改名/移动后，.venv 里的 speech-to-speech.exe 启动器内嵌了旧路径会失效，
因此改用本脚本显式调用包的入口函数（等价于原 console_scripts：
speech-to-speech = speech_to_speech.s2s_pipeline:main），命令行参数原样透传。

音频入口门控（fork 原创）：启动前对 LocalAudioStreamer.run 做运行时猴子补丁，
麦克风音频在进入 VAD 之前先过门——voice-bridge/gate.state 为 "1" 时放行真实
音频，为 "0"（或不存在）时改喂静音帧。门控状态由 mic-gate.pyw 原子写文件
维护。这样松开按键立即断音（新声音进不来），而已经在管道里的那句会继续
识别并送达——不需要任何"宽限期"。补丁只替换 run() 一个方法，不动
site-packages，升级上游后对照原函数同步即可（见 docs/upstream-patches.md）。
"""

import logging
import os
import time
from pathlib import Path

import numpy as np

GATE_STATE_FILE = Path(__file__).resolve().parent / "voice-bridge" / "gate.state"

_gate_cache = {"mtime": None, "open": False}


def _gate_open() -> bool:
    """读门控状态文件（mtime 缓存，音频回调每 ~32ms 调一次也几乎零开销）。
    文件不存在或内容异常一律视为关门（静音）。"""
    try:
        mtime = os.path.getmtime(GATE_STATE_FILE)
    except OSError:
        return False
    if mtime != _gate_cache["mtime"]:
        try:
            _gate_cache["open"] = GATE_STATE_FILE.read_text().strip() == "1"
        except OSError:
            _gate_cache["open"] = False
        _gate_cache["mtime"] = mtime
    return _gate_cache["open"]


def _patch_local_audio_streamer() -> None:
    """把 LocalAudioStreamer.run 换成门控分叉版（上游唯一差异：关门喂静音）。"""
    from speech_to_speech.connections.local_audio_streamer import LocalAudioStreamer
    from speech_to_speech.pipeline.messages import AUDIO_RESPONSE_DONE

    logger = logging.getLogger("speech_to_speech.connections.local_audio_streamer")

    def run(self) -> None:
        import sounddevice as sd

        # Pre-generate a static dither buffer (±1 LSB, -96 dB) to keep the
        # audio sink active without calling numpy inside the real-time callback.
        dither = np.random.randint(-1, 2, size=(self.list_play_chunk_size, 1), dtype=np.int16)
        silence = np.zeros((self.list_play_chunk_size, 1), dtype=np.int16)

        def callback(indata: np.ndarray, outdata: np.ndarray, frames: int, time_: float, status: str) -> None:
            # During shutdown, just output silence
            if self.stop_event.is_set():
                outdata[:] = 0 * outdata
                return

            if self.output_queue.empty():
                # —— 门控（fork 新增）：关门时喂静音帧，VAD 收不到任何声音 ——
                pcm = np.ascontiguousarray(
                    indata if _gate_open() else silence, dtype=np.int16)
                self.input_queue.put(pcm.tobytes())
                outdata[:] = dither
            else:
                try:
                    audio_chunk = self.output_queue.get_nowait()
                    if isinstance(audio_chunk, np.ndarray):
                        outdata[:] = audio_chunk[:, np.newaxis]
                    elif audio_chunk == AUDIO_RESPONSE_DONE:
                        self.should_listen.set()
                        logger.debug("Response complete, listening re-enabled")
                        outdata[:] = 0 * outdata
                    else:
                        outdata[:] = 0 * outdata
                except Exception:
                    outdata[:] = 0 * outdata

        logger.debug("Available devices:")
        logger.debug(sd.query_devices())
        with sd.Stream(
            samplerate=16000,
            dtype="int16",
            channels=1,
            callback=callback,
            blocksize=self.list_play_chunk_size,
        ):
            logger.info("Starting local audio stream (audio-gated)")
            while not self.stop_event.is_set():
                time.sleep(0.001)
            print("Stopping recording")

    LocalAudioStreamer.run = run


_patch_local_audio_streamer()

from speech_to_speech.s2s_pipeline import main

if __name__ == "__main__":
    main()
