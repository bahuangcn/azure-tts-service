"""
文本分块、音频拼接、词边界时间戳合并。

长文本超时解决方案的核心工具模块，提供三个纯函数：
  - split_text：按自然边界将长文本切分为多个片段
  - concat_audio_files：拼接多个 WAV/MP3 音频文件
  - merge_word_timings：合并多段词边界时间戳，按累计时长偏移

所有函数无外部依赖，仅使用 Python 标准库。
"""
import struct
import re
from pathlib import Path


# ── 文本分块 ──────────────────────────────────────────────────────────────────
# 四级切分策略：
#   Level 0 — 段落边界（\n）
#   Level 1 — 句子边界（。！？）
#   Level 2 — 子句边界（，；,;）
#   Level 3 — 硬切分（按字符数强制截断）

_SENTENCE_END = re.compile(r"([。！？])")
_CLAUSE_SEP = re.compile(r"([，；,;])")
_NEWLINE_SEP = re.compile(r"(\n)")


def _split_greedy(text: str, max_chars: int, level: int = 0) -> list[str]:
    """
    按当前层级的边界符贪心累积切分。

    参数：
      text     — 待切分的文本
      max_chars — 每段最大字符数
      level    — 当前切分层级：0=段落 1=句子 2=子句 3=硬切分

    返回：
      list[str]，每段 ≤ max_chars
    """
    if not text:
        return []

    if len(text) <= max_chars:
        return [text]

    # Level 3：硬切分——无自然边界可用
    if level >= 3:
        return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]

    sep = [_NEWLINE_SEP, _SENTENCE_END, _CLAUSE_SEP][level]

    # re.split 结果交替: [text_before, sep, text_between, sep, text_after]
    parts: list[str] = sep.split(text)

    # 将分隔符附加到前一段文本末尾（保持原文格式）
    segments: list[str] = []
    i = 0
    while i < len(parts):
        if i + 1 < len(parts):
            # 普通情况：文本 + 分隔符
            segments.append(parts[i] + parts[i + 1])
            i += 2
        else:
            # 最后一段（可能为空字符串）
            seg = parts[i]
            if seg:
                segments.append(seg)
            i += 1

    # 贪心累积：尽量将多个 segment 放入同一 chunk
    chunks: list[str] = []
    current = ""

    for seg in segments:
        combined = current + seg

        if len(combined) <= max_chars:
            # 还能放得下，继续累积
            current = combined
        else:
            # 放不下了，先 flush 当前的 chunk
            if current:
                chunks.append(current)

            # 如果单个 segment 超过 max_chars，递归到下一层级切分
            if len(seg) > max_chars:
                sub_chunks = _split_greedy(seg, max_chars, level + 1)
                # 最后一个 sub_chunk 可能没满，留到下一轮累积
                if len(sub_chunks) > 1 or len(seg) > max_chars:
                    *head, tail = sub_chunks
                    chunks.extend(head)
                    current = tail
                else:
                    current = sub_chunks[0]
            else:
                current = seg

    if current:
        chunks.append(current)

    return chunks


def split_text(text: str, max_chars: int) -> list[str]:
    """
    将长文本按自然边界切分为多个片段，每段 ≤ max_chars。

    切分优先级（从高到低）：
      1. 段落边界（\\n）—— 尽量保持段落完整
      2. 句子边界（。！？）—— 在句末切分，语义最完整
      3. 子句边界（，；,;）—— 在逗号/分号处切分
      4. 硬切分 —— 无自然边界时按字符数强制截断

    参数：
      text     — 待切分的文本
      max_chars — 每段最大字符数

    返回：
      list[str]，每段 ≤ max_chars
    """
    text = text.strip()
    if not text:
        return [""]
    return _split_greedy(text, max_chars, level=0)


# ── 音频拼接 ──────────────────────────────────────────────────────────────────
def concat_audio_files(chunk_paths: list[Path], output_path: Path) -> None:
    """
    拼接多个音频文件为单个文件。

    自动检测音频格式（WAV / MP3）：
      - WAV：复用第一个文件的 44 字节头，拼接 PCM 数据，修正 RIFF/data size 字段
      - MP3：第一个文件完整写入（含 ID3v2 标签），后续文件剥离 ID3v2 后追加原始帧
      - 其他格式：直接按字节拼接（降级处理）

    参数：
      chunk_paths — 待拼接的音频文件路径列表（按顺序）
      output_path — 输出文件路径
    """
    if not chunk_paths:
        raise ValueError("chunk_paths is empty")

    if len(chunk_paths) == 1:
        # 只有一个文件，直接复制
        output_path.write_bytes(chunk_paths[0].read_bytes())
        return

    # 读第一个文件的前 4 字节检测格式
    header_bytes = chunk_paths[0].read_bytes()[:4]

    if header_bytes[:4] == b"RIFF":
        _concat_wav(chunk_paths, output_path)
    elif header_bytes[:2] == b"\xff\xfb" or header_bytes[:2] == b"\xff\xf3" or header_bytes[:3] == b"ID3":
        _concat_mp3(chunk_paths, output_path)
    else:
        # 降级：直接字节拼接
        _concat_raw(chunk_paths, output_path)


