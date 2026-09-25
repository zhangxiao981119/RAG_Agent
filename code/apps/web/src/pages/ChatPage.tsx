import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
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
  DeleteOutlined,
  LikeOutlined,
  MessageOutlined,
  PlusOutlined,
  RedoOutlined,
  SendOutlined,
  StopOutlined,
} from '@ant-design/icons'

import {
  adoptAnswer,
  ChatEvent,
  Citation,
  Conversation,
  ConversationMessage,
  deleteConversation,
  fetchConversationMessages,
  fetchConversations,
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
  /** 快捷追问建议（后端 done 事件带回） */
  suggestions?: string[]
  /** 网络断开导致 SSE 中断：保留已流出的 fullText，提示用户重试 */
  networkError?: boolean
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

  /** 追问上下文：点击追问按钮时展示该条 AI 回答摘要，让用户感知"是在追问哪条" */
  const [followUpContext, setFollowUpContext] = useState<{
    assistantText: string
  } | null>(null)

  // 会话列表（左侧侧边栏）
  const [conversations, setConversations] = useState<Conversation[]>([])

  const abortRef = useRef<AbortController | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const inputAreaRef = useRef<HTMLDivElement>(null)
  /** conversationId 的 ref 镜像：供 SSE 事件回调读取最新值，避免陈旧闭包 */
  const conversationIdRef = useRef<string | null>(null)
  useEffect(() => {
    conversationIdRef.current = conversationId
  }, [conversationId])

  const [loadError, setLoadError] = useState<string | null>(null)

  /** 上下文压缩告警：后端 SSE context_warning 事件触发，建议用户新开对话 */
  const [contextWarning, setContextWarning] = useState<{
    compressionCount: number
    threshold: number
  } | null>(null)

  // 组件卸载：中止进行中的 SSE，避免请求/状态更新泄漏
  useEffect(() => {
    return () => {
      abortRef.current?.abort()
    }
  }, [])

  // 加载知识库列表
  useEffect(() => {
    fetchKbs()
      .then((list) => {
        setKbs(list)
        setSelectedKbs(list.map((kb) => kb.id))
      })
      .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)))
  }, [])

  // 标记是否已完成首次自动选中（只在首次加载会话列表时自动选中最近一条，
  // 后续手动新建对话 / 切换会话都不应再触发自动选中，否则会覆盖"新建对话"状态）
  const hasInitialSelected = useRef(false)

  // 加载会话列表（删会话 / 首轮提问拿到会话 id 后刷新侧边栏）
  // 不依赖 conversationId state，避免 SSE 回调持有陈旧闭包导致误自动选中
  const refreshConversations = useCallback(() => {
    fetchConversations()
      .then((list) => {
        setConversations(list)
        // 仅首次加载：自动选中最近一次对话（列表按 update_time 降序，第一个即最新）
        if (!hasInitialSelected.current && !conversationIdRef.current && list.length > 0) {
          hasInitialSelected.current = true
          handleSelectConversation(list[0])
        }
      })
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    refreshConversations()
    // 仅首次进入页面时触发，后续 conversationId 变化不应触发刷新
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
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

  /** 采纳回答为微调样本。 */
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

  /** 重试：用原问题重新提问。 */
  function handleRetry(msg: Message) {
    const q = findQuestionOf(msg.id)
    if (q) void askQuestion(q)
    else message.warning('未找到原始问题')
  }

  /** 新建对话：清空消息 + 重置会话 id。 */
  function handleNewConversation() {
    if (streaming) {
      message.warning('请等待当前回答结束')
      return
    }
    setConversationId(null)
    setMessages([
      {
        id: 'welcome',
        role: 'assistant',
        text: `你好 ${currentUser.display_name}，我是知识库问答助手。请选择知识库后提问。`,
      },
    ])
    setInput('')
    setSidebarCitation(null)
    setContextWarning(null)  // 新开对话重置上下文告警
  }

  /** 切换到历史会话：加载消息列表。 */
  async function handleSelectConversation(conv: Conversation) {
    if (streaming) {
      message.warning('请等待当前回答结束')
      return
    }
    try {
      const msgs = await fetchConversationMessages(conv.id)
      setConversationId(conv.id)
      setSidebarCitation(null)
      // 后端消息 → 前端 Message 结构
      const mapped: Message[] = msgs.map((m) => {
        const meta = m.meta || {}
        const refused = !!meta.refused
        const suggestions = Array.isArray(meta.suggestions) ? meta.suggestions : []
        return {
          id: m.id,
          role: m.role,
          text: m.content,
          citations: Array.isArray(m.citations) ? m.citations : [],
          refused,
          refusedMessage: refused ? '知识库中未找到相关内容' : undefined,
          done: true,
          messageId: m.id,
          suggestions: refused ? [] : suggestions,
        }
      })
      setMessages(
        mapped.length > 0
          ? mapped
          : [{ id: 'empty', role: 'assistant', text: '此会话暂无消息' }],
      )
    } catch (e) {
      message.error(e instanceof Error ? e.message : '加载失败')
    }
  }

  /** 删除会话。 */
  async function handleDeleteConversation(conv: Conversation, e: React.MouseEvent) {
    e.stopPropagation()
    try {
      await deleteConversation(conv.id)
      message.success('已删除')
      refreshConversations()
      // 如果删的是当前会话，清空
      if (conv.id === conversationId) {
        handleNewConversation()
      }
    } catch (e2) {
      message.error(e2 instanceof Error ? e2.message : '删除失败')
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

    // 提问前是否已有会话：用于"会话 id 从空变非空"的边沿刷新
    const startedWithConvId = conversationIdRef.current
    let fullText = ''
    let citations: Citation[] | undefined
    let refused = false
    let refusedMessage: string | undefined
    let suggestions: string[] = []

    // delta 直接渲染（真流式透传，不再用打字机缓冲）
    const appendDelta = (chunk: string) => {
      fullText += chunk
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMsg.id ? { ...m, loading: false, text: fullText } : m,
        ),
      )
    }

    const handler = (event: ChatEvent) => {
      if (event.event === 'meta') {
        const cid = event.data.conversation_id
        const mid = event.data.message_id
        // 敏感词拦截等分支会回 null id，不能用 null 覆盖已有会话
        if (cid) {
          // 仅首轮（id 从空变非空）时更新并刷新侧边栏，避免每轮重复刷新
          if (!startedWithConvId && !conversationIdRef.current) {
            conversationIdRef.current = cid
            setConversationId(cid)
            refreshConversations()
          }
        }
        if (mid) {
          setMessages((prev) =>
            prev.map((m) => (m.id === assistantMsg.id ? { ...m, messageId: mid } : m)),
          )
        }
      } else if (event.event === 'citations') {
        citations = event.data.citations
      } else if (event.event === 'delta') {
        appendDelta(event.data.text)
      } else if (event.event === 'refused') {
        refused = true
        refusedMessage = event.data.message
      } else if (event.event === 'done') {
        suggestions = event.data.suggestions ?? []
      } else if (event.event === 'context_warning') {
        // 后端 context 压缩次数超阈值，提示用户新开对话
        if (event.data.need_new_conversation) {
          setContextWarning({
            compressionCount: event.data.compression_count,
            threshold: event.data.threshold,
          })
        }
      }
    }

    try {
      await mockAsk(question, selectedKbs, conversationIdRef.current, handler, controller.signal)
      // 正常结束（含 refused）：统一收尾
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
            : { ...m, loading: false, done: true, text: fullText || '(空)', citations, suggestions }
        }),
      )
    } catch (e) {
      if (controller.signal.aborted) {
        // 用户点击"停止"主动中止：保留已流出的内容并正常收尾
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMsg.id
              ? { ...m, loading: false, done: true, text: fullText || '(已停止)', citations }
              : m,
          ),
        )
        return
      }
      // 非主动中止：网络断开 / 后端连接中断 / HTTP 错误
      // 保留已流出的 fullText（用户已看到的部分不丢失），标记为 networkError
      // MessageBubble 会渲染断网提示 + 重试按钮（重试走 handleRetry → askQuestion）
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMsg.id
            ? { ...m, loading: false, done: true, text: fullText || '', networkError: true }
            : m,
        ),
      )
    } finally {
      // 主动停止在 catch 内 return，finally 仍会执行，统一回收流式态
      setStreaming(false)
      abortRef.current = null
    }
  }

  async function handleAsk() {
    const q = input.trim()
    if (!q) return
    // 发送成功后清除追问上下文
    clearFollowUpContext()
    await askQuestion(q)
  }

  function handleStop() {
    abortRef.current?.abort()
  }

  /** 点击追问建议气泡：填入输入框，聚焦后用户可编辑后发送。 */
  function handleSuggestionClick(text: string) {
    setInput(text)
    inputAreaRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    setTimeout(() => inputRef.current?.focus(), 300)
  }

  /** 通用追问：不自动填入原问题，仅灰色行展示 AI 回答摘要并聚焦输入框，由用户自行输入新问题。 */
  function handleFollowUp(msg: Message) {
    const summary = msg.text.replace(/\s+/g, ' ').trim().slice(0, 80)
    setFollowUpContext({ assistantText: summary })
    inputAreaRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    setTimeout(() => inputRef.current?.focus(), 300)
  }

  /** 关闭追问上下文展示行（用户直接输入新问题时）。 */
  function clearFollowUpContext() {
    setFollowUpContext(null)
  }

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 64px)', gap: 0 }}>
      {/* 左侧会话侧边栏 */}
      <div
        style={{
          width: 220,
          minWidth: 220,
          borderRight: '1px solid #f0f0f0',
          display: 'flex',
          flexDirection: 'column',
          background: '#fafafa',
        }}
      >
        <div style={{ padding: '12px 12px 8px' }}>
          <Button
            type="dashed"
            block
            icon={<PlusOutlined />}
            onClick={handleNewConversation}
          >
            新建对话
          </Button>
        </div>
        <div style={{ flex: 1, overflowY: 'auto', padding: '0 8px 8px' }}>
          {conversations.length === 0 ? (
            <div style={{ textAlign: 'center', color: 'rgba(0,0,0,0.35)', fontSize: 12, marginTop: 20 }}>
              暂无历史对话
            </div>
          ) : (
            conversations.map((conv) => (
              <div
                key={conv.id}
                onClick={() => handleSelectConversation(conv)}
                style={{
                  padding: '8px 10px',
                  borderRadius: 8,
                  cursor: 'pointer',
                  marginBottom: 2,
                  background: conv.id === conversationId ? '#e6f4ff' : 'transparent',
                  transition: 'background 0.15s',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 4,
                }}
                onMouseEnter={(e) => {
                  if (conv.id !== conversationId) e.currentTarget.style.background = '#f0f0f0'
                }}
                onMouseLeave={(e) => {
                  if (conv.id !== conversationId) e.currentTarget.style.background = 'transparent'
                }}
              >
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div
                    style={{
                      fontSize: 13,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      fontWeight: conv.id === conversationId ? 500 : 400,
                    }}
                  >
                    {conv.title || '新对话'}
                  </div>
                </div>
                <Popconfirm
                  title="删除此对话？"
                  okText="删除"
                  cancelText="取消"
                  onConfirm={(e) => {
                    e?.stopPropagation()
                    void handleDeleteConversation(conv, e as unknown as React.MouseEvent)
                  }}
                  onCancel={(e) => e?.stopPropagation()}
                >
                  <Button
                    type="text"
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={(e) => e.stopPropagation()}
                    style={{ flexShrink: 0 }}
                  />
                </Popconfirm>
              </div>
            ))
          )}
        </div>
      </div>

      {/* 中间对话区 */}
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        {/* 知识库选择 */}
        <Card size="small" style={{ margin: 12, flexShrink: 0 }} styles={{ body: { padding: 12 } }}>
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

        {/* 对话消息区 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '0 16px' }}>
          {loadError && <Alert type="error" showIcon message={loadError} style={{ marginBottom: 12 }} />}
          {contextWarning && (
            <Alert
              type="warning"
              showIcon
              closable
              onClose={() => setContextWarning(null)}
              message={`上下文已被压缩 ${contextWarning.compressionCount} 次，建议新开对话重置上下文以获得更好的回答效果。`}
              action={
                <a onClick={handleNewConversation} style={{ fontWeight: 500 }}>
                  新开对话
                </a>
              }
              style={{ marginBottom: 12 }}
            />
          )}
          {messages.map((m) => (
            <MessageBubble
              key={m.id}
              msg={m}
              onOpenCitation={(c) => setSidebarCitation(c)}
              onCopy={handleCopy}
              onRetry={handleRetry}
              onSuggestionClick={handleSuggestionClick}
              onFollowUp={handleFollowUp}
              onAdopt={handleAdopt}
            />
          ))}
          <div ref={messagesEndRef} />
        </div>

        {/* 输入区 */}
        <div ref={inputAreaRef} style={{ borderTop: '1px solid #f0f0f0', padding: 12, flexShrink: 0 }}>
          {/* 追问上下文灰色展示行 */}
          {followUpContext && (
            <div
              style={{
                background: '#fafafa',
                border: '1px solid #f0f0f0',
                borderRadius: 6,
                padding: '6px 10px',
                marginBottom: 8,
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                fontSize: 13,
              }}
            >
              <span style={{ color: '#bfbfbf', flexShrink: 0 }}>追问这条回答</span>
              <span
                style={{
                  color: '#8c8c8c',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  flex: 1,
                }}
                title={followUpContext.assistantText}
              >
                {followUpContext.assistantText}
              </span>
              <Button
                type="text"
                size="small"
                style={{ flexShrink: 0, color: '#bfbfbf' }}
                onClick={clearFollowUpContext}
              >
                x
              </Button>
            </div>
          )}
          <Space.Compact style={{ width: '100%' }}>
            <Input.TextArea
              ref={inputRef}
              value={input}
              onChange={(e) => {
                setInput(e.target.value)
              }}
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
      </div>

      {/* 右侧常驻引用面板（不再用 Drawer） */}
      <div
        style={{
          width: 320,
          minWidth: 320,
          borderLeft: '1px solid #f0f0f0',
          overflowY: 'auto',
          padding: 12,
        }}
      >
        <CitationPanel citation={sidebarCitation} />
      </div>
    </div>
  )
}

