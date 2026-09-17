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

const TOKEN_KEY = 'kagent_token'
const USER_KEY = 'kagent_user'

/** 读取 localStorage 中的 JWT token。 */
export function getStoredToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

/** 读取 localStorage 中的已登录用户信息。 */
export function getStoredUser(): User | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as User
  } catch {
    return null
  }
}

/** 清除本地登录态。 */
export function clearAuth(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

/** 给请求头附加 Authorization: Bearer <token>。 */
function withAuth(init?: RequestInit): RequestInit {
  const token = getStoredToken()
  if (!token) return init ?? {}
  const headers = new Headers(init?.headers)
  headers.set('Authorization', `Bearer ${token}`)
  return { ...init, headers }
}

async function http<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, withAuth(init))
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
  const resp = await fetch(`/api/documents/${docId}`, withAuth({ method: 'DELETE' }))
  if (!resp.ok) {
    const text = await resp.text().catch(() => '')
    throw new Error(`HTTP ${resp.status}: ${text}`)
  }
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
  const response = await fetch('/api/chat/ask', withAuth({
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, kb_ids: kbIds, conversation_id: null }),
    signal,
  }))
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

// ---------- 登录（M3 真实认证：RSA 加密密码 → POST /api/auth/login） ----------

type LoginResponse = {
  token: string
  token_type: string
  user: User
}

type PublicKeyResponse = {
  public_key: string
}

/** base64 字符串转 ArrayBuffer（Web Crypto importKey 需要）。 */
function base64ToArrayBuffer(b64: string): ArrayBuffer {
  const binary = atob(b64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}

/** ArrayBuffer 转 base64 字符串。 */
function arrayBufferToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf)
  let binary = ''
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i])
  return btoa(binary)
}

/** 用后端 RSA 公钥（SPKI DER）以 RSA-OAEP(SHA-256) 加密明文密码，返回 base64 密文。 */
async function encryptPassword(publicKeyB64: string, password: string): Promise<string> {
  const der = base64ToArrayBuffer(publicKeyB64)
  const publicKey = await crypto.subtle.importKey(
    'spki',
    der,
    { name: 'RSA-OAEP', hash: 'SHA-256' },
    false,
    ['encrypt'],
  )
  const encrypted = await crypto.subtle.encrypt(
    { name: 'RSA-OAEP' },
    publicKey,
    new TextEncoder().encode(password),
  )
  return arrayBufferToBase64(encrypted)
}

export async function login(username: string, password: string): Promise<User> {
  // 1. 获取 RSA 公钥
  const { public_key } = await http<PublicKeyResponse>('/api/auth/public-key')
  // 2. 用公钥加密密码（网络上只传输密文）
  const encryptedPassword = await encryptPassword(public_key, password)
  // 3. 提交密文登录
  const data = await http<LoginResponse>('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password: encryptedPassword }),
  })
  localStorage.setItem(TOKEN_KEY, data.token)
  localStorage.setItem(USER_KEY, JSON.stringify(data.user))
  return data.user
}

/** 退出登录：清 localStorage。 */
export function logout(): void {
  clearAuth()
}

// ---------- 向后兼容（ChatPage 仍引用 mockAsk） ----------

export const mockAsk = askChat
