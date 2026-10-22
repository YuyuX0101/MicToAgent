"""voice-core（自研语音核心）的单元测试。

不加载真实模型：分段器状态机用脚本化 FakeVad + 假识别/假发送注入，
VadEngine 用假模型（喂概率脚本）驱动，热词纠错走 vendor 的真实
PostprocessHotwordMatcher（pypinyin + rapidfuzz）。

覆盖：
- 300ms 静音软切增量识别、1200ms 宽限（覆盖软切停顿期）、宽限内续话前缀保留、片段按序拼接
- dual 双引擎接力：初稿/精修任务路由、续话触发精修、精修覆盖暂存文本、
  提交屏障只等初稿不等在途精修、按住说话除最后一片外都排精修、
  空/退化精修不覆盖初稿、超长片段（>refine_max_sec）不排精修
- flush 提交屏障（等本轮片段到齐再发）、整轮作废（死 turn 结果丢弃 + purge）
- 关门 flush 立即切段提交（跳过宽限）、按住说话切分识别但松手才提交
- gate "0" 丢弃音频（音频回调）、过短语音段丢弃、续段阈值放宽
- gate.state 解析（mtime 缓存）、speak.state 节流、热词解析/纠错/热更新
- ten-vad 双跳取 max、隐藏状态回传
"""

import importlib.util
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

PROJ = Path(__file__).resolve().parent.parent
BRIDGE = PROJ / "voice-bridge"
sys.path.insert(0, str(BRIDGE))     # vendor/ 包


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def core():
    return _load_module("voice_core", BRIDGE / "voice-core.py")


# ---------- 测试替身 ----------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeVad:
    """脚本化 VAD（镜像 VadEngine 状态机）：speech 帧算语音；触发后连续
    min_silence_frames 帧静音报 "end"。"""

    def __init__(self, min_silence_frames=16):
        self.min_silence_frames = min_silence_frames
        self.triggered = False
        self.speech_frame = False
        self.active_frame = False
        self.script = []
        self._silence = 0

    def process(self, frame_f32):
        speech = self.script.pop(0) if self.script else False
        self.speech_frame = speech
        self.active_frame = speech
        if speech:
            self._silence = 0
            self.triggered = True
        elif self.triggered:
            self._silence += 1
            if self._silence >= self.min_silence_frames:
                self.triggered = False
                self._silence = 0
                return "end"
        return None


def _frame():
    return np.full(512, 1000, dtype=np.int16)


def _make_env(core, dual=False):
    e = SimpleNamespace()
    e.clock = Clock()
    e.gate = {"v": "1"}
    e.vad = FakeVad()
    e.submitted = []        # (turn_id, chunk_idx, audio) 识别任务
    e.kinds = []            # 与 submitted 对齐的任务类型（draft/refine/final）
    e.purged = []           # purge(turn_id) 调用记录
    e.sent = []             # 提交给适配器的文本
    e.spoke = 0             # speak.state 上报次数
    e.seg = core.Segmenter(
        lambda: e.gate["v"],
        SimpleNamespace(report=lambda: setattr(e, "spoke", e.spoke + 1)),
        e.vad,
        lambda t, i, a, k: (e.submitted.append((t, i, a)), e.kinds.append(k)),
        e.sent.append,
        purge_recog=e.purged.append,
        dual=dual,
        speech_pad_ms=160,          # 5 帧前置，测试保持紧凑
        reopen_grace_ms=1500,
        min_silence_ms=512,         # 对齐 FakeVad 的 16 帧静音判 end
        clock=e.clock,
    )
    e.core = core
    return e


@pytest.fixture()
def env(core):
    return _make_env(core)


@pytest.fixture()
def dual_env(core):
    """dual 双引擎接力路径（初稿 draft / 精修 refine）。"""
    return _make_env(core, dual=True)


def feed(e, speech=0, silence=0, gate="1"):
    """喂一串语音/静音帧（每帧后 poll 一次，模拟主循环节奏）。"""
    e.gate["v"] = gate
    e.vad.script += [True] * speech + [False] * silence
    for _ in range(speech + silence):
        e.seg.feed(_frame(), e.gate["v"])
        e.seg.poll()


# ---------- 分段器：软切与宽限 ----------

def test_soft_cut_after_silence(env):
    feed(env, silence=5)                        # 先把 pad 填满（模拟持续收音）
    feed(env, speech=20, silence=16)
    assert len(env.submitted) == 1
    turn_id, chunk_idx, audio = env.submitted[0]
    assert (turn_id, chunk_idx) == (1, 0)
    # 首片段 = 5 帧 pad + 20 帧语音 + 16 帧静音，转成 float32 送识别
    assert len(audio) == (5 + 20 + 16) * 512
    assert audio.dtype == np.float32
    assert env.sent == []           # 切段只是送识别，不直接提交


def test_send_after_grace(env):
    feed(env, speech=20, silence=16)
    env.seg.on_recog_result(1, 0, "final", "你好")
    env.seg.poll()
    assert env.sent == []                       # 宽限未满不发
    env.clock.advance(1.6)
    env.seg.poll()
    assert env.sent == ["你好"]                 # 宽限期满提交
    env.clock.advance(5)
    env.seg.poll()
    assert env.sent == ["你好"]                 # 只发一次


def test_grace_covers_soft_cut_silence(env):
    """宽限从话音结束算起、覆盖软切停顿期：软切时已消耗 min_silence（512ms），
    软切后只需再静默 grace-min_silence（≈988ms）即提交，
    总停顿 = grace（1500ms），而非 min_silence + grace。"""
    feed(env, speech=20, silence=16)            # 软切（512ms 静默已计入宽限）
    env.seg.on_recog_result(1, 0, "final", "你好")
    env.clock.advance(0.9)                      # 软切后 900ms：总停顿 1412ms < 1500
    env.seg.poll()
    assert env.sent == []                       # 还不到提交点
    env.clock.advance(0.2)                      # 总停顿 1612ms > 1500
    env.seg.poll()
    assert env.sent == ["你好"]