// 引用内容面板（右侧常驻栏）
function CitationPanel({ citation }: { citation: Citation | null }) {
  if (!citation) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="点击答案中的引用编号查看原文片段"
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
  onSuggestionClick,
  onFollowUp,
  onAdopt,
}: {
  msg: Message
  onOpenCitation: (c: Citation) => void
  onCopy: (m: Message) => void
  onRetry: (m: Message) => void
  onSuggestionClick: (text: string) => void
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

  // networkError 消息只显示断网提示行的重试按钮，隐藏正常操作栏（避免重复）
  const canOperate = !msg.loading && msg.done && !msg.refused && !msg.networkError && !!msg.messageId && !!msg.text

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

      {/* 网络断开提示：SSE 中途断连时已保留 fullText，提示用户重试 */}
      {msg.networkError && (
        <div
          style={{
            marginTop: 8,
            marginLeft: 4,
            padding: '6px 12px',
            background: '#fff7e6',
            border: '1px solid #ffd591',
            borderRadius: 6,
            fontSize: 13,
            color: '#d46b08',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <span>连接中断，已保留已流出的内容，请点击重试</span>
          <Button
            size="small"
            icon={<RedoOutlined />}
            onClick={() => onRetry(msg)}
            style={{ flexShrink: 0 }}
          >
            重试
          </Button>
        </div>
      )}

      {/* 追问建议气泡 */}
      {canOperate && msg.suggestions && msg.suggestions.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 8, marginLeft: 4 }}>
          {msg.suggestions.map((s, i) => (
            <Tag
              key={i}
              color="blue"
              style={{
                cursor: 'pointer',
                borderRadius: 16,
                padding: '2px 12px',
                fontSize: 13,
                background: '#f0f5ff',
                border: '1px solid #adc6ff',
                color: '#0958d9',
              }}
              onClick={() => onSuggestionClick(s)}
            >
              {s}
            </Tag>
          ))}
        </div>
      )}

      {/* 操作栏 */}
      {canOperate && (
        <Space size={0} style={{ marginLeft: 4, marginTop: 4 }}>
          <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => onCopy(msg)}>
            复制
          </Button>
          <Button type="text" size="small" icon={<RedoOutlined />} onClick={() => onRetry(msg)}>
            重试
          </Button>
          <Tooltip title="基于这条回答继续追问">
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
