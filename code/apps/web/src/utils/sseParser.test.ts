import { describe, expect, it, vi } from 'vitest'
import { createSSEParser } from './sseParser'

const encoder = new TextEncoder()

/** 把完整 SSE 文本编码后按字节切点分片，用于制造跨包边界。 */
function splitBytes(text: string, ...cuts: number[]): Uint8Array[] {
  const bytes = encoder.encode(text)
  const points = [0, ...cuts, bytes.length]
  return points.slice(0, -1).map((start, i) => bytes.slice(start, points[i + 1]))
}

function parseChunks(chunks: Uint8Array[]) {
  const parser = createSSEParser()
  return chunks.flatMap((chunk) => parser.push(chunk))
}

describe('createSSEParser', () => {
  it('解析完整事件并保持到达顺序', () => {
    const events = parseChunks([
      encoder.encode(
        'event: meta\ndata: {"stage":"retrieving"}\n\n' +
          'event: delta\ndata: {"text":"你好"}\n\n' +
          'event: done\ndata: {"usage":{}}\n\n',
      ),
    ])

    expect(events.map((event) => event.event)).toEqual(['meta', 'delta', 'done'])
    expect(events[1].data).toEqual({ text: '你好' })
  })

  it('多字节字符跨包时不解出乱码', () => {
    // 在"标"字的 UTF-8 三字节序列内部切开
    const prefix = 'event: delta\ndata: {"text":"报销'
    const events = parseChunks(
      splitBytes('event: delta\ndata: {"text":"报销标准"}\n\n', encoder.encode(prefix).length + 1),
    )

    expect(events).toHaveLength(1)
    expect(events[0].data).toEqual({ text: '报销标准' })
  })

  it('未完成事件留到下一包，不会提前产出', () => {
    const text = 'event: delta\ndata: {"text":"第一段"}\n\n'
    const parts = splitBytes(text, encoder.encode(text).length - 5)
    const parser = createSSEParser()

    expect(parser.push(parts[0])).toHaveLength(0)
    expect(parser.push(parts[1]).map((event) => event.event)).toEqual(['delta'])
  })

  it('兼容代理改写后的 CRLF 分帧', () => {
    const events = parseChunks([
      encoder.encode('event: delta\r\ndata: {"text":"换行"}\r\n\r\n'),
    ])

    expect(events).toHaveLength(1)
    expect(events[0].data).toEqual({ text: '换行' })
  })

  it('多行 data 按规范以换行拼接', () => {
    const events = parseChunks([
      encoder.encode('event: delta\ndata: {"text":\ndata: "多行"}\n\n'),
    ])

    expect(events[0].data).toEqual({ text: '多行' })
  })

  it('允许 data: 与 event: 后没有空格', () => {
    const events = parseChunks([
      encoder.encode('event:delta\ndata:{"text":"紧凑"}\n\n'),
    ])

    expect(events[0]).toEqual({ event: 'delta', data: { text: '紧凑' } })
  })

  it('忽略心跳注释行，不影响后续事件', () => {
    const events = parseChunks([
      encoder.encode(': ping\n\nevent: delta\ndata: {"text":"正常"}\n\n'),
    ])

    expect(events).toHaveLength(1)
    expect(events[0].data).toEqual({ text: '正常' })
  })

  it('特殊字符经 JSON 转义后原样还原', () => {
    const payload = { text: '他说："第一行\n第二行" \\ 结束 🚀' }
    const events = parseChunks([
      encoder.encode(`event: delta\ndata: ${JSON.stringify(payload)}\n\n`),
    ])

    expect(events[0].data).toEqual(payload)
  })

  it('非法 JSON 只丢弃当前事件，后续事件照常解析', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const events = parseChunks([
      encoder.encode(
        'event: delta\ndata: {oops}\n\nevent: done\ndata: {"ok":true}\n\n',
      ),
    ])
    spy.mockRestore()

    expect(events.map((event) => event.event)).toEqual(['done'])
    expect(events[0].data).toEqual({ ok: true })
  })

  it('逐字节喂入也能还原完整事件', () => {
    const payload = { text: '逐字节🚀' }
    const bytes = encoder.encode(
      `event: delta\ndata: ${JSON.stringify(payload)}\n\n`,
    )
    const chunks: Uint8Array[] = []
    for (let i = 0; i < bytes.length; i += 1) chunks.push(bytes.slice(i, i + 1))

    const events = parseChunks(chunks)

    expect(events).toHaveLength(1)
    expect(events[0].data).toEqual(payload)
  })

  it('同一分片包含多个事件时按序全部产出', () => {
    const events = parseChunks([
      encoder.encode(
        'event: a\ndata: {"i":1}\n\n' +
          'event: b\ndata: {"i":2}\n\n' +
          'event: c\ndata: {"i":3}\n\n',
      ),
    ])

    expect(events.map((event) => event.data)).toEqual([
      { i: 1 },
      { i: 2 },
      { i: 3 },
    ])
  })

  it('心跳与业务事件交错时只产出业务事件', () => {
    const events = parseChunks([
      encoder.encode(
        ': ping\n\nevent: delta\ndata: {"text":"前"}\n\n' +
          ': ping\n\nevent: delta\ndata: {"text":"后"}\n\n',
      ),
    ])

    expect(
      events.map((event) => (event.data as { text: string }).text),
    ).toEqual(['前', '后'])
  })

  it('长文本被拆成多片后仍完整拼接', () => {
    const longText = '段落'.repeat(500)
    const bytes = encoder.encode(
      `event: delta\ndata: ${JSON.stringify({ text: longText })}\n\n`,
    )
    const chunks: Uint8Array[] = []
    for (let i = 0; i < bytes.length; i += 7) chunks.push(bytes.slice(i, i + 7))

    const events = parseChunks(chunks)

    expect(events).toHaveLength(1)
    expect(events[0].data).toEqual({ text: longText })
  })

  it('缺少 data 的事件被忽略', () => {
    expect(parseChunks([encoder.encode('event: ping\n\n')])).toEqual([])
  })
})