def test_reopen_within_grace_stitches_chunks(env):
    """宽限内续话：前缀片段的识别结果保留，续段作为新片段送识别，
    最终按序拼接成一条完整文本。"""
    feed(env, speech=20, silence=16)            # 第一次软切 → 片段 #0
    env.seg.on_recog_result(1, 0, "final", "前半")
    env.clock.advance(0.5)                      # 宽限内重新开口
    feed(env, speech=10, silence=16)            # 续段确认取消待发，软切 → 片段 #1
    assert [(t, i) for t, i, _ in env.submitted] == [(1, 0), (1, 1)]
    # 第二次送识别的只是续段增量音频，不是整轮重识
    assert len(env.submitted[1][2]) < len(env.submitted[0][2])
    env.seg.on_recog_result(1, 1, "final", "后半")
    env.clock.advance(1.6)                      # 第二轮宽限期满
    env.seg.poll()
    assert env.sent == ["前半后半"]             # 按序拼接，只发一条


def test_chunk_results_stitch_in_order_regardless_of_arrival(env):
    """片段结果乱序回流，拼接仍按 chunk_idx 顺序。"""
    feed(env, speech=20, silence=16)
    feed(env, speech=10, silence=16)            # 第二个片段
    env.seg.on_recog_result(1, 1, "final", "乙")         # 后送的片段先回来
    env.clock.advance(1.6)
    env.seg.poll()
    assert env.sent == []                       # 屏障：片段 #0 没到齐不发
    env.seg.on_recog_result(1, 0, "final", "甲")
    env.seg.poll()
    assert env.sent == ["甲乙"]                 # 按序拼接，不是按到达顺序


def test_resume_within_grace_cancels_pending_send(env):
    """回归：宽限内已重新开口（但还没停顿到软切）时，宽限到期也不得发送，
    否则一句话被截成两条。实测事故：你好 | 听得到吗 被拆成两条提交。"""
    feed(env, speech=20, silence=16)            # 软切"你好"
    env.seg.on_recog_result(1, 0, "final", "你好")
    env.clock.advance(0.5)                      # 宽限内重新开口
    feed(env, speech=10)                        # 续段确认 → 待发应已取消
    env.clock.advance(1.2)                      # 越过原宽限截止（软切后宽限仅剩 ~1s）
    env.seg.poll()
    assert env.sent == []                       # 没有截断发送
    feed(env, silence=16)                       # 说完 → 续段软切送识别
    env.seg.on_recog_result(1, 1, "final", "听得到吗")
    env.clock.advance(1.6)
    env.seg.poll()
    assert env.sent == ["你好听得到吗"]         # 拼接成一条


def test_noise_burst_does_not_cancel_pending_send(env):
    """宽限内的短噪声（<192ms 续段确认阈值）不取消待发，原回合照常提交。"""
    feed(env, speech=20, silence=16)
    env.seg.on_recog_result(1, 0, "final", "你好")
    env.clock.advance(0.5)
    feed(env, speech=3, silence=16)             # 96ms 噪声段，被丢弃
    assert [(t, i) for t, i, _ in env.submitted] == [(1, 0)]
    env.clock.advance(1.2)
    env.seg.poll()
    assert env.sent == ["你好"]


def test_flush_waits_for_pending_chunks(env):
    """提交屏障：flush/宽限到期时本轮还有片段没识别完，等到齐再拼接提交。"""
    feed(env, speech=20, silence=16)
    feed(env, speech=10, silence=16)            # 两个片段都送识别了
    env.clock.advance(3.0)                      # 宽限早到期，识别还没回来
    env.seg.poll()
    assert env.sent == []
    env.seg.on_recog_result(1, 0, "final", "前半")
    env.seg.poll()
    assert env.sent == []                       # 片段 #1 没到齐，继续等
    env.seg.on_recog_result(1, 1, "final", "后半")
    env.seg.poll()
    assert env.sent == ["前半后半"]             # 到齐立即拼接提交


def test_empty_chunk_is_skipped_in_stitch(env):
    """某片段识别为空串（纯噪音/失败）不贡献文本，其他片段照常拼接。"""
    feed(env, speech=20, silence=16)
    feed(env, speech=10, silence=16)
    env.seg.on_recog_result(1, 0, "final", "有用的部分")
    env.seg.on_recog_result(1, 1, "final", "")           # 空片段
    env.clock.advance(1.6)
    env.seg.poll()
    assert env.sent == ["有用的部分"]


def test_empty_transcript_is_dropped(env):
    feed(env, speech=20, silence=16)
    env.seg.on_recog_result(1, 0, "final", "")
    env.clock.advance(1.6)
    env.seg.poll()
    assert env.sent == []
    feed(env, speech=20, silence=16)            # 回合已关闭，再说话起新回合
    assert [(t, i) for t, i, _ in env.submitted] == [(1, 0), (2, 0)]


def test_late_result_of_closed_turn_is_dropped(env):
    """整轮作废：回合关闭后迟到的识别结果按 turn_id 丢弃。"""
    feed(env, speech=20, silence=16)
    env.seg.on_recog_result(1, 0, "final", "正常提交")
    env.clock.advance(1.6)
    env.seg.poll()
    assert env.sent == ["正常提交"]
    env.seg.on_recog_result(1, 3, "final", "迟到的片段")   # turn 1 已关闭
    env.seg.poll()
    assert env.sent == ["正常提交"]
    assert 1 in env.purged                      # 关闭时清理了队列里该轮的残留


def test_short_segment_discarded(env):
    feed(env, speech=5, silence=16)             # 160ms 有效语音 < 384ms 阈值
    assert env.submitted == []


def test_continuation_threshold_is_relaxed(env):
    feed(env, speech=20, silence=16)            # 第一回合软切
    env.seg.on_recog_result(1, 0, "final", "甲")
    env.clock.advance(0.5)                      # 宽限内续话
    feed(env, speech=7, silence=16)             # 224ms：≥192ms 续段阈值，<384ms
    assert [(t, i) for t, i, _ in env.submitted] == [(1, 0), (1, 1)]


# ---------- 分段器：门控三态 ----------

def test_flush_on_gate_close_sends_immediately(env):
    feed(env, speech=20)                        # 还在说（未软切）
    assert env.submitted == []
    env.gate["v"] = "0"                         # 点按右Ctrl关门
    env.seg.poll()                              # 主循环发现跳变：立即软切
    assert [(t, i) for t, i, _ in env.submitted] == [(1, 0)]
    env.seg.on_recog_result(1, 0, "final", "说完了")
    env.seg.poll()                              # 跳过宽限，识别完即发
    assert env.sent == ["说完了"]


def test_flush_expires_pending_grace(env):
    feed(env, speech=20, silence=16)            # 已软切，在等宽限
    env.seg.on_recog_result(1, 0, "final", "在等宽限")
    env.gate["v"] = "0"
    env.seg.poll()                              # 关门：宽限立即到期
    assert env.sent == ["在等宽限"]


