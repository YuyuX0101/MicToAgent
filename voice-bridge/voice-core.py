"""voice-core：MicToAgent 自研语音核心。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors（本文件为 MicToAgent 原创）

数据流：麦克风 → 门控（gate.state="0" 直接丢弃音频）→ VAD 分段
（ten-vad/onnxruntime）→ sherpa-onnx 增量识别（core.stt_engine 选择：
"dual" 默认双引擎接力 / "qwen3_asr" 单引擎）→ 热词文本纠错 → 宽限合并、
片段按序拼接 → POST voice-bridge 适配器。

切段机制：每次软切只把"上次切点到现在"的音频作为一个片段（chunk）送识别，
结果按序暂存；宽限到期或 flush 时等本轮所有片段到齐，按序拼接一次提交。
任何一轮的最后等待 = 最后一个片段的识别耗时。

双引擎接力（core.stt_engine="dual"，默认）：每个片段先送 SenseVoice 出
初稿（快，RTF≈0.03）；宽限内续话确认/下次软切时，给已关闭的前序片段排
qwen3-asr 精修任务，精修结果覆盖内部暂存文本（不可见，不动已提交内容）。
提交屏障只等初稿——宽限到期/松手即发，不等在途精修（迟到的精修按死
turn 丢弃）。净效果：除最后一片外多为 qwen3 质量，最后一片永远是
SenseVoice 初稿，提交零等待。qwen3 精修有片段长度上限
（core.refine_max_sec，默认 25s）：音频按 13 token/秒进
max_total_len=512 的 KV，超限 sherpa 截断音频、生成退化文本（实测 40s
片段输出只剩 "language"）；超限片段和疑似退化的精修结果都保留初稿。

线程模型：
  [音频回调]  只把帧丢进队列（关门时直接丢弃），不做任何重活
  [主循环]    逐帧过 VAD、跑回合状态机（软切/宽限/flush）、决定何时提交
  [识别线程]  片段按序全做（初稿 SenseVoice / 精修 qwen3-asr + 热词纠错），
              结果经队列回主循环
  [发送线程]  串行 POST 适配器：一次请求一次发送，超时不重发
              （语音提交非幂等，重发 = 目标会话收到重复消息）；
              POST 本身失败（连不上适配器 / 非 2xx）弹 Windows 通知，
              预热文本除外。kimi web / 未选中会话等由适配器自己通知。

进程间接口（与 mic-gate / adapter / launcher 的契约，不可变）：
  gate.state   "0"=关门 "1"=常听 "2"=按住说话（mic-gate 原子写，本核心读）
  speak.state  检出真实语音时 touch mtime，150ms 节流；只碰不删（mic-gate 读）
  POST {listen_host}:{listen_port}/v1/chat/completions
               OpenAI 兼容格式，adapter 只取最后一条 role=user 的文本
  启动末尾发送 config["warmup_text"]（adapter 收到后置 /ready）
  日志锚点（launcher 进度条探测 err.log 的子串，不可改）：
    "[stt] loading engine=" / "[stt] engine ready"（sherpa 引擎的加载/就绪锚点）
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

logger = logging.getLogger("voice-core")

from envutil import bridge_dir, models_dir, project_root

BASE = bridge_dir()                             # voice-bridge 目录
PROJ = project_root()                           # 项目根（.venv / logs 所在）
sys.path.insert(0, str(BASE))                   # vendor/ 包

CONFIG_PATH = BASE / "config.json"
GATE_STATE_FILE = BASE / "gate.state"
SPEAK_STATE_FILE = BASE / "speak.state"
HOTWORD_FILE = BASE / "hotwords.txt"

# sherpa-onnx 模型目录（可用 MICTOAGENT_MODELS 覆盖；输入必须 16k float32，
# 麦克风采集本身就是 16k，无需重采样——qwen3 喂 44.1k 会严重退化）
_MODELS = models_dir()
QWEN3_ASR_DIR = _MODELS / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
SENSE_VOICE_DIR = _MODELS / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

# TEN-VAD 模型（sherpa-onnx 版，带 metadata；官方原版缺 mean/inv_stddev/window
# 元数据无法复刻特征管线。许可：修改版 Apache 2.0，见 models/README.md）
TEN_VAD_MODEL = _MODELS / "ten-vad.onnx"

SAMPLE_RATE = 16000
BLOCKSIZE = 512                                  # 32ms / 帧
FRAME_MS = 1000 * BLOCKSIZE // SAMPLE_RATE       # 32

# 说话状态刷新节流：VAD 每 32ms 一帧，文件 mtime 刷新 150ms 一次足够
SPEAK_TOUCH_INTERVAL_S = 0.15

# core 配置默认值
CORE_DEFAULTS = {
    "stt_engine": "dual",            # dual=SV初稿+qwen3精修接力；qwen3_asr=单引擎
    "stt_num_threads": 4,
    "vad_threshold": 0.5,            # ten-vad 标定（spike 实测：静音底 0.17，语音体 0.5-0.9）
    "min_silence_ms": 300,
    "min_speech_ms": 384,
    "min_speech_continuation_ms": 192,
    "speech_pad_ms": 500,
    "reopen_grace_ms": 1200,
    "refine_max_sec": 25,          # qwen3 精修片段时长上限（音频 13 token/秒，超 ~30s 必超 max_total_len=512）
    "filler_words": ["呃"],        # 语气词清理词表（纯规则删除 + 口吃折叠）；嗯/啊/那个/就是语义风险高，别轻易加
    "post_timeout_sec": 120,
}


def load_config(path=CONFIG_PATH) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    core = dict(CORE_DEFAULTS)
    core.update(cfg.get("core") or {})
    core.pop("stt_stitch", None)     # 已移除的回退开关，忽略配置里的残留
    cfg["core"] = core
    return cfg


def int2float(sound: np.ndarray) -> np.ndarray:
    """int16 → float32（VAD/识别模型的输入格式）。"""
    abs_max = np.abs(sound).max()
    sound = sound.astype("float32")
    if abs_max > 0:
        sound *= 1 / 32768
    return sound.squeeze()


# ---------- 门控与说话信号（voice-bridge 文件契约） ----------

class GateWatcher:
    """读门控状态文件（mtime 缓存，音频回调每 32ms 调一次也几乎零开销）。
    返回 "0"=关门 / "1"=常听 / "2"=按住说话；文件不存在或内容异常一律视为关门。"""

    def __init__(self, path=GATE_STATE_FILE):
        self.path = Path(path)
        self._mtime = None
        self._value = "0"

    def value(self) -> str:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return "0"
        if mtime != self._mtime:
            try:
                value = self.path.read_text().strip()
                self._value = value if value in ("1", "2") else "0"
            except OSError:
                self._value = "0"
            self._mtime = mtime
        return self._value


class SpeakReporter:
    """托盘说话动画的信号源：VAD 处于语音段时刷新 speak.state 的 mtime
    （节流 150ms）。文件只碰不删——mic-gate 以 mtime 新鲜度（<350ms）
    判断说话状态，静音/关门/退出都会让 mtime 自然过期，无需清理。"""

    def __init__(self, path=SPEAK_STATE_FILE, clock=time.monotonic):
        self.path = Path(path)
        self._clock = clock
        self._last = float("-inf")

    def report(self) -> None:
        now = self._clock()
        if now - self._last < SPEAK_TOUCH_INTERVAL_S:
            return
        self._last = now
        try:
            self.path.touch()
        except OSError:
            pass


# ---------- 热词表解析与识别后文本纠错 ----------

_EXPLICIT_SEPARATORS = ("=>", "->", "→")
_CJK_FULLMATCH = re.compile(r"[一-鿿]+")
_CJK_ADJACENT_SPACE = re.compile(r"\s+(?=[一-鿿])|(?<=[一-鿿])\s+")


def normalize_stt_text(text: str) -> str:
    """识别文本归一化：只去掉与 CJK 相邻的空格，英文词间空格保留。"""
    return _CJK_ADJACENT_SPACE.sub("", text.strip())


def parse_hotword_files(paths: list) -> tuple[dict, list]:
    """解析热词表文件，返回 (显式纠错映射, 同音目标词)。

    每行一条：
      错误词=>目标词   显式替换（识别结果里出现"错误词"就直接换成"目标词"）
      目标词           同音目标（拼音完全相同的误听会被纠正为目标词，仅限纯中文词）

    含英文的词两种机制都不适用：拼音键会丢掉英文字母（纯英文词拼音键为空、
    中英混杂词会和自身子串碰撞——实测「AI日报」把「I日报」替换成「AI日报」
    变成「AAI日报」），英文误听只能用「错误词=>目标词」显式映射逐条补充。
    """
    explicit: dict = {}
    fuzzy: list = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as exc:
            logger.warning("热词表文件不可读，跳过: %s (%s)", path, exc)
            continue
        for line in lines:
            word = line.strip()
            if not word or word.startswith("#"):
                continue
            mapped = False
            for sep in _EXPLICIT_SEPARATORS:
                if sep in word:
                    wrong, right = (part.strip() for part in word.split(sep, 1))
                    if wrong and right:
                        explicit[wrong] = right
                        mapped = True
                    break
            if not mapped and _CJK_FULLMATCH.fullmatch(word):
                fuzzy.append(word)
    return explicit, fuzzy


class HotwordCorrector:
    """识别后文本纠错（显式映射 + 拼音同音纠正），词表文件 mtime 变了自动重建
    （改完 hotwords.txt 不用重启，下一次识别即生效）。

    阈值固定 1.0（纯同音才替换）：模糊窗口允许目标长度 ±1，阈值放低会误改
    （实测「区下」被改成「取消」、「的小说」被改成「小说」）。拼音按音节
    加分隔符比对，音节结构不同的词不算同音（连 lian ≠ 立案 li-an）。
    阈值 1.0 下 matcher 走拼音键字典查表（精确路径），纠错耗时与词表
    规模脱钩（实测 214→2000 词均 ~80ms/次，192 字文本），词表随便涨。
    matcher 用的是 vendor 进来的 FunASR PostprocessHotwordMatcher（MIT），
    不再依赖 funasr 内部 API。"""

    def __init__(self, paths):
        self._paths = [str(p) for p in paths]
        self._sig = None
        self._matcher = None

    def reload_if_changed(self) -> None:
        if not self._paths:
            return
        sig = tuple(os.path.getmtime(p) if os.path.isfile(p) else None
                    for p in self._paths)
        if sig == self._sig:
            return
        explicit, fuzzy = parse_hotword_files(self._paths)
        if not explicit and not fuzzy:
            self._matcher = None
        else:
            try:
                from vendor.postprocess_hotwords import PostprocessHotwordMatcher
            except ImportError as exc:
                logger.warning("热词纠错不可用（缺依赖: %s）", exc)
                self._sig = sig
                return
            self._matcher = PostprocessHotwordMatcher(
                explicit_map=explicit, fuzzy_targets=fuzzy, threshold=1.0)
        self._sig = sig
        logger.info("热词表已加载: %d 条显式映射, %d 个同音目标",
                    len(explicit), len(fuzzy))

    def apply(self, text: str) -> str:
        if self._matcher is None or not text:
            return text
        corrected, matches = self._matcher.apply_text(text)
        if matches:
            logger.info("热词纠错: %s -> %s (%s)", text, corrected,
                        [(m.original, m.replacement) for m in matches])
        return corrected


# ---------- 语气词与口吃清理 ----------

def _cleanup_fillers(text: str, fillers) -> str:
    """去掉语气词（默认只删「呃」）和口吃重复，纯规则无模型。

    两段式：先折叠口吃重复（同一 1~4 字片段被重复且中间夹了语气词，或
    ≥2 字的片段连排 ≥3 次——「通过通过呃通过」→「通过」；纯相邻 2 连
    不动，「谢谢」「研究研究」是合法叠词），再删残留语气词并收拾它
    留下的标点（「。呃，」→「。」）。"""
    if not text or not fillers:
        return text
    alt = "|".join(re.escape(f) for f in fillers if f)
    if not alt:
        return text
    text = _collapse_stutters(text, alt)
    # 语气词连同紧随的停顿标点一起删
    text = re.sub(r"(?:%s)+[，。、！？…\s]*" % alt, "", text)
    # 收拾残留：逗号紧贴句号类标点 → 留句号；连续逗号合并；句首标点删掉
    text = re.sub(r"[，、]([。！？…])", r"\1", text)
    text = re.sub(r"([。！？…])[，、]", r"\1", text)
    text = re.sub(r"[，、]{2,}", "，", text)
    return text.lstrip("，。、！？… ")


def _collapse_stutters(text: str, filler_alt: str) -> str:
    sep = r"(?:%s|[，、\s])*" % filler_alt
    pat = re.compile(r"([一-鿿]{1,4})((?:%s)\1)+" % sep)

    def repl(m):
        token, run = m.group(1), m.group(0)
        if re.fullmatch(r"(?:%s)+" % filler_alt, token):
            return run  # 语气词自身连排（呃呃呃）交给语气词删除
        seps = run.split(token)
        count = len(seps) - 1
        has_filler = any(re.search(filler_alt, s) for s in seps)
        if has_filler or (count >= 3 and len(token) >= 2):
            return token
        return run

    return pat.sub(repl, text)


# ---------- VAD ----------

_TEN_VAD_HOP = 256          # ten-vad 一跳 256 样本（16ms @16k）
_TEN_VAD_FFT = 1024         # 特征帧长 64ms（16k * 64ms）
_TEN_VAD_LOG_SCALE = 20.79441541679836   # log(32768^2)，sherpa 实现的固定偏置


class TenVadModel:
    """TEN-VAD（sherpa-onnx 版 ten-vad.onnx）逐跳概率，onnxruntime 推理。

    特征管线逐条复刻 sherpa-onnx csrc/ten-vad-model.cc 的 ComputeFeatures：
    缩放 32768 → 0.97 预加重（跨跳携带末样本）→ 加窗（模型 metadata 的
    window 前 n）→ 1024 点 rfft 功率谱 → knf MelBanks（librosa/slaney/
    40bin/0-8k/floor_to_int_bin）→ log(x+1e-10)-log(32768^2) → 第 41 维
    pitch 置 0（sherpa 的简化）→ (x-mean)*inv_stddev → 滚动 3 帧拼 (3,41)。
    4 个 (1,64) 隐藏状态跨跳回传。"""

    def __init__(self, model_path=TEN_VAD_MODEL):
        import onnxruntime as ort
        self._sess = ort.InferenceSession(str(model_path),
                                          providers=["CPUExecutionProvider"])
        meta = self._sess.get_modelmeta().custom_metadata_map
        if meta.get("model_type") != "ten-vad":
            raise RuntimeError(
                f"{model_path} 缺少 ten-vad metadata，需要用 sherpa-onnx 版模型"
                "（https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models）")
        self._mean = np.fromstring(meta["mean"], sep=",", dtype=np.float32)
        self._inv_std = np.fromstring(meta["inv_stddev"], sep=",",
                                      dtype=np.float32)
        self._window = np.fromstring(meta["window"], sep=",", dtype=np.float32)
        if self._mean.size != 41 or self._window.size != 768:
            raise RuntimeError(f"{model_path} metadata 维度异常")

        import kaldi_native_fbank as knf
        frame_opts = knf.FrameExtractionOptions()
        frame_opts.frame_length_ms = 64          # 16k * 64ms = 1024 = num_fft
        mel_opts = knf.MelBanksOptions()
        mel_opts.is_librosa = True
        mel_opts.norm = ""
        mel_opts.use_slaney_mel_scale = True
        mel_opts.floor_to_int_bin = True
        mel_opts.low_freq = 0
        mel_opts.high_freq = 8000
        mel_opts.num_bins = 40
        self._mel = knf.MelBanks(mel_opts, frame_opts, 1.0)

        self._inputs = [i.name for i in self._sess.get_inputs()]
        self.reset()

    def reset(self) -> None:
        self._last_sample = 0.0
        self._feat_hist = np.zeros((3, 41), dtype=np.float32)
        self._states = [np.zeros((1, 64), dtype=np.float32) for _ in range(4)]

    def hop(self, samples: np.ndarray) -> float:
        """一跳（≤256 样本 float32 [-1,1]）→ 语音概率。"""
        buf = np.zeros(_TEN_VAD_FFT, dtype=np.float32)
        n = len(samples)
        buf[:n] = samples * 32768.0
        raw_last = float(buf[n - 1]) if n else self._last_sample
        # 预加重；跨跳携带的是缩放后、预加重前的末样本（与 sherpa 一致）
        prev = np.concatenate([[self._last_sample], buf[:n - 1]])
        buf[:n] = buf[:n] - 0.97 * prev
        self._last_sample = raw_last
        buf[:n] *= self._window[:n]
        power = (np.abs(np.fft.rfft(buf)) ** 2).astype(np.float32)
        feats = np.empty(41, dtype=np.float32)
        feats[:40] = np.log(self._mel.compute(power) + 1e-10) - _TEN_VAD_LOG_SCALE
        feats[40] = 0.0                    # pitch 置 0（sherpa 简化）
        feats = (feats - self._mean) * self._inv_std
        self._feat_hist = np.vstack([self._feat_hist[1:], feats[None, :]])
        feed = {self._inputs[0]: self._feat_hist[None, :, :].copy()}
        for name, st in zip(self._inputs[1:], self._states):
            feed[name] = st
        outs = self._sess.run(None, feed)
        self._states = list(outs[1:])      # 隐藏状态跨跳保持
        return float(outs[0].ravel()[0])

    def __call__(self, frame_f32: np.ndarray) -> float:
        """一个 512 样本块 = 两跳。块概率取两跳的 max：
        实测静音底稳定在 0.16-0.19（距阈值 0.5 很远，max 不会误触发），
        而语音体内偶有单跳跌落（合成语料最低 0.30），取 max 防漏检更保守。"""
        p = 0.0
        for i in range(0, len(frame_f32), _TEN_VAD_HOP):
            p = max(p, self.hop(frame_f32[i:i + _TEN_VAD_HOP]))
        return p


class VadEngine:
    """逐帧 VAD 判定（32ms 帧 @16kHz；ten-vad/onnxruntime 后端）。

    判定规则：概率 ≥ threshold 触发段开始；概率 ≥ threshold-0.15（含死区）
    算有效语音并重置静音计时；连续低于 threshold-0.15 满 min_silence 判段
    结束（返回 "end"）。

    每帧可读状态：triggered（正处于语音段）、speech_frame（本帧概率过阈值）、
    active_frame（本帧算有效语音，≥ threshold-0.15，供 min_speech 过滤计数）。

    model 注入约定：可调用对象，接收一帧 float32 返回概率（测试喂脚本化概率）。
    """

    def __init__(self, model=None, *, threshold=0.5,
                 min_silence_ms=500, sample_rate=SAMPLE_RATE):
        if model is None:
            model = TenVadModel()
        self._model = model
        self.threshold = threshold
        self._neg_threshold = threshold - 0.15
        self.sample_rate = sample_rate
        self.min_silence_samples = sample_rate * min_silence_ms // 1000
        self.triggered = False
        self.speech_frame = False
        self.active_frame = False
        self._temp_end = 0
        self._sample = 0

    def process(self, frame_f32: np.ndarray) -> str | None:
        """逐帧判定：
        - 概率 ≥ threshold：语音帧；未触发则立即触发（段开始）；
        - 概率 ≥ threshold-0.15（含死区）：算有效语音并重置静音计时；
        - 概率 < threshold-0.15：连续静音 ≥ min_silence 判段结束（"end"）。
        """
        self._sample += len(frame_f32)
        prob = float(self._model(frame_f32))
        self.speech_frame = prob >= self.threshold
        self.active_frame = False
        if self.speech_frame and not self.triggered:
            self.triggered = True
        if self.triggered:
            if prob >= self._neg_threshold:
                self.active_frame = True
                self._temp_end = 0
            else:
                if not self._temp_end:
                    self._temp_end = self._sample
                if self._sample - self._temp_end >= self.min_silence_samples:
                    self._temp_end = 0
                    self.triggered = False
                    return "end"
        return None


# ---------- 分段器（回合状态机） ----------

class Segmenter:
    """软切 + 宽限合并 + flush，增量识别按序拼接。

    一个"回合"（turn_id）对应最终提交给适配器的一条文本：
    - 检出语音开始累积；连续静音 ≥ min_silence（默认 300ms）软切：把"上次切点
      到现在"的音频作为一个片段（chunk）送识别线程，结果按序暂存，不再丢弃；
    - 宽限（reopen_grace，默认 1200ms）从话音结束算起、覆盖软切停顿期：
      停顿满 min_silence 触发软切时宽限已消耗 min_silence，再静默
      grace-min_silence 没重新开口即提交（话音结束到提交的总停顿 = grace）；
      宽限内重新开口：暂存的前缀文本保留，从切点继续累积新片段，
      宽限撤销、下次软切重新计时；
    - 宽限期满且本轮全部片段识别完成：按序拼接成完整文本一次提交（提交屏障，
      提交时机 = max(宽限期满, 最后一个片段识别完成)；识别大多与宽限窗口重叠，
      正常等待只有最后一个片段的零点几秒）；
    - gate "2"（按住说话）：切分识别与提交判定解耦——仍按 min_silence 软切
      送识别（按住说 30s 也只需等最后一个片段），但绝不自动提交，
      提交只在松手/关门 flush 时发生；
    - flush（离开 "2" 或关门 →"0"）：在途语音立即软切并跳过宽限，
      等本轮片段到齐后拼接即发；flush 只影响在途那一轮，之后恢复常听节奏；
    - 整轮作废：回合关闭后迟到的识别结果按 turn_id 丢弃，识别队列里该轮
      未做的任务由 purge 清理。

    dual 双引擎接力（dual=True）：片段软切先送初稿任务（draft，SenseVoice）；
    宽限内续话确认时给全部已关闭片段、下次软切时给除刚切片段外的已关闭
    片段排精修任务（refine，qwen3）——刚切的这片是不是最后一片还不知道，
    等续话确认再补。精修结果只覆盖暂存文本，提交屏障（_all_chunks_done）
    只等初稿，不等在途精修。
    qwen3 精修有长度上限（refine_max_sec，默认 25s）：音频按 13 token/秒
    编码进 max_total_len=512 的 KV，超限 sherpa 会截断音频、生成退化文本
    （实测 40s 片段输出只剩 "language" 一个词）；超限片段不排精修，
    保留 SenseVoice 初稿。精修文本异常短于初稿（<50%）也视为退化，不覆盖。
    """

    def __init__(self, gate_value, speak, vad, submit_recog, send, *,
                 purge_recog=None, dual=False,
                 min_speech_ms=384, min_speech_continuation_ms=192,
                 speech_pad_ms=500, reopen_grace_ms=1500, min_silence_ms=300,
                 refine_max_sec=25,
                 clock=time.monotonic):
        self._gate_value = gate_value
        self._speak = speak
        self._vad = vad
        self._submit_recog = submit_recog       # (turn_id, chunk_idx, audio, kind)
        self._purge_recog = purge_recog or (lambda turn_id: None)
        self._send = send
        self._dual = dual
        self._refine_max_samples = refine_max_sec * SAMPLE_RATE
        self._clock = clock
        self._min_speech_frames = max(1, min_speech_ms // FRAME_MS)
        self._min_continuation_frames = max(1, min_speech_continuation_ms // FRAME_MS)
        # 宽限覆盖软切停顿期：软切时 min_silence 的静默已过去，
        # 软切后只需再等 grace-min_silence（grace ≤ min_silence 时软切即到期）
        self._grace_after_cut_s = max(0.0, (reopen_grace_ms - min_silence_ms) / 1000.0)
        # +1：当前帧入 pad 后才播种语音段，maxlen 多留一格才凑得齐 speech_pad 前置
        self._pad = deque(maxlen=max(1, speech_pad_ms // FRAME_MS) + 1)
        self._seg = []                  # 未软切的当前语音段（int16 帧列表）
        self._seg_active = False
        self._seg_active_frames = 0     # 当前段内算有效语音的帧数（≥阈值-0.15）
        self._turn_open = False
        self._turn_id = 0               # 回合编号：回合关闭后迟到的结果按此丢弃
        self._next_chunk = 0            # 本轮已送识别的片段数（即下一个 chunk_idx）
        self._texts = {}                # chunk_idx -> 识别文本（已热词纠错；精修可覆盖）
        self._drafts_done = set()       # 初稿已回流的片段（提交屏障只等初稿）
        self._chunk_audio = {}          # dual：chunk_idx -> 音频（排精修任务用）
        self._refine_submitted = set()  # dual：已排过精修任务的片段
        self._refined = set()           # dual：精修已覆盖的片段（迟到初稿忽略）
        self._grace_deadline = None
        self._prev_gate = "0"
        self._cur_gate = "0"

    def feed(self, frame: np.ndarray, gate: str) -> None:
        """喂入一帧开门音频（gate != "0" 的帧才会到这里）。"""
        self._cur_gate = gate
        self._pad.append(frame)
        event = self._vad.process(int2float(frame))
        if self._vad.triggered:
            self._speak.report()
        if self._vad.triggered and not self._seg_active:
            self._seg_active = True
            if not self._seg:
                # 新段：带上 speech_pad 前置（pad 含当前帧，不再重复追加）
                self._seg = list(self._pad)
                self._seg_active_frames = 0
            else:
                self._seg.append(frame)     # 按住说话续段：pad 不重复补
        elif self._seg_active:
            self._seg.append(frame)
        if self._seg_active and self._vad.active_frame:
            self._seg_active_frames += 1
            self._maybe_cancel_pending_send()
        if event == "end":
            self._seg_active = False
            self._soft_cut()

    def _maybe_cancel_pending_send(self) -> None:
        """宽限内重新开口的续段确认（≥192ms 有效语音）即取消待发：宽限计时撤销，
        等说完重新软切。已暂存的前缀片段文本保留。
        必须在续段确认时就取消待发——不能等软切才算续话，
        否则用户已在说话而宽限到期，半截话就被发出去了。"""
        if not self._turn_open or self._grace_deadline is None:
            return
        if self._seg_active_frames < self._min_continuation_frames:
            return
        self._grace_deadline = None
        logger.info("宽限内续话确认，取消待发（turn %d，前缀片段保留）",
                    self._turn_id)
        # dual：续话确认 = 已关闭的片段都不是最后一片，全部排 qwen3 精修
        self._submit_refines(self._next_chunk)

    def poll(self) -> None:
        """周期调用：处理门控跳变（flush）与宽限到期提交。"""
        gate = self._gate_value()
        prev = self._prev_gate
        if (prev == "2" and gate != "2") or (gate == "0" and prev != "0"):
            self._flush()
        self._prev_gate = gate
        self._maybe_send()

    def on_recog_result(self, turn_id: int, chunk_idx: int, kind: str,
                        text: str) -> None:
        """识别结果回流（主循环调用）：回合已关闭/作废的按 turn_id 丢弃。
        dual 模式：初稿（draft）填充暂存文本并计入提交屏障；精修（refine）
        只覆盖暂存文本，不影响屏障，空精修不覆盖（防 qwen3 失败抹掉初稿）；
        精修已覆盖的片段忽略迟到的初稿
        （单线程 FIFO 下初稿必先于精修回流，这只是防御）。"""
        if turn_id != self._turn_id or not self._turn_open:
            logger.info("丢弃过期识别结果（turn %d 片段 #%d，当前 turn %d）",
                        turn_id, chunk_idx, self._turn_id)
            return
        if kind == "refine":
            if not text:
                # 空精修（qwen3 失败）不得抹掉已回流的好初稿
                logger.warning("精修结果为空，保留初稿（turn %d 片段 #%d）",
                               turn_id, chunk_idx)
                return
            # 退化精修两道判据（qwen3 超 max_total_len 截断音频后的典型症状：
            # 几十秒的初稿被"精修"成一个词，实测输出过 "language"）：
            # 1) 时长下限——片段内停顿都 <300ms（否则已软切），语音近乎连续，
            #    每秒至少约 1.5 字；与初稿质量无关（初稿为空/虚长都能兜住）；
            # 2) 比初稿短一半以上——捕捉部分截断（只返回开头一段）。
            # 误判的代价只是保留初稿，不会更糟，所以判据可以激进。
            audio_sec = len(self._chunk_audio.get(chunk_idx, [])) / SAMPLE_RATE
            draft = self._texts.get(chunk_idx, "")
            if len(text) < audio_sec * 1.5 or (draft and len(text) < len(draft) * 0.5):
                logger.warning("精修疑似退化（%d 字，音频 %.1fs，初稿 %d 字），"
                               "保留初稿（turn %d 片段 #%d）: %s",
                               len(text), audio_sec, len(draft),
                               turn_id, chunk_idx, text[:30])
                return
            self._refined.add(chunk_idx)
            self._drafts_done.add(chunk_idx)     # 防御：精修到 = 这片已有文本
            self._texts[chunk_idx] = text
            logger.info("精修覆盖（turn %d 片段 #%d）: %s",
                        turn_id, chunk_idx, text[:60])
            return
        if chunk_idx in self._refined:
            logger.info("精修已覆盖，丢弃迟到的初稿（turn %d 片段 #%d）",
                        turn_id, chunk_idx)
            return
        self._drafts_done.add(chunk_idx)
        self._texts[chunk_idx] = text

    def _submit_refines(self, upto: int) -> None:
        """dual 模式：给已关闭、还没排过精修的片段（0..upto-1）排 qwen3
        精修任务。调用时机：宽限内续话确认（upto=全部已关闭片段）、下次
        软切（upto=刚切片段之前——刚切的这片是否最后一片还不知道）。"""
        if not self._dual:
            return
        for i in range(upto):
            if i in self._refine_submitted or i not in self._chunk_audio:
                continue
            self._refine_submitted.add(i)
            audio = self._chunk_audio[i]
            if len(audio) > self._refine_max_samples:
                # qwen3 音频按 13 token/秒进 max_total_len=512 的 KV，
                # 超限 sherpa 截断音频、生成退化——长片段直接用初稿
                logger.info("片段 #%d 超长（%.1fs > %ds），不排精修，保留初稿"
                            "（turn %d）",
                            i, len(audio) / SAMPLE_RATE,
                            self._refine_max_samples // SAMPLE_RATE,
                            self._turn_id)
                continue
            self._submit_recog(self._turn_id, i, audio, "refine")
            logger.info("片段 #%d 送精修（%.1fs，turn %d）",
                        i, len(audio) / SAMPLE_RATE, self._turn_id)

    def _soft_cut(self, skip_grace=False) -> None:
        seg, self._seg = self._seg, []
        self._seg_active = False
        if not seg:
            return
        min_frames = (self._min_continuation_frames if self._turn_open
                      else self._min_speech_frames)
        if self._seg_active_frames < min_frames:
            logger.info("丢弃过短语音段（约 %dms 有效语音）",
                        self._seg_active_frames * FRAME_MS)
            return
        if not self._turn_open:
            self._turn_id += 1
            self._next_chunk = 0
            self._texts = {}
            self._drafts_done = set()
            self._chunk_audio = {}
            self._refine_submitted = set()
            self._refined = set()
        self._turn_open = True
        chunk_idx = self._next_chunk
        self._next_chunk += 1
        audio = np.concatenate([int2float(f) for f in seg])
        kind = "draft" if self._dual else "final"
        if self._dual:
            self._chunk_audio[chunk_idx] = audio
        self._submit_recog(self._turn_id, chunk_idx, audio, kind)
        # dual：前序已关闭片段确定不是最后一片（用户又开口了才切到这片），
        # 补排精修；刚切的这片等续话确认/下次软切再说
        self._submit_refines(chunk_idx)
        if skip_grace:
            self._grace_deadline = self._clock()
        elif self._cur_gate != "2":
            self._grace_deadline = self._clock() + self._grace_after_cut_s
        # 按住说话（gate "2"）：不启动宽限——切分识别与提交判定解耦，
        # 片段照送识别但绝不自动提交，提交只发生在松手/关门 flush 时
        logger.info("语音软切：片段 #%d 送识别（%.1fs，turn %d%s）",
                    chunk_idx, len(audio) / SAMPLE_RATE, self._turn_id,
                    "，跳过宽限" if skip_grace else
                    "，按住说话不自动提交" if self._cur_gate == "2" else "")

    def _flush(self) -> None:
        """关门/松开长按：在途语音立即软切并跳过宽限；回合开着但没在等宽限的
        （按住说话）立即到期——等本轮片段到齐后拼接提交。"""
        if self._seg:
            self._soft_cut(skip_grace=True)
        if self._turn_open:
            if self._grace_deadline is None:
                self._grace_deadline = self._clock()
            else:
                self._grace_deadline = min(self._grace_deadline, self._clock())

    def _all_chunks_done(self) -> bool:
        """提交屏障：本轮送出去识别的片段初稿是否全部回流（不等在途精修）。"""
        return len(self._drafts_done) == self._next_chunk

    def _maybe_send(self) -> None:
        """宽限到期 + 本轮片段全部到齐才拼接提交。结果由主循环轮询回流，
        等待期间音频回调、门控等其他事件照常处理（不会卡死）。"""
        if not self._turn_open or self._grace_deadline is None:
            return
        if self._clock() < self._grace_deadline:
            return
        if not self._all_chunks_done():
            return
        # 按序顺接（不加分隔符；片段文本已含 qwen3 自带标点），
        # 识别为空串的片段（纯噪音/失败）不贡献文本，自然跳过
        text = "".join(self._texts.get(i, "")
                       for i in range(self._next_chunk)).strip()
        if text:
            logger.info("提交回合文本（%d 字）: %s", len(text), text[:60])
            self._send(text)
        else:
            logger.info("识别结果为空，丢弃该回合")
        self._close_turn()

    def _close_turn(self) -> None:
        # 屏障保证此时片段初稿已全到齐，purge 是防御性清理（正常是空操作；
        # dual 模式下在途精修随回合关闭按死 turn 丢弃）
        self._purge_recog(self._turn_id)
        self._turn_open = False
        self._next_chunk = 0
        self._texts = {}
        self._drafts_done = set()
        self._chunk_audio = {}
        self._refine_submitted = set()
        self._refined = set()
        self._grace_deadline = None


# ---------- 识别与发送线程 ----------

def _load_sherpa_recognizer(engine: str, num_threads: int):
    """构造 sherpa-onnx OfflineRecognizer（离线、int8 模型，CPU，路径相对项目根写死）。"""
    import sherpa_onnx
    if engine == "qwen3_asr":
        return sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=str(QWEN3_ASR_DIR / "conv_frontend.onnx"),
            encoder=str(QWEN3_ASR_DIR / "encoder.int8.onnx"),
            decoder=str(QWEN3_ASR_DIR / "decoder.int8.onnx"),
            tokenizer=str(QWEN3_ASR_DIR / "tokenizer"),
            num_threads=num_threads, max_new_tokens=512)
    raise ValueError(f"未知 stt_engine: {engine}")


def _load_sense_voice_recognizer(num_threads: int):
    """SenseVoice-Small（int8）：dual 接力模式的初稿引擎（快，RTF≈0.03）。"""
    import sherpa_onnx
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(SENSE_VOICE_DIR / "model.int8.onnx"),
        tokens=str(SENSE_VOICE_DIR / "tokens.txt"),
        num_threads=num_threads, language="auto", use_itn=True)


def _sherpa_infer(rec, audio: np.ndarray) -> str:
    """sherpa 整轮识别：audio 是 float32 @16k（采集即 16k，无需重采样）。"""
    stream = rec.create_stream()
    stream.accept_waveform(SAMPLE_RATE, audio)
    rec.decode_stream(stream)
    return normalize_stt_text(stream.result.text)


class Recognizer(threading.Thread):
    """片段识别 + 热词纠错（单线程串行）。引擎由 core.stt_engine 选择：
    "dual"（默认）双引擎接力——初稿（draft）走 SenseVoice（快），精修
    （refine）走 qwen3_asr（准，回流后由分段器覆盖初稿暂存文本）；
    "qwen3_asr" 单引擎——全部任务走 qwen3。未知值报 ValueError。

    队列契约：任务 (turn_id, chunk_idx, kind, audio) 按序全做——每个片段
    内容唯一，丢弃就是丢字，不再是旧投机方案的"只留最新"。结果以
    (turn_id, chunk_idx, kind, text) 回流。整轮作废走 purge(turn_id)：
    清掉队列里该轮未做的任务（在途那个做完照常回流，由分段器按死 turn 丢弃）。

    热词纠错只有事后一层（HotwordCorrector，显式映射 + 拼音同音，全量
    词表、热更新），对全部引擎、每个片段结果生效。qwen3 的解码偏置热词
    已移除：偏置词是 prompt 前缀，占用 max_total_len=512 的模型硬上限，
    词表增长曾让 prompt 超限、生成必然为空、静默丢字；纠错收益主要在
    中文同音词，恰是事后纠错的主场，故只保留这一层。"""

    def __init__(self, cfg, corrector, result_queue: queue.Queue):
        super().__init__(daemon=True, name="voice-core-stt")
        self._cfg = cfg
        self._corrector = corrector
        self._result_queue = result_queue   # 结果以 (turn_id, chunk_idx, kind, text) 回流
        self._queue = queue.Queue()
        self.ready = threading.Event()

    def submit(self, turn_id: int, chunk_idx: int, audio: np.ndarray,
               kind: str = "final") -> None:
        self._queue.put((turn_id, chunk_idx, kind, audio))

    def purge(self, turn_id: int) -> None:
        """整轮作废：队列里该 turn 还没做的任务直接丢弃。"""
        with self._queue.mutex:
            kept = [t for t in self._queue.queue if t[0] != turn_id]
            dropped = len(self._queue.queue) - len(kept)
            if dropped:
                self._queue.queue.clear()
                self._queue.queue.extend(kept)
                logger.info("识别队列清理：丢弃 turn %d 的 %d 个未做任务",
                            turn_id, dropped)

    def run(self) -> None:
        core = self._cfg["core"]
        engine = core.get("stt_engine", "dual")
        num_threads = int(core.get("stt_num_threads", 4))
        if engine not in ("dual", "qwen3_asr"):
            raise ValueError(f"未知 stt_engine: {engine}")
        logger.info("[stt] loading engine=%s ...", engine)
        t0 = time.monotonic()
        recs = {}
        if engine == "dual":
            recs["draft"] = _load_sense_voice_recognizer(num_threads)
            recs["refine"] = _load_sherpa_recognizer("qwen3_asr", num_threads)
        else:
            recs["final"] = _load_sherpa_recognizer(engine, num_threads)
        logger.info("[stt] engine ready (%.1fs), warming up",
                    time.monotonic() - t0)
        # 预热推理（32ms 静音即可，只为触发图构建/内存分配）
        for rec in recs.values():
            _sherpa_infer(rec, np.zeros(BLOCKSIZE, dtype=np.float32))
        self.ready.set()
        logger.info("语音识别模型就绪")
        while True:
            turn_id, chunk_idx, kind, audio = self._queue.get()  # 按序全做，不丢任务
            self._corrector.reload_if_changed()
            # dual：draft→SenseVoice，refine→qwen3；意外 kind 兜底到 qwen3
            rec = recs.get(kind) or recs.get("refine") or recs["final"]
            t0 = time.monotonic()
            try:
                text = _sherpa_infer(rec, audio)
                text = self._corrector.apply(text)
                cleaned = _cleanup_fillers(text, core.get("filler_words") or [])
                if cleaned != text:
                    logger.info("语气词清理: %s -> %s", text, cleaned)
                text = cleaned
            except Exception:
                # 识别失败不能让线程死掉（否则之后所有语音都静默丢失）：
                # 该片段按空串回流，拼接时自然跳过
                logger.exception("识别失败，该片段按空结果回流（turn %d 片段 #%d）",
                                 turn_id, chunk_idx)
                text = ""
            if kind == "refine" and not text and len(audio) >= SAMPLE_RATE:
                # sherpa 侧失败只往 stderr 打 C++ 日志，这里给一个明确的
                # 中文警告（历史上 qwen3 静默生成为空曾导致长句丢字）
                logger.warning("qwen3 精修生成为空（%.1fs 音频）——"
                               "检查音频是否过长或模型是否异常",
                               len(audio) / SAMPLE_RATE)
            label = {"draft": "初稿", "refine": "精修"}.get(kind, "识别")
            logger.info("%s完成（turn %d 片段 #%d，%.1fs 音频，耗时 %.1fs）: %s",
                        label, turn_id, chunk_idx, len(audio) / SAMPLE_RATE,
                        time.monotonic() - t0, text[:60])
            self._result_queue.put((turn_id, chunk_idx, kind, text))


_toaster = None


def _win_notify(text: str) -> None:
    """voice-core → 适配器 POST 失败时弹 Windows 通知。
    适配器自己的通知覆盖 kimi web 没开 / 没选中会话 / 草稿页失败等；
    本函数只覆盖「适配器根本没收到」这一层。通知失败不影响主流程。"""
    global _toaster
    try:
        if _toaster is None:
            from windows_toasts import WindowsToaster
            _toaster = WindowsToaster("MicToAgent")
        from windows_toasts import Toast
        _toaster.show_toast(Toast([text]))
    except Exception:
        pass


class Sender(threading.Thread):
    """串行 POST 适配器。一次请求一次发送，超时不重发——语音提交非幂等，
    重发 = 目标会话收到重复消息。POST 失败弹 Windows 通知（仍不重发）。"""

    def __init__(self, url, timeout_sec, *, notify=None, skip_notify_for=()):
        super().__init__(daemon=True, name="voice-core-send")
        self._url = url
        self._timeout = timeout_sec
        self._queue = queue.Queue()
        self._notify = notify if notify is not None else _win_notify
        self._skip_notify = set(skip_notify_for)

    def send(self, text: str) -> None:
        self._queue.put(text)

    def run(self) -> None:
        import httpx
        # POST 本机适配器，忽略系统代理
        with httpx.Client(timeout=self._timeout, trust_env=False) as client:
            while True:
                text = self._queue.get()
                try:
                    r = client.post(self._url, json={
                        "model": "voice-bridge",
                        "messages": [{"role": "user", "content": text}],
                    })
                    r.raise_for_status()
                    logger.info("已提交到适配器（HTTP %d）", r.status_code)
                except Exception:
                    logger.exception("提交适配器失败（不重发）: %s", text[:60])
                    if text not in self._skip_notify:
                        try:
                            self._notify("语音指令没有发出去，桥接服务连不上。"
                                         "请确认托盘图标还在，或重新启动语音助手")
                        except Exception:
                            pass


def make_audio_callback(gate_value, frame_queue):
    """sounddevice 回调：关门（gate "0"）直接丢弃音频；开门只把帧丢进队列，
    不做任何重活（实时回调里不能阻塞）。
    队列满（主循环掉队超 ~9.6s，系统卡顿/占满 CPU 时可能触发）必须打日志：
    丢帧 = 话中间丢字，静默丢弃会让排查无门。"""
    dropped = 0

    def callback(indata, frames, time_info, status):
        nonlocal dropped
        if gate_value() == "0":
            return
        try:
            frame_queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            dropped += 1
            if dropped % 32 == 1:   # 每丢约 1s 报一次，不刷屏
                logger.warning("音频队列已满，正在丢帧（累计 %d 帧）——"
                               "主循环跟不上，语音会丢字", dropped)
    return callback


# ---------- 主流程 ----------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    cfg = load_config()
    core = cfg["core"]
    adapter_url = (f"http://{cfg['listen_host']}:{cfg['listen_port']}"
                   f"/v1/chat/completions")

    gate = GateWatcher()
    speak = SpeakReporter()

    # hotwords.txt 不存在也传路径：mtime 签名变化（None→存在）会被热更新
    # 机制捕获，以后新建词表下一次识别即生效，不用重启
    corrector = HotwordCorrector([HOTWORD_FILE])
    recog_results = queue.Queue()
    recognizer = Recognizer(cfg, corrector, recog_results)
    recognizer.start()              # 先起识别线程（模型加载耗时最长）

    vad = VadEngine(threshold=core["vad_threshold"],
                    min_silence_ms=core["min_silence_ms"])
    sender = Sender(adapter_url, core["post_timeout_sec"],
                    skip_notify_for=(cfg.get("warmup_text") or "",))
    segmenter = Segmenter(
        gate.value, speak, vad, recognizer.submit, sender.send,
        purge_recog=recognizer.purge,
        dual=(core.get("stt_engine", "dual") == "dual"),
        min_speech_ms=core["min_speech_ms"],
        min_speech_continuation_ms=core["min_speech_continuation_ms"],
        speech_pad_ms=core["speech_pad_ms"],
        reopen_grace_ms=core["reopen_grace_ms"],
        min_silence_ms=core["min_silence_ms"],
        refine_max_sec=core["refine_max_sec"],
    )
    sender.start()

    logger.info("等待语音识别模型加载…")
    recognizer.ready.wait()
    # 链路预热：适配器收到与 warmup_text 相同的文本后置 /ready（launcher 阶段 4）
    sender.send(cfg["warmup_text"])
    logger.info("语音核心就绪，开始收音")

    import sounddevice as sd
    frames = queue.Queue(maxsize=300)
    callback = make_audio_callback(gate.value, frames)
    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, dtype="int16", channels=1,
                            blocksize=BLOCKSIZE, callback=callback):
            while True:
                try:
                    frame = frames.get(timeout=0.05)
                except queue.Empty:
                    frame = None
                while True:         # 识别结果回流（保持主循环单线程访问分段器）
                    try:
                        turn_id, chunk_idx, kind, text = recog_results.get_nowait()
                    except queue.Empty:
                        break
                    segmenter.on_recog_result(turn_id, chunk_idx, kind, text)
                if frame is not None:
                    segmenter.feed(frame, gate.value())
                segmenter.poll()
    except KeyboardInterrupt:
        logger.info("语音核心退出")


if __name__ == "__main__":
    main()
