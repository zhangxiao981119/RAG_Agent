import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Drawer,
  Empty,
  Input,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd'
import { SendOutlined, StopOutlined } from '@ant-design/icons'

import {
  ChatEvent,
  Citation,
  fetchKbs,
  KnowledgeBase,
  mockAsk,
  User,
} from '../mocks/data'

const { Text } = Typography

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
    {
      id: 'welcome',
      role: 'assistant',
      text: `你好 ${currentUser.display_name}，我是知识库问答助手。请选择知识库后提问。`,
    },
  ])
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [selectedKbs, setSelectedKbs] = useState<string[]>([])
  const [sidebarCitation, setSidebarCitation] = useState<Citation | null>(null)
  const [streaming, setStreaming] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  // 知识库列表加载失败用 Alert 展示（页面级错误，不适合一闪而过的 message）
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    fetchKbs()
      .then((list) => {
        setKbs(list)
        // 默认选中所有知识库
        setSelectedKbs(list.map((kb) => kb.id))
      })
      .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)))
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
        prev.map((m) => {
          if (m.id !== assistantMsg.id) return m
          return refused
            ? {
                ...m,
                loading: false,
                refused: true,
                refusedMessage: refusedMessage ?? '知识库中未找到相关内容',
                text: '',
              }
            : { ...m, loading: false, text: buffer || '(空)', citations }
        }),
      )
    } catch (e) {
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantMsg.id ? { ...m, loading: false, text: '' } : m)),
      )
      // 输出错误气泡（同时保留在对话流中，比顶部红框更贴近出错位置）
      setMessages((prev) =>
        prev.concat({
          id: `err_${Date.now()}`,
          role: 'assistant',
          text: '',
          refused: true,
          refusedMessage: e instanceof Error ? e.message : '请求失败，请稍后重试',
        }),
      )
    } finally {
      setStreaming(false)
      abortRef.current = null
    }
  }

  function handleStop() {
    abortRef.current?.abort()
  }

  return (
    <div className="chat-grid">
      {/* 窄屏隐藏右侧常驻栏，引用通过 Drawer 查看 */}
      <style>{`
        .chat-grid { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 16px; }
        @media (max-width: 992px) {
          .chat-grid { grid-template-columns: 1fr; }
          .chat-citation-aside { display: none; }
        }
      `}</style>

      <div style={{ minWidth: 0 }}>
        {/* 知识库选择 */}
        <Card size="small" style={{ marginBottom: 16 }} styles={{ body: { padding: 12 } }}>
          <Text strong style={{ display: 'block', marginBottom: 8, fontSize: 13 }}>
            选择知识库
          </Text>
          <Checkbox.Group
            value={selectedKbs}
            onChange={(values) => setSelectedKbs(values as string[])}
            options={kbs.map((kb) => ({
              value: kb.id,
              label: (
                <Space size={6}>
                  <span>{kb.name}</span>
                  {kb.is_public && <Tag color="blue" style={{ marginInlineEnd: 0 }}>公开</Tag>}
                </Space>
              ),
            }))}
          />
        </Card>

        {/* 对话区 */}
        <Card variant="borderless" styles={{ body: { padding: 0 } }}>
          <div style={{ height: 'calc(100vh - 360px)', minHeight: 320, overflowY: 'auto', padding: 16 }}>
            {loadError && <Alert type="error" showIcon message={loadError} style={{ marginBottom: 12 }} />}
            {messages.map((m) => (
              <MessageBubble
                key={m.id}
                msg={m}
                onOpenCitation={(c) => setSidebarCitation(c)}
              />
            ))}
            <div ref={messagesEndRef} />
          </div>

          <div style={{ borderTop: '1px solid #f0f0f0', padding: 16 }}>
            <Space.Compact style={{ width: '100%' }}>
              <Input.TextArea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    void handleAsk()
                  }
                }}
                placeholder="输入你的问题，Enter 发送，Shift+Enter 换行"
                autoSize={{ minRows: 1, maxRows: 4 }}
                disabled={streaming}
              />
              {streaming ? (
                <Button danger icon={<StopOutlined />} onClick={handleStop} style={{ height: 'auto' }}>
                  停止
                </Button>
              ) : (
                <Button
                  type="primary"
                  icon={<SendOutlined />}
                  onClick={() => void handleAsk()}
                  disabled={!input.trim()}
                  style={{ height: 'auto' }}
                >
                  提问
                </Button>
              )}
            </Space.Compact>
          </div>
        </Card>
      </div>

      {/* 宽屏：右侧常驻引用面板 */}
      <aside className="chat-citation-aside" style={{ minWidth: 0 }}>
        <Card variant="borderless" style={{ height: '100%' }} styles={{ body: { padding: 16 } }}>
          <CitationPanel citation={sidebarCitation} onClose={() => setSidebarCitation(null)} />
        </Card>
      </aside>

      {/* 窄屏：引用抽屉 */}
      <Drawer
        open={sidebarCitation !== null}
        onClose={() => setSidebarCitation(null)}
        title="原文片段"
        width={420}
        placement="right"
      >
        <CitationPanel citation={sidebarCitation} onClose={() => setSidebarCitation(null)} />
      </Drawer>
    </div>
  )
}