def test_hold_cuts_chunks_but_sends_only_on_release(env):
    """按住说话：切分识别与提交判定解耦——停顿照常软切送识别
    （长按说很久也只需等最后一个片段），但绝不自动提交，松手才拼接发。"""
    feed(env, speech=20, silence=16, gate="2")  # 按住说话，停顿到软切
    assert [(t, i) for t, i, _ in env.submitted] == [(1, 0)]
    feed(env, speech=10, silence=16, gate="2")  # 按住期间续说：切第二个片段
    assert [(t, i) for t, i, _ in env.submitted] == [(1, 0), (1, 1)]
    env.seg.on_recog_result(1, 0, "final", "按住说的前半")
    env.seg.on_recog_result(1, 1, "final", "后半")
    env.clock.advance(5)
    env.seg.poll()
    assert env.sent == []                       # 宽限不存在，绝不自动提交
    env.gate["v"] = "1"                         # 松开
    env.seg.poll()
    assert env.sent == ["按住说的前半后半"]      # 片段早已识别完，立即拼接发


def test_hold_release_waits_for_last_chunk(env):
    """按住说话的提交屏障：松手时在途/最后片段还没识别完，等到齐再发。"""
    feed(env, speech=20, silence=16, gate="2")  # 片段 #0 已切走
    feed(env, speech=10, gate="2")              # 还在说第二段
    env.gate["v"] = "1"                         # 松手：在途语音 flush 成片段 #1
    env.seg.poll()
    assert [(t, i) for t, i, _ in env.submitted] == [(1, 0), (1, 1)]
    env.seg.on_recog_result(1, 0, "final", "前半")
    env.seg.poll()
    assert env.sent == []                       # 最后片段没到齐，等
    env.seg.on_recog_result(1, 1, "final", "后半")
    env.seg.poll()
    assert env.sent == ["前半后半"]


def test_flush_only_affects_current_turn(env):
    feed(env, speech=20, gate="1")
    env.gate["v"] = "0"                         # 关门 flush 发掉在途一轮
    env.seg.poll()
    env.seg.on_recog_result(1, 0, "final", "第一句")
    env.seg.poll()
    assert env.sent == ["第一句"]
    feed(env, speech=20, silence=16, gate="1")  # 重新开门：恢复常听节奏
    env.seg.on_recog_result(2, 0, "final", "第二句")
    env.seg.poll()
    assert env.sent == ["第一句"]               # 宽限未满不发
    env.clock.advance(1.6)
    env.seg.poll()
    assert env.sent == ["第一句", "第二句"]


# ---------- 分段器：dual 双引擎接力（初稿/精修） ----------

def test_dual_continuation_triggers_refine_and_overwrites(dual_env):
    """续话确认 → 刚关闭的片段排 qwen3 精修；精修回流覆盖暂存文本；
    最后一片永远是初稿。"""
    e = dual_env
    feed(e, speech=20, silence=16)              # 软切片段 0（初稿）
    assert e.kinds == ["draft"]
    e.seg.on_recog_result(1, 0, "draft", "前半初稿")
    feed(e, speech=8)                           # 宽限内续话确认（≥192ms）
    assert e.kinds == ["draft", "refine"]       # 片段 0 补排精修
    assert e.submitted[1][1] == 0               # 精修的还是片段 0
    feed(e, silence=16)                         # 软切片段 1（初稿；片段 0 不重复排）
    assert e.kinds == ["draft", "refine", "draft"]
    e.seg.on_recog_result(1, 0, "refine", "前半精修")   # 精修覆盖
    e.seg.on_recog_result(1, 1, "draft", "后半")
    e.clock.advance(1.6)
    e.seg.poll()
    assert e.sent == ["前半精修后半"]


def test_dual_send_does_not_wait_for_inflight_refine(dual_env):
    """提交屏障只等初稿：精修在途不等，宽限到期用初稿文本提交；
    迟到的精修按死 turn 丢弃。"""
    e = dual_env
    feed(e, speech=20, silence=16)
    e.seg.on_recog_result(1, 0, "draft", "前半")
    feed(e, speech=8, silence=16)               # 续话（排精修）→ 软切片段 1
    assert e.kinds == ["draft", "refine", "draft"]
    e.seg.on_recog_result(1, 1, "draft", "后半")
    e.clock.advance(1.6)
    e.seg.poll()                                # 精修没回来也照常提交
    assert e.sent == ["前半后半"]
    e.seg.on_recog_result(1, 0, "refine", "迟到的精修")
    assert e.sent == ["前半后半"]               # 回合已关闭，不影响已提交内容


def test_dual_single_chunk_turn_is_draft_only(dual_env):
    """单片段回合（短指令）：只有初稿，不排精修，宽限到期直接提交。"""
    e = dual_env
    feed(e, speech=20, silence=16)
    assert e.kinds == ["draft"]
    e.seg.on_recog_result(1, 0, "draft", "你好")
    e.clock.advance(1.6)
    e.seg.poll()
    assert e.sent == ["你好"]
    assert e.kinds == ["draft"]                 # 没有精修任务


def test_dual_late_draft_after_refine_is_ignored(dual_env):
    """防御：精修先覆盖的片段，迟到的初稿不得回退文本（单线程 FIFO 下
    初稿本应先回流，这里直接乱序注入验证防御分支）。"""
    e = dual_env
    feed(e, speech=20, silence=16)
    feed(e, speech=8, silence=16)               # 续话（排精修）→ 软切片段 1
    e.seg.on_recog_result(1, 0, "refine", "精修")   # 精修先回（乱序）
    e.seg.on_recog_result(1, 0, "draft", "初稿")    # 迟到的初稿被忽略
    e.seg.on_recog_result(1, 1, "draft", "后半")
    e.clock.advance(1.6)
    e.seg.poll()
    assert e.sent == ["精修后半"]


def test_dual_hold_refines_all_but_last_chunk(dual_env):
    """按住说话（无宽限续话事件）：每次软切给前序片段补排精修，
    松手 flush 时最后一片用初稿直接提交。"""
    e = dual_env
    feed(e, speech=20, silence=16, gate="2")    # 软切片段 0（初稿）
    assert e.kinds == ["draft"]
    feed(e, speech=20, silence=16, gate="2")    # 软切片段 1 + 片段 0 补精修
    assert e.kinds == ["draft", "draft", "refine"]
    assert e.sent == []
    e.seg.on_recog_result(1, 0, "draft", "按住前半初稿")
    e.seg.on_recog_result(1, 0, "refine", "按住前半精修")
    e.seg.on_recog_result(1, 1, "draft", "后半")
    e.gate["v"] = "1"                           # 松开：flush，跳过宽限
    e.seg.poll()
    assert e.sent == ["按住前半精修后半"]


