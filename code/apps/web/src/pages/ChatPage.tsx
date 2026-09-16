import { useEffect, useRef, useState } from 'react'

import { mockAsk, ChatEvent, Citation, KnowledgeBase, fetchKbs, User } from '../mocks/data'

type Message = {
  id: string
  role: 'user' | 'assistant'
  text: string
  citations?: Citation[]
  refused?: boolean
  refusedMessage?: string
  loading?: boolean
}

type Props = { currentUser: User }

export function ChatPage({ currentUser }: Props) {
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState<Message[]>([
    { id: 'welcome', role: 'assistant', text: `你好 ${currentUser.display_name}，我是知识库问答助手。请选择知识库后提问。` },
  ])
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [selectedKbs, setSelectedKbs] = useState<string[]>([])
  const [sidebarCitation, setSidebarCitation] = useState<Citation | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [streaming, setStreaming] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    fetchKbs().then((list) => {
      setKbs(list)
      // 默认选中所有知识库（M2 无权限，全选）
      setSelectedKbs(list.map((kb) => kb.id))
    }).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function handleAsk() {
    const question = input.trim()
    if (!question || streaming) return

    const userMsg: Message = { id: `u_${Date.now()}`, role: 'user', text: question }
    const assistantMsg: Message = { id: `a_${Date.now()}`, role: 'assistant', text: '', loading: true }
    setMessages((prev) => [...prev, userMsg, assistantMsg])
    setInput('')
    setError(null)
    setStreaming(true)

    const controller = new AbortController()
    abortRef.current = controller

    let buffer = ''
    let citations: Citation[] | undefined
    let refused = false
    let refusedMessage: string | undefined

    const handler = (event: ChatEvent) => {
      if (event.event === 'citations') citations = event.data.citations
      else if (event.event === 'delta') buffer += event.data.text
      else if (event.event === 'refused') {
        refused = true
        refusedMessage = event.data.message
      }
    }

    try {
      await mockAsk(question, selectedKbs, handler, controller.signal)

      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMsg.id
            ? refused
              ? { ...m, loading: false, refused: true, refusedMessage: refusedMessage ?? '知识库中未找到相关内容', text: '' }
              : { ...m, loading: false, text: buffer || '(空)', citations }
            : m,
        ),
      )
    } catch (e: any) {
      setMessages((prev) => prev.map((m) => (m.id === assistantMsg.id ? { ...m, loading: false, text: '' } : m)))
      setError(e.message || '请求失败')
    } finally {
      setStreaming(false)
      abortRef.current = null
    }
  }

  function handleStop() {
    abortRef.current?.abort()
  }

  function toggleKb(kbId: string) {
    setSelectedKbs((prev) =>
      prev.includes(kbId) ? prev.filter((id) => id !== kbId) : [...prev, kbId],
    )
  }

  return (
    <>
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_360px]">
      {/* 左侧：知识库选择 + 对话 */}
      <div className="min-w-0">
        <section className="mb-4 rounded-xl border border-slate-200 bg-white p-4">
          <h3 className="mb-2 text-sm font-semibold text-slate-700">选择知识库</h3>
          <div className="flex flex-wrap gap-2">
            {kbs.map((kb) => (
              <label key={kb.id} className="flex cursor-pointer items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-1.5 text-sm hover:bg-slate-100">
                <input
                  type="checkbox"
                  className="h-4 w-4 rounded border-slate-300 text-blue-600"
                  checked={selectedKbs.includes(kb.id)}
                  onChange={() => toggleKb(kb.id)}
                />
                <span className="text-slate-700">{kb.name}</span>
                {kb.is_public && <span className="rounded bg-blue-100 px-1.5 py-0.5 text-xs text-blue-700">公开</span>}
              </label>
            ))}
          </div>
        </section>

        <section className="rounded-xl border border-slate-200 bg-white">
          <div className="h-[calc(100vh-420px)] min-h-[300px] space-y-4 overflow-y-auto p-4">
            {messages.map((m) => (
              <MessageBubble
                key={m.id}
                msg={m}
                onOpenCitation={(c) => setSidebarCitation(c)}
              />
            ))}
            <div ref={messagesEndRef} />
          </div>

          {error && (
            <div className="border-t border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>
          )}

          <div className="border-t border-slate-200 p-4">
            <div className="flex gap-2">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    handleAsk()
                  }
                }}
                placeholder="输入你的问题，Enter 发送，Shift+Enter 换行"
                className="flex-1 resize-none rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
                rows={2}
                disabled={streaming}
              />
              {streaming ? (
                <button
                  onClick={handleStop}
                  className="rounded-lg bg-slate-700 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800"
                >
                  停止
                </button>
              ) : (
                <button
                  onClick={handleAsk}
                  disabled={!input.trim()}
                  className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-60"
                >
                  提问
                </button>
              )}
            </div>
          </div>
        </section>
      </div>

      {/* 右侧：引用抽屉（宽屏侧栏） */}
      <aside className="hidden rounded-xl border border-slate-200 bg-white lg:block">
        {sidebarCitation ? (
          <div className="p-4">
            <CitationPanel citation={sidebarCitation} onClose={() => setSidebarCitation(null)} />
          </div>
        ) : (
          <CitationPanel citation={null} onClose={() => {}} />
        )}
      </aside>
    </div>

    {/* 窄屏：点击引用编号弹出 bottom-sheet overlay */}
    {sidebarCitation && (
      <div
        className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 block lg:hidden"
        onClick={() => setSidebarCitation(null)}
      >
        <div className="w-full max-h-[80vh] overflow-y-auto rounded-t-2xl border border-slate-200 bg-white" onClick={(e) => e.stopPropagation()}>
          <div className="border-b border-slate-200 px-5 py-3">
            <div className="h-1.5 w-10 mx-auto rounded-full bg-slate-300" />
          </div>
          <div className="p-5">
            <h3 className="mb-3 text-sm font-semibold text-slate-700">原文片段</h3>
            <CitationPanel citation={sidebarCitation} onClose={() => setSidebarCitation(null)} />
          </div>
        </div>
      </div>
    )}
    </>
  )
}

