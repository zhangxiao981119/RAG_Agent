import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createFrameBatcher } from './frameBatcher'

type Frame = { id: number; callback: FrameRequestCallback }

let frames: Frame[] = []
let cancelled: Set<number>
let nextId = 1

beforeEach(() => {
  frames = []
  cancelled = new Set()
  nextId = 1
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    const id = nextId++
    frames.push({ id, callback })
    return id
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => cancelled.add(id))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

/** 模拟浏览器渲染一帧：执行本帧已登记且未被取消的回调。 */
function runFrame() {
  const pending = frames
  frames = []
  for (const frame of pending) {
    if (!cancelled.has(frame.id)) frame.callback(frame.id)
  }
}

describe('createFrameBatcher', () => {
  it('同一帧内多次调度只刷新一次', () => {
    const flush = vi.fn()
    const batcher = createFrameBatcher(flush)

    batcher.schedule()
    batcher.schedule()
    batcher.schedule()

    expect(flush).not.toHaveBeenCalled()
    runFrame()
    expect(flush).toHaveBeenCalledTimes(1)
  })

  it('下一帧可以再次调度刷新', () => {
    const flush = vi.fn()
    const batcher = createFrameBatcher(flush)

    batcher.schedule()
    runFrame()
    batcher.schedule()
    runFrame()

    expect(flush).toHaveBeenCalledTimes(2)
  })

  it('cancel 后未执行的刷新不再触发', () => {
    const flush = vi.fn()
    const batcher = createFrameBatcher(flush)

    batcher.schedule()
    batcher.cancel()
    runFrame()

    expect(flush).not.toHaveBeenCalled()
  })

  it('cancel 之后仍可重新调度', () => {
    const flush = vi.fn()
    const batcher = createFrameBatcher(flush)

    batcher.schedule()
    batcher.cancel()
    batcher.schedule()
    runFrame()

    expect(flush).toHaveBeenCalledTimes(1)
  })

  it('flush 抛异常后仍可继续调度', () => {
    const flush = vi.fn(() => {
      throw new Error('渲染失败')
    })
    const batcher = createFrameBatcher(flush)

    batcher.schedule()
    expect(() => runFrame()).toThrow('渲染失败')
    batcher.schedule()
    expect(() => runFrame()).toThrow('渲染失败')

    expect(flush).toHaveBeenCalledTimes(2)
  })

  it('未调度时 cancel 是安全的空操作', () => {
    const batcher = createFrameBatcher(vi.fn())

    expect(() => batcher.cancel()).not.toThrow()
  })
})
