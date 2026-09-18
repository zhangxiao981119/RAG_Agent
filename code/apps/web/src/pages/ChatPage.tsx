import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
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
  /** 流式打字完成（终态落定后为 true，操作栏仅在完成后可用） */
  done?: boolean
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
  // 输入区容器：追问时滚动进视野（长对话时输入框可能在视口外）
  const inputAreaRef = useRef<HTMLDivElement>(null)

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
    // 打字机期间每 33ms 更新一次消息，auto 瞬时贴底开销小；smooth 高频调用会抖动
    messagesEndRef.current?.scrollIntoView({ behavior: 'auto' })
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

  /** 追问：把该回答对应的原问题填入输入框（可直接修改/补充后发送），并滚动聚焦输入区。 */
  function handleFollowUp(msg: Message) {
    const q = findQuestionOf(msg.id)
    setInput(q ?? '')
    inputAreaRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    inputRef.current?.focus()
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
    let sseDone = false

    // 确定性打字机：前端严格按约 30 字/秒渲染缓冲，积压过大时加速；
    // SSE 结束后必须等缓冲打完才落终态，避免 flush 跳字破坏打字机观感
    const typer = window.setInterval(() => {
      if (shown >= buffer.length) {
        if (!sseDone) return
        // 缓冲已全部打出且流已结束：落终态（citations / refused）
        clearInterval(typer)
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== assistantMsg.id) return m
            return refused
              ? {
                  ...m,
                  loading: false,
                  done: true,
                  refused: true,
                  refusedMessage: refusedMessage ?? '知识库中未找到相关内容',
                  text: '',
                }
              : { ...m, loading: false, done: true, text: buffer || '(空)', citations }
          }),
        )
        setStreaming(false)
        abortRef.current = null
        return
      }
      const diff = buffer.length - shown
      const step = diff > 150 ? 5 : 1
      shown = Math.min(buffer.length, shown + step)
      const visible = buffer.slice(0, shown)
      setMessages((prev) =>
        // 打字开始即清 loading：气泡组件在 loading 态只渲染"思考中"，会遮住打字文本
        prev.map((m) => (m.id === assistantMsg.id ? { ...m, loading: false, text: visible } : m)),
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
      clearInterval(typer)
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
      setStreaming(false)
      abortRef.current = null
      return
    }
    // SSE 正常结束：置位后由 typer 在缓冲打完时落终态（见 interval 内）
    sseDone = true
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
                onFollowUp={handleFollowUp}
                onAdopt={handleAdopt}
              />
            ))}
            <div ref={messagesEndRef} />
          </div>

          <div ref={inputAreaRef} style={{ borderTop: '1px solid #f0f0f0', padding: 16 }}>
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

// ─────────────────────────────────────────────────────────────
// rehype 插件：把 markdown 文本节点里的 [n] 引用标号替换为 sup.cite-ref 元素，
// 由下方 ReactMarkdown components.sup 接管渲染（点击打开引用）。
// 手写递归遍历，避开 unist-util-visit 类型签名反复卡壳。
// ─────────────────────────────────────────────────────────────
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function rehypeCitation() {
  return (tree: any) => {
    const walk = (node: any, parent: any, index: number | null) => {
      if (!node || typeof node !== 'object') return
      if (node.type === 'text' && typeof node.value === 'string' && /\[\d+\]/.test(node.value)) {
        const newChildren: any[] = []
        let last = 0
        for (const m of node.value.matchAll(/\[(\d+)\]/g)) {
          const i = m.index ?? 0
          if (i > last) newChildren.push({ type: 'text', value: node.value.slice(last, i) })
          newChildren.push({
            type: 'element',
            tagName: 'sup',
            properties: { className: ['cite-ref'], dataCite: m[1] },
            children: [{ type: 'text', value: m[1] }],
          })
          last = i + m[0].length
        }
        if (last < node.value.length) newChildren.push({ type: 'text', value: node.value.slice(last) })
        if (parent && index !== null && Array.isArray(parent.children)) {
          parent.children.splice(index, 1, ...newChildren)
          // 替换后从新增节点继续往下走，跳过刚插入的纯文本/元素
          for (let k = index; k < index + newChildren.length; k++) {
            walk(newChildren[k], parent, k)
          }
          return
        }
      }
      if (Array.isArray(node.children)) {
        for (let i = 0; i < node.children.length; i++) {
          walk(node.children[i], node, i)
        }
      }
    }
    walk(tree, null, null)
  }
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
  onFollowUp: (m: Message) => void
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

  // 正常回答：气泡（Markdown 渲染）+ 底部操作栏（复制/重试/追问/采纳）
  const canOperate = !msg.loading && msg.done && !msg.refused && !!msg.messageId && !!msg.text

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
            color: 'rgba(0,0,0,0.85)',
          }}
        >
          <div className="md-body">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeCitation]}
              components={{
                sup: ({ node, children }) => {
                  // eslint-disable-next-line @typescript-eslint/no-explicit-any
                  const n = Number((node as any)?.properties?.dataCite ?? 0)
                  const citation = msg.citations?.find((c) => c.n === n)
                  if (!citation) return <sup>{children}</sup>
                  return (
                    <sup
                      className="cite-ref"
                      title={citation.filename}
                      onClick={() => onOpenCitation(citation)}
                    >
                      {n}
                    </sup>
                  )
                },
              }}
            >
              {msg.text}
            </ReactMarkdown>
          </div>
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
            <Button type="text" size="small" icon={<MessageOutlined />} onClick={() => onFollowUp(msg)}>
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
