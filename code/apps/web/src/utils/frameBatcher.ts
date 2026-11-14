/**
 * 每帧最多执行一次的可取消批处理器。
 *
 * 流式输出时 token 到达频率远高于屏幕刷新率，逐个 setState 会让一帧内
 * 重复渲染同一个气泡。把增量累积到内存、由 requestAnimationFrame 每帧
 * 统一刷新一次，渲染节奏就与屏幕刷新率对齐。
 */
export function createFrameBatcher(flush: () => void) {
  let frameId: number | null = null
  return {
    /** 同一帧内重复调用只会安排一次刷新。 */
    schedule(): void {
      if (frameId !== null) return
      frameId = requestAnimationFrame(() => {
        frameId = null
        flush()
      })
    },
    /** 取消尚未执行的刷新，避免组件卸载或流结束后触发渲染。 */
    cancel(): void {
      if (frameId === null) return
      cancelAnimationFrame(frameId)
      frameId = null
    },
  }
}
