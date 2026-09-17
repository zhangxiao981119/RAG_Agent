import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Drawer,
  Empty,
  Input,
  Popconfirm,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  CheckOutlined,
  CopyOutlined,
  LikeOutlined,
  MessageOutlined,
  RedoOutlined,
  SendOutlined,
  StopOutlined,
} from '@ant-design/icons'

import {
  adoptAnswer,
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
  /** 后端 assistant message id（meta 事件带回），采纳/复制等操作依赖它 */
  messageId?: string
  /** 已采纳为微调样本 */
  adopted?: boolean
}

type Props = { currentUser: User }

export function ChatPage({ currentUser }: Props) {
  const { message } = App.useApp()
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
  /** 会话 id：首轮提问后由 meta 事件带回，后续追问续传（上下文连续） */
  const [conversationId, setConversationId] = useState<string | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

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

  /** 找某条 assistant 消息对应的提问（向前最近一条 user 消息）。 */
  function findQuestionOf(assistantMsgId: string): string | null {
    const idx = messages.findIndex((m) => m.id === assistantMsgId)
    for (let i = idx - 1; i >= 0; i--) {
      if (messages[i].role === 'user') return messages[i].text
    }
    return null
  }

  /** 采纳回答为微调样本（幂等，后端唯一约束兜底）。 */
  async function handleAdopt(msg: Message) {
    if (!msg.messageId) return
    try {
      const r = await adoptAnswer(msg.messageId)
      if (r.already_adopted) {
        message.info('该回答已采纳过')
      } else {
        message.success('已采纳，问答对已收集为微调样本')
      }
      setMessages((prev) => prev.map((m) => (m.id === msg.id ? { ...m, adopted: true } : m)))
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    }
  }

  /** 复制回答全文。 */
  async function handleCopy(msg: Message) {
    try {
      await navigator.clipboard.writeText(msg.text)
      message.success('已复制')
    } catch {
      message.error('复制失败，请手动选择文本复制')
    }
  }

  async function askQuestion(question: string) {
    if (!question || streaming) return

    const userMsg: Message = { id: `u_${Date.now()}`, role: 'user', text: question }
    const assistantMsg: Message = { id: `a_${Date.now()}`, role: 'assistant', text: '', loading: true }
    setMessages((prev) => [...prev, userMsg, assistantMsg])
    setInput('')
    setStreaming(true)

    const controller = new AbortController()
    abortRef.current = controller

    let buffer = ''
    let shown = 0
    let citations: Citation[] | undefined
    let refused = false
    let refusedMessage: string | undefined

    // 打字机：约 30 字/秒渲染 delta 缓冲；落后过多时按比例加速追赶
    const typer = window.setInterval(() => {
      if (buffer.length <= shown) return
      const step = Math.max(1, Math.ceil((buffer.length - shown) / 30))
      shown = Math.min(buffer.length, shown + step)
      const visible = buffer.slice(0, shown)
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantMsg.id ? { ...m, text: visible } : m)),
      )
    }, 33)

    const handler = (event: ChatEvent) => {
      if (event.event === 'meta') {
        // 首轮事件带回后端会话/消息 id：会话续传用，采纳用
        setConversationId(event.data.conversation_id)
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMsg.id ? { ...m, messageId: event.data.message_id } : m,
          ),
        )
      } else if (event.event === 'citations') citations = event.data.citations
      else if (event.event === 'delta') buffer += event.data.text
      else if (event.event === 'refused') {
        refused = true
        refusedMessage = event.data.message
      }
    }

    try {
      await mockAsk(question, selectedKbs, conversationId, handler, controller.signal)
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
      clearInterval(typer)
    }

    // 流结束：flush 全文（打字机残余 + citations / refused 终态）
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
    setStreaming(false)
    abortRef.current = null
  }

  async function handleAsk() {
    await askQuestion(input.trim())
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
                onCopy={handleCopy}
                onRetry={(msg) => {
                  const q = findQuestionOf(msg.id)
                  if (q) void askQuestion(q)
                  else message.warning('未找到原始问题')
                }}
                onFollowUp={() => inputRef.current?.focus()}
                onAdopt={handleAdopt}
              />
            ))}
            <div ref={messagesEndRef} />
          </div>

          <div style={{ borderTop: '1px solid #f0f0f0', padding: 16 }}>
            <Space.Compact style={{ width: '100%' }}>
              <Input.TextArea
                ref={inputRef}
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
  onCopy,
  onRetry,
  onFollowUp,
  onAdopt,
}: {
  msg: Message
  onOpenCitation: (c: Citation) => void
  onCopy: (m: Message) => void
  onRetry: (m: Message) => void
  onFollowUp: () => void
  onAdopt: (m: Message) => void
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

  // 正常回答：气泡 + 底部操作栏（复制/重试/追问/采纳）
  const canOperate = !msg.loading && !msg.refused && !!msg.messageId && !!msg.text
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
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: 'flex' }}>
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
      {canOperate && (
        <Space size={0} style={{ marginLeft: 4, marginTop: 2 }}>
          <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => onCopy(msg)}>
            复制
          </Button>
          <Button type="text" size="small" icon={<RedoOutlined />} onClick={() => onRetry(msg)}>
            重试
          </Button>
          <Tooltip title="继续就此话题提问">
            <Button type="text" size="small" icon={<MessageOutlined />} onClick={onFollowUp}>
              追问
            </Button>
          </Tooltip>
          {msg.adopted ? (
            <Tooltip title="该问答对已收集为微调样本，微调时由管理员导出">
              <span>
                <Button type="text" size="small" icon={<CheckOutlined />} disabled>
                  已采纳
                </Button>
              </span>
            </Tooltip>
          ) : (
            <Popconfirm
              title="采纳这条回答？"
              description="问答对将收集为微调样本，供后续模型微调使用。"
              okText="采纳"
              cancelText="取消"
              onConfirm={() => onAdopt(msg)}
            >
              <Button type="text" size="small" icon={<LikeOutlined />}>
                采纳
              </Button>
            </Popconfirm>
          )}
        </Space>
      )}
    </div>
  )
}