def _concat_wav(chunk_paths: list[Path], output_path: Path) -> None:
    """WAV 文件拼接：复用第一个文件的头，拼接所有文件的 PCM 数据。"""
    # 读取第一个文件的 WAV 头（标准 PCM WAV 头 44 字节）
    first_data = chunk_paths[0].read_bytes()

    if len(first_data) < 44 or first_data[:4] != b"RIFF" or first_data[8:12] != b"WAVE":
        # 非标准 WAV，降级为原始拼接
        _concat_raw(chunk_paths, output_path)
        return

    # 收集所有文件的 PCM 数据
    pcm_chunks: list[bytes] = []
    for path in chunk_paths:
        data = path.read_bytes()
        # 查找 "data" chunk（从偏移 36 开始找，跳过 fmt subchunk 的变长部分）
        data_offset = data.find(b"data", 36)
        if data_offset == -1:
            # 没找到 data chunk，跳过整个文件（降级）
            continue
        # data chunk: 4 字节 "data" + 4 字节 size + PCM 数据
        pcm_start = data_offset + 8
        pcm_chunks.append(data[pcm_start:])

    total_pcm = b"".join(pcm_chunks)
    total_pcm_size = len(total_pcm)

    # 构建输出 WAV：复用第一个文件的头，但更新 RIFF size 和 data size
    # RIFF size（偏移 4，uint32 LE）= 文件总大小 - 8
    # data size（偏移 40，uint32 LE）= PCM 数据大小
    riff_size = 36 + total_pcm_size  # 4(RIFF)+4(size)+4(WAVE)+fmt_chunk+4(data)+4(dsize)+pcm
    # fmt chunk 的大小从第一个文件的偏移 16-19 读取
    # 但更简单的方法：复用第一个文件 0 到 data_offset 的字节，只修正两个 size 字段

    # 找到第一个文件的 data chunk 位置
    first_data_offset = first_data.find(b"data", 36)
    header_with_fmt = first_data[:first_data_offset + 8]  # 包含 "data" + 原始 data size

    # 修正：重新构建 data chunk header
    header_end = first_data[:first_data_offset]  # 直到 "data" 之前
    data_header = b"data" + struct.pack("<I", total_pcm_size)

    output_data = bytearray(header_end)
    output_data.extend(data_header)
    output_data.extend(total_pcm)

    # 修正 RIFF size（偏移 4）
    struct.pack_into("<I", output_data, 4, len(output_data) - 8)

    output_path.write_bytes(output_data)


def _concat_mp3(chunk_paths: list[Path], output_path: Path) -> None:
    """MP3 文件拼接：第一个文件完整写入，后续文件剥离 ID3v2 后追加。"""
    parts: list[bytes] = []

    for i, path in enumerate(chunk_paths):
        data = path.read_bytes()
        if i == 0:
            parts.append(data)
        else:
            # 剥离 ID3v2 标签（如果存在）
            stripped = _strip_id3v2(data)
            parts.append(stripped)

    output_path.write_bytes(b"".join(parts))


def _strip_id3v2(data: bytes) -> bytes:
    """
    剥离 MP3 文件开头的 ID3v2 标签。

    ID3v2 标签格式：
      - 前 3 字节：b"ID3"
      - 字节 3-4：版本（主.次）
      - 字节 5：标志位
      - 字节 6-9：synchsafe 编码的标签大小（不包括 10 字节头）
    """
    if len(data) < 10 or data[:3] != b"ID3":
        return data

    # ID3v2 size：4 字节，每字节只用低 7 位（synchsafe 整数）
    size = (
        ((data[6] & 0x7F) << 21)
        | ((data[7] & 0x7F) << 14)
        | ((data[8] & 0x7F) << 7)
        | (data[9] & 0x7F)
    )
    # 标签总长 = 10 字节头 + size
    offset = 10 + size
    return data[offset:]


def _concat_raw(chunk_paths: list[Path], output_path: Path) -> None:
    """降级方案：直接按字节拼接所有文件。"""
    output_path.write_bytes(b"".join(p.read_bytes() for p in chunk_paths))


# ── 词边界时间戳合并 ──────────────────────────────────────────────────────────
def merge_word_timings(
    chunk_timings: list[list[dict]],
    chunk_durations_ms: list[int],
) -> list[dict]:
    """
    合并多段词边界时间戳，按累计时长偏移。

    每个 chunk 的时间戳是相对于当次合成的起始偏移（从 0 开始）。
    合并后每个词的 start_ms / end_ms 加上前面所有 chunk 的累计音频时长。

    参数：
      chunk_timings    — 每段的词边界列表，格式 [{"text":...,"start_ms":...,"end_ms":...}, ...]
      chunk_durations_ms — 每段音频的实际时长（毫秒），通过 mutagen.File() 获取
                          使用实际文件时长而非末词 end_ms，正确计入尾部静音

    返回：
      合并后的词边界列表，时间戳全局连续
    """
    merged: list[dict] = []
    offset = 0

    for i, timings in enumerate(chunk_timings):
        for w in timings:
            merged.append({
                "text": w["text"],
                "start_ms": w["start_ms"] + offset,
                "end_ms": w["end_ms"] + offset,
            })

        # 下一段的偏移 = 当前偏移 + 当前段音频实际时长
        if i < len(chunk_durations_ms):
            offset += chunk_durations_ms[i]

    return merged