def test_dual_empty_refine_keeps_draft(dual_env):
    """qwen3 精修返回空串（失败/超 max_total_len 生成为空）不得抹掉
    已回流的好初稿——热词表超预算时每次精修都是空串，无此防御长句
    只剩最后一片。"""
    e = dual_env
    feed(e, speech=20, silence=16)
    e.seg.on_recog_result(1, 0, "draft", "前半初稿")
    feed(e, speech=8, silence=16)               # 续话（排精修）→ 软切片段 1
    assert e.kinds == ["draft", "refine", "draft"]
    e.seg.on_recog_result(1, 0, "refine", "")   # 空精修：保留初稿
    e.seg.on_recog_result(1, 1, "draft", "后半")
    e.clock.advance(1.6)
    e.seg.poll()
    assert e.sent == ["前半初稿后半"]


def test_dual_degenerate_refine_keeps_draft(dual_env):
    """精修文本异常短于初稿视为退化（qwen3 超 max_total_len 截断音频后的
    典型症状：几十秒初稿被"精修"成一个词，实测输出过 "language"），
    不覆盖初稿。"""
    e = dual_env
    feed(e, speech=20, silence=16)
    e.seg.on_recog_result(1, 0, "draft", "这是一段很长很长的初稿文本，有几十个字")
    feed(e, speech=8, silence=16)               # 续话（排精修）→ 软切片段 1
    assert e.kinds == ["draft", "refine", "draft"]
    e.seg.on_recog_result(1, 0, "refine", "language")   # 退化精修：保留初稿
    e.seg.on_recog_result(1, 1, "draft", "后半")
    e.clock.advance(1.6)
    e.seg.poll()
    assert e.sent == ["这是一段很长很长的初稿文本，有几十个字后半"]


def test_dual_degenerate_refine_rejected_with_empty_draft(dual_env):
    """初稿为空（SenseVoice 失败按空串回流）时，初稿比例判定失效，
    时长下限（片段内停顿 <300ms、语音近乎连续，每秒 ≥1.5 字）仍能
    拦截退化精修——否则 "language" 会被当成该片段的正式文本提交。"""
    e = dual_env
    feed(e, speech=400, silence=16)             # ~13s 片段 0（含 pad）
    e.seg.on_recog_result(1, 0, "draft", "")    # 初稿失败为空
    feed(e, speech=8, silence=16)               # 续话（排精修）→ 软切片段 1
    assert e.kinds == ["draft", "refine", "draft"]
    e.seg.on_recog_result(1, 0, "refine", "language")   # 8 字 << 13s×1.5
    e.seg.on_recog_result(1, 1, "draft", "后半")
    e.clock.advance(1.6)
    e.seg.poll()
    assert e.sent == ["后半"]                   # 退化精修被拦截


def test_dual_long_chunk_skips_refine(dual_env):
    """超过 refine_max_sec（默认 25s）的片段不排 qwen3 精修（音频 13 token/秒，
    超 ~30s 必超 max_total_len=512 被截断退化），直接用初稿。"""
    e = dual_env
    feed(e, speech=800, silence=16)             # ~25.8s 片段 0（含 pad）
    assert e.kinds == ["draft"]
    feed(e, speech=8, silence=16)               # 续话确认 + 软切片段 1
    assert e.kinds == ["draft", "draft"]        # 片段 0 超长，不排精修
    e.seg.on_recog_result(1, 0, "draft", "长片段初稿")
    e.seg.on_recog_result(1, 1, "draft", "后半")
    e.clock.advance(1.6)
    e.seg.poll()
    assert e.sent == ["长片段初稿后半"]


def test_audio_callback_discards_when_gate_closed(env):
    import queue as q
    frames = q.Queue()
    gate = {"v": "0"}
    cb, _ = env.core.make_audio_callback(lambda: gate["v"], frames)
    indata = np.zeros((512, 1), dtype=np.int16)
    cb(indata, 512, None, None)                 # 关门：丢弃
    assert frames.empty()
    gate["v"] = "1"
    cb(indata, 512, None, None)                 # 开门：进队列
    assert frames.get_nowait().shape == (512,)
    gate["v"] = "2"
    cb(indata, 512, None, None)                 # 按住说话：同样放行
    assert frames.qsize() == 1


def test_audio_callback_liveness_heartbeat(env):
    """回调心跳（音频流假死看门狗的依据）：关门也更新；状态异常不致命。"""
    import queue as q
    frames = q.Queue()
    cb, last_at = env.core.make_audio_callback(lambda: "0", frames)
    assert last_at[0] == 0.0
    indata = np.zeros((512, 1), dtype=np.int16)
    cb(indata, 512, None, None)
    assert last_at[0] > 0.0                     # 关门丢帧但心跳照打
    cb(indata, 512, None, "input overflow")     # PortAudio 状态异常不炸回调
    assert frames.empty()


def test_speak_reported_while_triggered(env):
    feed(env, speech=3)
    assert env.spoke > 0
    feed(env, silence=20)                       # 静音窗口内仍算语音段（会上报），
    spoke = env.spoke                           # 报过 end 之后才真的停
    feed(env, silence=5)
    assert env.spoke == spoke


# ---------- GateWatcher / SpeakReporter ----------

def test_gate_value_missing_file_is_closed(core, tmp_path):
    assert core.GateWatcher(tmp_path / "gate.state").value() == "0"


@pytest.mark.parametrize("content,expected", [
    ("0", "0"), ("1", "1"), ("2", "2"), ("1\n", "1"), ("garbage", "0"), ("", "0"),
])
def test_gate_value_parsing(core, tmp_path, content, expected):
    gate = tmp_path / "gate.state"
    gate.write_text(content, encoding="ascii")
    assert core.GateWatcher(gate).value() == expected