// 引用内容面板（宽屏侧栏与窄屏抽屉共用）
function CitationPanel({ citation, onClose }: { citation: Citation | null; onClose: () => void }) {
  if (!citation) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="点击答案中的引用编号可查看原文片段"
        style={{ marginTop: 80 }}
      />
    )
  }
  return (
    <div>
      <div style={{ background: '#e6f4ff', borderRadius: 8, padding: 12 }}>
        <div style={{ fontWeight: 600, color: '#0958d9' }}>{citation.filename}</div>
        {citation.heading_path && (
          <div style={{ marginTop: 4, color: '#1677ff', fontSize: 13 }}>{citation.heading_path}</div>
        )}
        <div style={{ marginTop: 4, color: '#4096ff', fontSize: 12 }}>
          {citation.page_no != null && <>第 {citation.page_no} 页 · </>}
          相关度 {citation.score.toFixed(2)}
        </div>
      </div>
      <div
        style={{
          marginTop: 12,
          background: '#fafafa',
          borderRadius: 8,
          padding: 12,
          lineHeight: 1.7,
          fontSize: 13,
          color: 'rgba(0,0,0,0.75)',
          whiteSpace: 'pre-wrap',
        }}
      >
        {citation.snippet}
      </div>
      <Button type="link" size="small" style={{ paddingLeft: 0, marginTop: 8 }} onClick={onClose}>
        关闭
      </Button>
    </div>
  )
}

// 消息气泡
function MessageBubble({
  msg,
  onOpenCitation,
}: {
  msg: Message
  onOpenCitation: (c: Citation) => void
}) {
  if (msg.role === 'user') {
    return (
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <div
          style={{
            maxWidth: '80%',
            background: '#1677ff',
            color: '#fff',
            borderRadius: 12,
            borderTopRightRadius: 2,
            padding: '8px 14px',
            fontSize: 14,
            whiteSpace: 'pre-wrap',
          }}
        >
          {msg.text}
        </div>
      </div>
    )
  }

  // 拒答 / 出错提示（手册 M1 验收 B2）
  if (msg.refused) {
    return (
      <div style={{ display: 'flex', marginBottom: 12 }}>
        <Alert
          type="warning"
          showIcon
          style={{ maxWidth: '80%' }}
          message="知识库中未找到相关内容"
          description={msg.refusedMessage || '换个问题试试？或者检查知识库选择是否正确。'}
        />
      </div>
    )
  }

  if (msg.loading) {
    return (
      <div style={{ display: 'flex', marginBottom: 12 }}>
        <div
          style={{
            background: '#f5f5f5',
            borderRadius: 12,
            borderTopLeftRadius: 2,
            padding: '10px 14px',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            color: 'rgba(0,0,0,0.45)',
            fontSize: 14,
          }}
        >
          <Spin size="small" />
          思考中...
        </div>
      </div>
    )
  }

  // 渲染答案：按换行分段，每段内把 [n] 替换成可点击的引用编号
  const segments = msg.text.split('\n').filter((s) => s.length > 0)

  function renderInline(text: string) {
    const parts: React.ReactNode[] = []
    const regex = /\[(\d+)\]/g
    let lastIndex = 0
    let keyIdx = 0
    let m: RegExpExecArray | null
    while ((m = regex.exec(text)) !== null) {
      if (m.index > lastIndex) parts.push(text.slice(lastIndex, m.index))
      const n = Number(m[1])
      const citation = msg.citations?.find((c) => c.n === n)
      if (citation) {
        parts.push(
          <Tag
            key={`ref-${keyIdx++}`}
            color="blue"
            style={{ cursor: 'pointer', marginInline: 2, borderRadius: 10 }}
            onClick={() => onOpenCitation(citation)}
          >
            {n}
          </Tag>,
        )
      } else {
        parts.push(`[${n}]`)
      }
      lastIndex = m.index + m[0].length
    }
    if (lastIndex < text.length) parts.push(text.slice(lastIndex))
    return parts
  }

  return (
    <div style={{ display: 'flex', marginBottom: 12 }}>
      <div
        style={{
          maxWidth: '80%',
          background: '#f5f5f5',
          borderRadius: 12,
          borderTopLeftRadius: 2,
          padding: '10px 14px',
          fontSize: 14,
          lineHeight: 1.7,
          color: 'rgba(0,0,0,0.85)',
        }}
      >
        {segments.map((seg, i) => (
          <div key={i} style={{ marginTop: i > 0 ? 6 : 0 }}>
            {renderInline(seg)}
          </div>
        ))}
      </div>
    </div>
  )
}
