/** SSE 事件的通用形态；业务层再按 event 名收窄 data 类型。 */
export type SSEEvent = { event: string; data: unknown }

/** 按 SSE 规范切出完整事件块，兼容 \n\n 与代理改写的 \r\n\r\n，残余不完整帧留在 rest。 */
function splitSSEFrames(buffer: string): { frames: string[]; rest: string } {
  const parts = buffer.split(/\r?\n\r?\n/)
  return { frames: parts.slice(0, -1), rest: parts[parts.length - 1] ?? '' }
}

/** 解析单个事件块；注释行（`: ping` 心跳）与缺少 event/data 的块返回 null。 */
function parseSSEBlock(block: string): SSEEvent | null {
  let event = ''
  const dataLines: string[] = []
  for (const line of block.split(/\r?\n/)) {
    // 冒号开头是注释行，不属于任何事件
    if (line.startsWith(':')) continue
    if (line.startsWith('event:')) event = line.slice(6).trim()
    // 规范允许 `data:` 后无空格，仅剥离一个可选空格
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }
  if (!event || dataLines.length === 0) return null
  // 多行 data 按规范以换行拼接，兼容任意分帧方式
  const payload = dataLines.join('\n')
  try {
    return { event, data: JSON.parse(payload) }
  } catch (e) {
    console.error('SSE 解析失败', e, payload)
    return null
  }
}

/**
 * 增量 SSE 解析器：跨 chunk 保留 UTF-8 解码状态与未完成帧。
 *
 * TextDecoder 必须复用并开启 stream 模式，否则多字节字符（中文、emoji）
 * 被切在两个 chunk 边界时会被解成乱码。
 */
export function createSSEParser() {
  const decoder = new TextDecoder()
  let buffer = ''
  return {
    /** 喂入一个字节分片，返回本次能完整解析出的事件（保持到达顺序）。 */
    push(chunk: Uint8Array): SSEEvent[] {
      buffer += decoder.decode(chunk, { stream: true })
      const { frames, rest } = splitSSEFrames(buffer)
      buffer = rest
      return frames
        .map(parseSSEBlock)
        .filter((event): event is SSEEvent => event !== null)
    },
  }
}
