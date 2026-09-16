// ============================================================
// M2 真实 API 层 —— 从 M1 mock 切换到真实后端
// 接口 schema 对齐手册 §5.2
// ============================================================

export type Citation = {
  n: number
  chunk_id: string
  doc_id: string
  filename: string
  heading_path: string
  page_no: number
  score: number
  snippet?: string
}

export type ChatEvent =
  | { event: 'meta'; data: { conversation_id: string; message_id: string; stage: string } }
  | { event: 'stage'; data: { stage: string; ms: number } }
  | { event: 'citations'; data: { citations: Citation[] } }
  | { event: 'delta'; data: { text: string } }
  | { event: 'done'; data: { finish_reason: string; grounding: { stripped_sentences: number }; usage: { prompt_tokens: number; completion_tokens: number } } }
  | { event: 'refused'; data: { reason: string; message: string } }

export type KnowledgeBase = {
  id: string
  name: string
  description: string
  is_public: boolean
  doc_count: number
}

export type Document = {
  id: string
  kb_id: string
  filename: string
  ext: string
  size_bytes: number
  status: 'pending' | 'parsing' | 'indexed' | 'failed'
  version: number
  level_rank: number
  uploaded_at: string
}

export type Job = {
  id: string
  document_id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'dead'
  attempts: number
  max_attempts: number
  last_error: string | null
  started_at: string | null
  finished_at: string | null
}

export type User = {
  id: string
  display_name: string
  dept_path: string
  clearance: number
}

// ---------- 工具函数 ----------

async function http<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    const text = await response.text().catch(() => '')
    throw new Error(`HTTP ${response.status}: ${text}`)
  }
  return response.json() as Promise<T>
}

// ---------- API 函数 ----------

export async function fetchKbs(): Promise<KnowledgeBase[]> {
  return http<KnowledgeBase[]>('/api/kbs')
}

export async function createKb(name: string, description: string, isPublic: boolean): Promise<KnowledgeBase> {
  return http<KnowledgeBase>('/api/kbs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description, is_public: isPublic }),
  })
}

export async function fetchDocuments(kbId: string): Promise<Document[]> {
  return http<Document[]>(`/api/kbs/${kbId}/documents`)
}

export async function fetchDocument(docId: string): Promise<Document | undefined> {
  try {
    return await http<Document>(`/api/documents/${docId}`)
  } catch {
    return undefined
  }
}

export async function deleteDocument(docId: string): Promise<void> {
  await fetch(`/api/documents/${docId}`, { method: 'DELETE' })
}

export async function uploadDocument(kbId: string, file: File): Promise<Document> {
  const form = new FormData()
  form.append('file', file)
  return http<Document>(`/api/kbs/${kbId}/documents`, {
    method: 'POST',
    body: form,
  })
}

export async function fetchJob(jobId: string): Promise<Job> {
  return http<Job>(`/api/jobs/${jobId}`)
}

// ---------- SSE 提问 ----------

export async function askChat(
  question: string,
  kbIds: string[],
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch('/api/chat/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, kb_ids: kbIds, conversation_id: null }),
    signal,
  })
  if (!response.ok || !response.body) {
    const text = await response.text().catch(() => '')
    throw new Error(`HTTP ${response.status}: ${text}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // SSE 事件块以双换行分隔
    const blocks = buffer.split('\n\n')
    buffer = blocks.pop() || ''
    for (const block of blocks) {
      const lines = block.split('\n')
      let event = ''
      let data = ''
      for (const line of lines) {
        if (line.startsWith('event: ')) event = line.slice(7).trim()
        else if (line.startsWith('data: ')) data = line.slice(6)
      }
      if (event && data) {
        try {
          onEvent({ event, data: JSON.parse(data) } as ChatEvent)
        } catch (e) {
          console.error('SSE 解析失败', e, data)
        }
      }
    }
  }
}

// ---------- 登录（M2 占位：无真实认证，返回固定 admin） ----------
// M3 接 JWT 后替换为 POST /api/auth/login

export async function mockLogin(username: string, _password: string): Promise<User> {
  await new Promise((r) => setTimeout(r, 200))
  // M2 固定返回 admin（后端也是固定 admin）
  if (username === 'admin') {
    return { id: 'u_admin', display_name: '系统管理员', dept_path: '/总部/', clearance: 40 }
  }
  // 其他用户名也允许（M2 无权限校验）
  return { id: 'u_admin', display_name: username, dept_path: '/总部/技术中心/', clearance: 20 }
}

// ---------- 向后兼容（ChatPage 仍引用 mockAsk） ----------

export const mockAsk = askChat