def test_gate_value_mtime_cache(core, tmp_path):
    gate = tmp_path / "gate.state"
    gate.write_text("1", encoding="ascii")
    watcher = core.GateWatcher(gate)
    assert watcher.value() == "1"
    # mtime 不变时走缓存，文件内容被替换也不重读（缓存按 mtime 失效）
    mtime = os.path.getmtime(gate)
    gate.write_text("0", encoding="ascii")
    os.utime(gate, (mtime, mtime))
    assert watcher.value() == "1"


def test_speak_reporter_throttled(core, tmp_path):
    clock = Clock()
    state = tmp_path / "speak.state"
    reporter = core.SpeakReporter(state, clock=clock)
    reporter.report()
    assert state.exists()
    mtime = os.path.getmtime(state)
    reporter.report()                           # 节流窗口内：不刷新
    assert os.path.getmtime(state) == mtime
    clock.advance(core.SPEAK_TOUCH_INTERVAL_S + 0.01)
    reporter.report()
    assert reporter._last == clock.t            # 超过节流窗口再次刷新


# ---------- VadEngine（假模型喂概率脚本） ----------

class _FakeModel:
    def __init__(self, probs):
        self._probs = list(probs)

    def __call__(self, frame_f32):
        return self._probs.pop(0) if self._probs else 0.0


def _vad(core, probs, **kw):
    return core.VadEngine(_FakeModel(probs), threshold=0.6, **kw)


def test_vad_engine_start_and_end(core):
    # 3 帧语音 + 6 帧静音（min_silence=160ms → 5 帧静音判 end）
    vad = _vad(core, [0.9, 0.9, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1],
               min_silence_ms=160)
    events = [vad.process(np.zeros(512, dtype=np.float32)) for _ in range(9)]
    assert vad.speech_frame is False
    assert events[:3] == [None, None, None]     # 触发不报事件，只置 triggered
    assert "end" in events[3:]                  # 静音满 160ms 报 end
    assert vad.triggered is False


def test_vad_engine_dead_zone_resets_silence(core):
    # 0.5 处于 [neg=0.45, threshold=0.6) 死区：算有效语音、
    # 重置静音计时，切段要求连续 min_silence 都低于 0.45
    vad = _vad(core, [0.9, 0.1, 0.5, 0.1, 0.1, 0.1, 0.1, 0.1],
               min_silence_ms=160)
    events = [vad.process(np.zeros(512, dtype=np.float32)) for _ in range(8)]
    assert "end" not in events                # 死区帧重置了计时，静音没凑满
    # 再继续静音，凑满 5 帧连续 <0.45 才切段
    vad._model = _FakeModel([0.1])
    assert vad.process(np.zeros(512, dtype=np.float32)) == "end"


def test_vad_engine_active_frame_semantics(core):
    # active_frame = 概率 ≥ threshold-0.15（含死区），供 min_speech 计数
    vad = _vad(core, [0.9, 0.5, 0.1], min_silence_ms=160)
    vad.process(np.zeros(512, dtype=np.float32))
    assert vad.active_frame is True           # 0.9 语音帧
    vad.process(np.zeros(512, dtype=np.float32))
    assert vad.active_frame is True           # 0.5 死区帧也算有效语音
    vad.process(np.zeros(512, dtype=np.float32))
    assert vad.active_frame is False          # 0.1 真静音


def test_vad_engine_speech_resets_silence(core):
    vad = _vad(core, [0.9, 0.1, 0.1, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1],
               min_silence_ms=160)
    events = [vad.process(np.zeros(512, dtype=np.float32)) for _ in range(10)]
    assert events[:3] == [None, None, None]     # 中途重新开口，不切段
    assert "end" in events[3:]


# ---------- TEN-VAD 模型包装 ----------

def test_vad_engine_injected_model(core):
    # 注入假模型时不加载任何真实后端
    vad = core.VadEngine(_FakeModel([0.9]), threshold=0.6)
    vad.process(np.zeros(512, dtype=np.float32))
    assert vad.triggered is True


def _bare_ten_vad(core, hop_probs):
    """不碰 onnx 的 TenVadModel：__new__ 绕过构造，脚本化 hop。"""
    m = core.TenVadModel.__new__(core.TenVadModel)
    calls = []
    probs = list(hop_probs)

    def hop(samples):
        calls.append(len(samples))
        return probs.pop(0) if probs else 0.0
    m.hop = hop
    return m, calls


def test_ten_vad_block_takes_max_of_two_hops(core):
    # 512 样本块 = 两跳 256；块概率取 max（静音底远离阈值，取 max 防漏检）
    m, calls = _bare_ten_vad(core, [0.3, 0.7])
    assert m(np.zeros(512, dtype=np.float32)) == 0.7
    assert calls == [256, 256]                 # 两跳各 256 样本
    m2, _ = _bare_ten_vad(core, [0.8, 0.2])
    assert m2(np.zeros(512, dtype=np.float32)) == 0.8


def _fake_ort_session(prob):
    """假 ORT session：记录 feed，返回脚本概率 + 递增隐藏状态。"""
    feeds = []

    class _Sess:
        def run(self, _out, feed):
            feeds.append(feed)
            prob_arr = np.array([[prob]], dtype=np.float32)
            states = [np.full((1, 64), i + 1, dtype=np.float32) for i in range(4)]
            return [prob_arr] + states
    return _Sess(), feeds


def _wired_ten_vad(core, prob):
    """绕过 __init__ 接好最小状态的 TenVadModel（跳过特征提取，只测状态流）。"""
    m = core.TenVadModel.__new__(core.TenVadModel)
    sess, feeds = _fake_ort_session(prob)
    m._sess = sess
    m._inputs = ["input_1", "input_2", "input_3", "input_6", "input_7"]
    m._mean = np.zeros(41, dtype=np.float32)
    m._inv_std = np.ones(41, dtype=np.float32)
    m._window = np.ones(768, dtype=np.float32)
    m._mel = SimpleNamespace(compute=lambda power: np.ones(40, dtype=np.float32))
    m.reset()
    return m, feeds


def test_ten_vad_hidden_states_carried_across_hops(core):
    m, feeds = _wired_ten_vad(core, 0.9)
    m.hop(np.zeros(256, dtype=np.float32))
    m.hop(np.zeros(256, dtype=np.float32))
    assert len(feeds) == 2
    # 第一跳：初始状态全零；第二跳：回传的是上一跳输出的状态（值 1..4）
    first, second = feeds
    assert all(np.all(first[name] == 0) for name in m._inputs[1:])
    for i, name in enumerate(m._inputs[1:]):
        assert np.all(second[name] == i + 1)
    # 特征历史滚动：3x41 输入
    assert first["input_1"].shape == (1, 3, 41)