function CitationPanel({ citation, onClose }: { citation: Citation | null; onClose: () => void }) {
  if (!citation) {
    return (
      <div className="rounded-xl border border-slate-200 bg-white p-10 text-center text-sm text-slate-400">
        点击答案中的引用编号可查看原文片段
      </div>
    )
  }
  return (
    <div className="space-y-3 text-sm">
      <div className="mb-3 flex items-center justify-between">
        <div />
        <button
          onClick={onClose}
          className="text-slate-400 hover:text-slate-600"
        >
          x
        </button>
      </div>
      <div className="rounded-lg bg-blue-50 p-3">
        <div className="font-medium text-blue-900">{citation.filename}</div>
        {citation.heading_path && (
          <div className="mt-1 text-blue-700">{citation.heading_path}</div>
        )}
        <div className="mt-1 text-xs text-blue-600">第 {citation.page_no} 页 · 相关度 {citation.score.toFixed(2)}</div>
      </div>
      <div className="rounded-lg bg-slate-50 p-3 leading-relaxed text-slate-700">
        {citation.snippet}
      </div>
    </div>
  )
}

function MessageBubble({ msg, onOpenCitation }: { msg: Message; onOpenCitation: (c: Citation) => void }) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-xl bg-blue-600 px-4 py-2 text-sm text-white">{msg.text}</div>
      </div>
    )
  }

  // 拒答专用样式（手册 M1 验收 B2）
  if (msg.refused) {
    return (
      <div className="flex">
        <div className="flex max-w-[80%] items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          <span className="mt-0.5 text-base">!</span>
          <div>
            <div className="font-medium">知识库中未找到相关内容</div>
            <div className="mt-1 text-xs text-amber-600">换个问题试试？或者检查知识库选择是否正确。</div>
          </div>
        </div>
      </div>
    )
  }

  if (msg.loading) {
    return (
      <div className="flex">
        <div className="rounded-xl bg-slate-100 px-4 py-2 text-sm text-slate-500">思考中...</div>
      </div>
    )
  }

  // 渲染答案，把 [n] 替换成可点击的引用编号
  const parts: React.ReactNode[] = []
  const regex = /\[(\d+)\]/g
  let lastIndex = 0
  let m: RegExpExecArray | null
  let keyIdx = 0
  while ((m = regex.exec(msg.text)) !== null) {
    if (m.index > lastIndex) parts.push(msg.text.slice(lastIndex, m.index))
    const n = Number(m[1])
    const citation = msg.citations?.find((c) => c.n === n)
    if (citation) {
      parts.push(
        <button
          key={`ref-${keyIdx++}`}
          onClick={() => onOpenCitation(citation)}
          className="mx-0.5 inline-flex h-5 min-w-[1.25rem] items-center justify-center rounded-full bg-blue-100 px-1.5 text-xs font-medium text-blue-700 hover:bg-blue-200"
        >
          {n}
        </button>,
      )
    } else {
      parts.push(`[${n}]`)
    }
    lastIndex = m.index + m[0].length
  }
  if (lastIndex < msg.text.length) parts.push(msg.text.slice(lastIndex))

  return (
    <div className="flex">
      <div className="max-w-[80%] rounded-xl bg-slate-100 px-4 py-2 text-sm text-slate-800 leading-relaxed">
        {parts}
      </div>
    </div>
  )
}