def test_ten_vad_reset_clears_states(core):
    m, feeds = _wired_ten_vad(core, 0.9)
    m.hop(np.zeros(256, dtype=np.float32))
    m.reset()
    m.hop(np.zeros(256, dtype=np.float32))
    assert all(np.all(feeds[-1][name] == 0) for name in m._inputs[1:])


# ---------- 热词表解析与识别后纠错 ----------

HOTWORD_FILE_CONTENT = """\
# 注释行会被忽略
会话
工作区
kimi code
chemicde=>kimi code
"""


@pytest.fixture()
def hotword_file(tmp_path):
    path = tmp_path / "hotwords.txt"
    path.write_text(HOTWORD_FILE_CONTENT, encoding="utf-8")
    return str(path)


def test_parse_hotword_files(core, hotword_file):
    explicit, fuzzy, english = core.parse_hotword_files([hotword_file])
    assert explicit == {"chemicde": "kimi code"}
    assert fuzzy == ["会话", "工作区"]
    assert english == ["kimi code"]


def test_parse_hotword_files_missing_file(core, tmp_path):
    explicit, fuzzy, english = core.parse_hotword_files([str(tmp_path / "nope.txt")])
    assert explicit == {}
    assert fuzzy == []
    assert english == []


@pytest.fixture()
def corrector(core, hotword_file):
    pytest.importorskip("pypinyin")
    pytest.importorskip("rapidfuzz")
    c = core.HotwordCorrector([hotword_file])
    c.reload_if_changed()
    return c


def test_homophone_and_explicit_correction(corrector):
    assert corrector.apply("帮我看一下工作区里chemicde的绘画") == "帮我看一下工作区里kimi code的会话"


def test_correct_text_is_untouched(corrector):
    assert corrector.apply("帮我部署一下这个项目的会话") == "帮我部署一下这个项目的会话"


def test_near_homophone_is_not_overcorrected(corrector):
    # 「区下」(quxia) 与「取消」(quxiao) 拼音相近但不相同，阈值 1.0 下不得误改
    assert corrector.apply("工作区下有几个会话") == "工作区下有几个会话"


def test_syllable_boundary_is_not_overcorrected(core, tmp_path):
    # 「连」(lian) 与「立案」(li-an) 拼接拼音相同但音节数不同，不得互相替换；
    # 同音节数的真同音（利安 → 立案）仍要纠正；已正确的「立案。」不得被吞标点。
    pytest.importorskip("pypinyin")
    pytest.importorskip("rapidfuzz")
    p = tmp_path / "hotwords.txt"
    p.write_text("立案\n", encoding="utf-8")
    c = core.HotwordCorrector([str(p)])
    c.reload_if_changed()
    assert c.apply("我经常说连接和连网") == "我经常说连接和连网"
    assert c.apply("先去利安再回来") == "先去立案再回来"
    assert c.apply("已经立案。下一步") == "已经立案。下一步"


# ---------- 英文热词模糊纠错 ----------

@pytest.fixture()
def en_corrector(core, tmp_path):
    pytest.importorskip("pypinyin")
    pytest.importorskip("rapidfuzz")
    p = tmp_path / "hotwords.txt"
    p.write_text("kimi code\nMicToAgent\nDeepSeek\nAI\n", encoding="utf-8")
    c = core.HotwordCorrector([str(p)])
    c.reload_if_changed()
    return c


def test_english_fuzzy_near_miss(en_corrector):
    # 1 个字母之差的误听自动纠正，不需要逐条建显式映射
    assert en_corrector.apply("我用kimi cold写代码") == "我用kimi code写代码"
    assert en_corrector.apply("Kimi Cold 真好用") == "kimi code 真好用"


def test_english_fuzzy_unifies_case_and_spacing(en_corrector):
    # 大小写/分词变体归一到词表规范写法
    assert en_corrector.apply("打开 mic to agent 项目") == "打开 MicToAgent 项目"
    assert en_corrector.apply("用 deep seek 查一下") == "用 DeepSeek 查一下"


def test_english_fuzzy_no_false_positive(en_corrector):
    # deep sea 与 DeepSeek 相似度 0.80 < 阈值 0.85，不得误改
    assert en_corrector.apply("the deep sea is blue") == "the deep sea is blue"
    # 已正确的文本原样保留
    assert en_corrector.apply("用 kimi code 和 DeepSeek") == "用 kimi code 和 DeepSeek"


def test_english_fuzzy_short_target_excluded(en_corrector):
    # 归一化 <5 字母的缩写不参与模糊（只能走显式映射）
    assert en_corrector.apply("他说了一个 A I") == "他说了一个 A I"


def test_english_fuzzy_wild_miss_not_matched(en_corrector):
    # 大偏差误听与目标相差太远，模糊收不到（靠显式映射兜底）
    assert en_corrector.apply("Timmy caughtd 是什么") == "Timmy caughtd 是什么"


def test_english_fuzzy_cjk_gap_not_merged(en_corrector):
    # 窗口内夹中文不得合并替换
    assert en_corrector.apply("kimi 的 code 在这里") == "kimi 的 code 在这里"


# ---------- 语气词与口吃清理 ----------

def test_filler_words_removed(core):
    f = core._cleanup_fillers
    assert f("呃，我这一长段话有几个要求", ["呃"]) == "我这一长段话有几个要求"
    assert f("配置。呃，华为的笔记本", ["呃"]) == "配置。华为的笔记本"
    assert f("呃", ["呃"]) == ""
    assert f("呃呃，那个功能", ["呃"]) == "那个功能"
    assert f("已经立案。呃。", ["呃"]) == "已经立案。"
    assert f("没有语气词的句子", ["呃"]) == "没有语气词的句子"


def test_stutter_collapsed(core):
    f = core._cleanup_fillers
    assert f("这应该可以通过通过呃通过代码实现", ["呃"]) == "这应该可以通过代码实现"
    assert f("我呃我觉得可以", ["呃"]) == "我觉得可以"
    assert f("通过通过通过", ["呃"]) == "通过"


def test_legit_reduplication_kept(core):
    f = core._cleanup_fillers
    assert f("谢谢你的帮助", ["呃"]) == "谢谢你的帮助"
    assert f("我们研究研究再说", ["呃"]) == "我们研究研究再说"
    # 纯相邻 2 连且无语气词：保守不动
    assert f("我我想想", ["呃"]) == "我我想想"
    assert f("哈哈哈", ["呃"]) == "哈哈哈"


def test_hotword_file_hot_reload(core, corrector, hotword_file):
    # 初始词表只有「会话」同音目标：绘画 → 会话
    assert corrector.apply("这是绘画") == "这是会话"
    # 追加一条显式映射并 bump mtime，不重启，下一句就按新词表纠错
    # （显式映射先于同音纠正执行，绘画=>画板 会直接盖住 绘画→会话 的同音替换）
    with open(hotword_file, "a", encoding="utf-8") as f:
        f.write("绘画=>画板\n")
    future = time.time() + 2
    os.utime(hotword_file, (future, future))
    corrector.reload_if_changed()
    assert corrector.apply("这是绘画") == "这是画板"


# ---------- 识别线程（队列契约，假 sherpa_onnx 注入） ----------

def _fake_sherpa(monkeypatch, decode):
    """注入假的 sherpa_onnx 模块：OfflineRecognizer 两个构造入口都返回同一个
    假识别器，decode_stream 调 decode(samples) 决定识别文本（可抛错）。"""
    class _Stream:
        def __init__(self):
            self.samples = None
            self.result = SimpleNamespace(text="")

        def accept_waveform(self, sample_rate, samples):
            assert sample_rate == 16000     # 输入必须 16k
            self.samples = samples

    class _Recognizer:
        def __init__(self):
            self.calls = []

        def create_stream(self):
            stream = _Stream()
            self.calls.append(stream)
            return stream

        def decode_stream(self, stream):
            stream.result.text = decode(stream.samples)

    rec = _Recognizer()
    fake = SimpleNamespace(OfflineRecognizer=SimpleNamespace(
        from_qwen3_asr=lambda **kw: rec))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    return rec


def test_recognizer_result_queue_contract(core, monkeypatch):
    """Recognizer 把结果以 (turn_id, chunk_idx, kind, text) 元组放进结果队列——
    回归：曾经把 Queue.put 当回调传入，gen/text 被拆成两个参数，队列里
    只剩 int，主循环解包崩溃，整轮语音链路全灭。"""
    import queue as q

    _fake_sherpa(monkeypatch, lambda samples: " 你 好 ")
    results = q.Queue()
    rec = core.Recognizer({"core": dict(core.CORE_DEFAULTS,
                                        stt_engine="qwen3_asr")},
                          core.HotwordCorrector([]), results)
    rec.start()
    assert rec.ready.wait(timeout=10)
    rec.submit(3, 1, np.zeros(512, dtype=np.float32))
    item = results.get(timeout=10)      # 必须是 (turn_id, chunk_idx, kind, text) 四元组
    assert item == (3, 1, "final", "你好")   # 去 CJK 相邻空格 + 原文返回


def test_recognizer_processes_all_tasks_in_order(core, monkeypatch):
    """队列契约（新）：任务按序全做，不再"只留最新"——每个片段内容唯一，
    丢弃就是丢字。"""
    import queue as q

    _fake_sherpa(monkeypatch, lambda samples: f"{len(samples) // 512}帧")
    results = q.Queue()
    rec = core.Recognizer({"core": dict(core.CORE_DEFAULTS,
                                        stt_engine="qwen3_asr")},
                          core.HotwordCorrector([]), results)
    rec.start()
    assert rec.ready.wait(timeout=10)
    for i in range(3):                      # 连续堆 3 个片段，长度各不相同
        rec.submit(1, i, np.zeros(512 * (i + 1), dtype=np.float32))
    assert results.get(timeout=10) == (1, 0, "final", "1帧")
    assert results.get(timeout=10) == (1, 1, "final", "2帧")
    assert results.get(timeout=10) == (1, 2, "final", "3帧")


def test_recognizer_purge_drops_pending_turn_tasks(core, monkeypatch):
    """整轮作废：purge 清掉队列里该 turn 未做的任务；在途那个照常回流，
    由分段器按死 turn 丢弃。"""
    import queue as q
    import threading

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def _decode(samples):
        calls["n"] += 1
        if calls["n"] == 1:                 # 第 1 次是预热推理，不阻塞
            return "预热"
        started.set()
        release.wait(timeout=10)            # 识别中挂住，让后续任务排队
        return "完成"

    _fake_sherpa(monkeypatch, _decode)
    results = q.Queue()
    rec = core.Recognizer({"core": dict(core.CORE_DEFAULTS,
                                        stt_engine="qwen3_asr")},
                          core.HotwordCorrector([]), results)
    rec.start()
    assert rec.ready.wait(timeout=10)
    rec.submit(1, 0, np.zeros(512, dtype=np.float32))
    assert started.wait(timeout=10)         # (1,0) 在识别中
    rec.submit(1, 1, np.zeros(512, dtype=np.float32))
    rec.submit(2, 0, np.zeros(512, dtype=np.float32))
    rec.purge(1)                            # turn 1 作废：清掉排队的 (1,1)
    release.set()
    assert results.get(timeout=10) == (1, 0, "final", "完成")   # 在途的照常回流
    assert results.get(timeout=10) == (2, 0, "final", "完成")   # (1,1) 被 purge，不会回来
    assert results.empty()


def test_recognizer_survives_model_failure(core, monkeypatch):
    """识别推理抛错不能让识别线程死掉：该片段按空串回流（拼接时跳过），
    之后的片段还能继续识别。"""
    import queue as q

    calls = {"n": 0}

    def _decode(samples):
        calls["n"] += 1
        if calls["n"] == 2:     # 第 1 次是预热推理，第 2 次才是首个识别任务
            raise RuntimeError("CUDA 炸了")
        return "恢复"

    _fake_sherpa(monkeypatch, _decode)
    results = q.Queue()
    rec = core.Recognizer({"core": dict(core.CORE_DEFAULTS,
                                        stt_engine="qwen3_asr")},
                          core.HotwordCorrector([]), results)
    rec.start()
    assert rec.ready.wait(timeout=10)
    rec.submit(1, 0, np.zeros(512, dtype=np.float32))
    assert results.get(timeout=10) == (1, 0, "final", "")      # 失败 → 空串回流
    rec.submit(1, 1, np.zeros(512, dtype=np.float32))
    assert results.get(timeout=10) == (1, 1, "final", "恢复")  # 线程还活着


def test_recognizer_dual_routes_draft_and_refine(core, monkeypatch):
    """dual 引擎：draft 走 SenseVoice 构造入口，refine 走 qwen3_asr，
    结果带 kind 回流。"""
    import queue as q

    made = {}

    def _ctor(tag, decode):
        def _make(**kw):
            made[tag] = kw

            class _Stream:
                def accept_waveform(self, sample_rate, samples):
                    assert sample_rate == 16000

            class _Rec:
                def create_stream(self):
                    return SimpleNamespace(
                        accept_waveform=lambda sr, s: None,
                        result=SimpleNamespace(text=""))

                def decode_stream(self, stream):
                    stream.result.text = decode

            return _Rec()
        return _make

    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(
        OfflineRecognizer=SimpleNamespace(
            from_sense_voice=_ctor("sv", "初稿文"),
            from_qwen3_asr=_ctor("qw", "精修文"))))
    cfg = dict(core.CORE_DEFAULTS, stt_engine="dual")
    results = q.Queue()
    rec = core.Recognizer({"core": cfg}, core.HotwordCorrector([]), results)
    rec.start()
    assert rec.ready.wait(timeout=10)
    assert "sv" in made and "qw" in made      # 双引擎都加载
    assert made["sv"]["language"] == "zh"     # 语种固定 zh，不用 auto（防误判日/韩语）
    rec.submit(1, 0, np.zeros(512, dtype=np.float32), kind="draft")
    rec.submit(1, 0, np.zeros(512, dtype=np.float32), kind="refine")
    assert results.get(timeout=10) == (1, 0, "draft", "初稿文")
    assert results.get(timeout=10) == (1, 0, "refine", "精修文")


def test_stt_engine_selects_sherpa_engine(core, monkeypatch):
    """stt_engine="qwen3_asr"（单引擎模式）走 sherpa 构造入口。"""
    import queue as q

    made = {}

    def _ctor(**kw):
        made.update(kw)
        stream = SimpleNamespace(
            accept_waveform=lambda sr, s: None,
            result=SimpleNamespace(text=""))
        return SimpleNamespace(create_stream=lambda: stream,
                               decode_stream=lambda s: None)

    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(
        OfflineRecognizer=SimpleNamespace(from_qwen3_asr=_ctor)))
    cfg = dict(core.CORE_DEFAULTS, stt_engine="qwen3_asr", stt_num_threads=2)
    rec = core.Recognizer({"core": cfg}, core.HotwordCorrector([]), q.Queue())
    rec.start()
    assert rec.ready.wait(timeout=10)   # 构造发生在 ready 之前
    assert made["num_threads"] == 2
    assert "hotwords" not in made       # 解码偏置已移除，不传热词参数


def test_normalize_stt_text_cjk_space(core):
    """只去掉与 CJK 相邻的空格，英文词间空格保留。"""
    n = core.normalize_stt_text
    assert n(" 你 好 ") == "你好"                 # CJK 字间空格去掉
    assert n("你好 world") == "你好world"         # CJK 相邻空格去掉
    assert n("use kimi code please") == "use kimi code please"  # 英文词间保留
    assert n("打开 voice bridge 的 配置") == "打开voice bridge的配置"


# ---------- 配置 ----------

def test_load_config_merges_core_defaults(core, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        '{"listen_host": "127.0.0.1", "listen_port": 9100, '
        '"warmup_text": "hello", "core": {"min_silence_ms": 700}}',
        encoding="utf-8")
    merged = core.load_config(cfg)
    assert merged["core"]["min_silence_ms"] == 700      # 覆盖默认
    assert merged["core"]["reopen_grace_ms"] == 1200    # 默认保留
    assert merged["core"]["stt_engine"] == "dual"


def test_load_config_drops_removed_stt_stitch(core, tmp_path):
    """配置里残留的 stt_stitch 忽略，不再提供整轮重识回退。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(
        '{"listen_host": "127.0.0.1", "listen_port": 9100, '
        '"core": {"stt_stitch": false}}',
        encoding="utf-8")
    merged = core.load_config(cfg)
    assert "stt_stitch" not in merged["core"]


def test_load_config_without_core_section(core, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"listen_host": "127.0.0.1", "listen_port": 9100}',
                   encoding="utf-8")
    merged = core.load_config(cfg)
    assert merged["core"] == core.CORE_DEFAULTS


# ---------- 发送线程 ----------

def _install_fake_httpx(monkeypatch, post_fn):
    class FakeClient:
        def __init__(self, timeout=None, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            return post_fn(url, json)

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=FakeClient))


def test_sender_notifies_on_failure_without_retry(core, monkeypatch):
    """POST 失败弹通知、不重发（语音提交非幂等）。"""
    posts = []
    notes = []

    def post_fn(url, json):
        posts.append(json)
        raise ConnectionError("adapter down")

    _install_fake_httpx(monkeypatch, post_fn)
    sender = core.Sender("http://127.0.0.1:9/v1/chat/completions", 5,
                         notify=notes.append)
    sender.start()
    sender.send("帮我看看这个项目")
    deadline = time.monotonic() + 5
    while not notes and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(posts) == 1
    assert len(notes) == 1
    assert "没有发出去" in notes[0]


def test_sender_notifies_on_http_error_without_retry(core, monkeypatch):
    """非 2xx 同样视为失败：通知一次、不重发。"""
    posts = []
    notes = []

    class FakeResp:
        status_code = 500

        def raise_for_status(self):
            raise Exception("500 Internal Server Error")

    def post_fn(url, json):
        posts.append(json)
        return FakeResp()

    _install_fake_httpx(monkeypatch, post_fn)
    sender = core.Sender("http://127.0.0.1:9/v1/chat/completions", 5,
                         notify=notes.append)
    sender.start()
    sender.send("第二条指令")
    deadline = time.monotonic() + 5
    while not notes and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(posts) == 1
    assert len(notes) == 1


def test_sender_skips_notify_for_warmup(core, monkeypatch):
    """启动预热失败由 launcher 进度卡承接，不另弹通知。"""
    notes = []

    def post_fn(url, json):
        raise ConnectionError("adapter down")

    _install_fake_httpx(monkeypatch, post_fn)
    sender = core.Sender("http://127.0.0.1:9/v1/chat/completions", 5,
                         notify=notes.append, skip_notify_for=("hello",))
    sender.start()
    sender.send("hello")
    sender.send("真正的指令")
    deadline = time.monotonic() + 5
    while not notes and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(notes) == 1
    assert "没有发出去" in notes[0]
