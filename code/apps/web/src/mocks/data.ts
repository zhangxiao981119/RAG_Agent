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
  | { event: 'meta'; data: { conversation_id: string | null; message_id: string | null; stage: string } }
  | { event: 'stage'; data: { stage: string; ms: number } }
  | { event: 'citations'; data: { citations: Citation[] } }
  | { event: 'delta'; data: { text: string } }
  | { event: 'done'; data: { finish_reason: string; grounding: { stripped_sentences: number }; usage: { prompt_tokens: number; completion_tokens: number }; suggestions?: string[] } }
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

// 全部用 sessionStorage：access/refresh token + user 信息
// 关 tab 即失效，比 localStorage 长期持有更防 XSS 重放
const TOKEN_KEY = 'kagent_token'
const REFRESH_TOKEN_KEY = 'kagent_refresh_token'
const USER_KEY = 'kagent_user'

/** 读取 sessionStorage 中的 JWT token。 */
export function getStoredToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY)
}

/** 读取 sessionStorage 中的已登录用户信息。 */
export function getStoredUser(): User | null {
  const raw = sessionStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as User
  } catch {
    return null
  }
}

/** 清除本地登录态。 */
export function clearAuth(): void {
  sessionStorage.removeItem(TOKEN_KEY)
  sessionStorage.removeItem(REFRESH_TOKEN_KEY)
  sessionStorage.removeItem(USER_KEY)
}

/** 读取 sessionStorage 中的 refresh token。 */
export function getStoredRefreshToken(): string | null {
  return sessionStorage.getItem(REFRESH_TOKEN_KEY)
}

/** 给请求头附加 Authorization: Bearer <token>。 */
function withAuth(init?: RequestInit): RequestInit {
  const token = getStoredToken()
  if (!token) return init ?? {}
  const headers = new Headers(init?.headers)
  headers.set('Authorization', `Bearer ${token}`)
  return { ...init, headers }
}

/** 统一 API 错误：携带 HTTP 状态码与后端中文消息。 */
export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * 解析 FastAPI 错误响应体：
 *  · HTTPException → { "detail": "中文消息" }
 *  · 422 校验失败 → { "detail": [{ "msg": "..." }, ...] }
 */
function extractErrorMessage(status: number, text: string): string {
  if (text) {
    try {
      const body = JSON.parse(text) as { detail?: unknown }
      const detail = body.detail
      if (typeof detail === 'string' && detail.trim()) return detail
      if (Array.isArray(detail) && detail.length > 0) {
        return detail
          .map((item) => {
            const msg = (item as { msg?: string })?.msg
            return msg ?? ''
          })
          .filter(Boolean)
          .join('；')
      }
    } catch {
      // 非 JSON 响应体，按纯文本处理
    }
    return text.slice(0, 300)
  }
  return `请求失败（HTTP ${status}）`
}

/**
 * 登录态失效统一处理：本地持有 token 却收到 401（refresh 也失败/被拉黑），
 * 清除本地凭据并回到登录页；登录接口自身的 401 不跳转。
 */
function handleUnauthorized(): void {
  if (!getStoredToken()) return
  clearAuth()
  if (!window.location.pathname.startsWith('/login')) {
    window.location.assign('/login')
  }
}

// ── 401 自动 refresh + inflight 复用 ─────────────────────
// 多个并发请求同时 401 时，只发起一次 refresh，其余复用同一 Promise
let refreshPromise: Promise<string | null> | null = null

/**
 * 用 refresh_token 换新 access + 新 refresh（rotation）。
 * 成功返回新 access token 并更新存储；失败返回 null（调用方应走 handleUnauthorized）。
 * refresh 自身 401 不重试（避免循环）。
 */
async function refreshAccessToken(): Promise<string | null> {
  if (refreshPromise) return refreshPromise
  const refreshToken = getStoredRefreshToken()
  if (!refreshToken) return null
  refreshPromise = (async () => {
    try {
      const r = await fetch('/api/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      })
      if (!r.ok) return null
      const data = (await r.json()) as { token: string; refresh_token: string }
      sessionStorage.setItem(TOKEN_KEY, data.token)
      // refresh token rotation：后端发了新 refresh，旧 refresh 已入黑名单
      sessionStorage.setItem(REFRESH_TOKEN_KEY, data.refresh_token)
      return data.token
    } catch {
      return null
    } finally {
      refreshPromise = null
    }
  })()
  return refreshPromise
}

async function http<T>(url: string, init?: RequestInit): Promise<T> {
  let response = await fetch(url, withAuth(init))
  // 401 先尝试 refresh + 重试一次；refresh 失败或重试仍 401 才登出
  if (response.status === 401) {
    const newToken = await refreshAccessToken()
    if (newToken) {
      const headers = new Headers(init?.headers)
      headers.set('Authorization', `Bearer ${newToken}`)
      response = await fetch(url, { ...init, headers })
    }
  }
  if (!response.ok) {
    const text = await response.text().catch(() => '')
    if (response.status === 401) handleUnauthorized()
    throw new ApiError(response.status, extractErrorMessage(response.status, text))
  }
  if (response.status === 204) return undefined as T
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

export async function deleteKb(kbId: string): Promise<void> {
  await http<void>(`/api/kbs/${kbId}`, { method: 'DELETE' })
}

// ---------- 知识库成员管理 (M4 任务 8) ----------

export type KbMemberItem = {
  subject_type: 'user' | 'group' | 'dept' | 'role'
  subject_id: string
  label: string
}

export async function fetchKbMembers(kbId: string): Promise<KbMemberItem[]> {
  const r = await http<{ kb_id: string; members: KbMemberItem[] }>(`/api/kbs/${kbId}/members`)
  return r.members
}

export async function setKbMembers(
  kbId: string,
  members: KbMemberItem[],
): Promise<{ changed: boolean; members: KbMemberItem[] }> {
  const payload = {
    members: members.map((m) => ({ subject_type: m.subject_type, subject_id: m.subject_id })),
  }
  return http<{ changed: boolean; members: KbMemberItem[] }>(`/api/kbs/${kbId}/members`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
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

/** 拉取文档原文（预览用）。调用方按 ext 决定读文本还是 blob。 */
export async function fetchDocumentRaw(docId: string): Promise<Response> {
  let resp = await fetch(`/api/documents/${docId}/raw`, withAuth())
  // 401 先 refresh + 重试一次
  if (resp.status === 401) {
    const newToken = await refreshAccessToken()
    if (newToken) {
      resp = await fetch(`/api/documents/${docId}/raw`, {
        headers: { Authorization: `Bearer ${newToken}` },
      })
    }
  }
  if (!resp.ok) {
    const text = await resp.text().catch(() => '')
    if (resp.status === 401) handleUnauthorized()
    throw new ApiError(resp.status, extractErrorMessage(resp.status, text))
  }
  return resp
}

export async function deleteDocument(docId: string): Promise<void> {
  await http<void>(`/api/documents/${docId}`, { method: 'DELETE' })
}

/** 带上传进度的文件上传。fetch 标准不支持 upload progress，必须用 XMLHttpRequest。
 *  401 自动 refresh + 重试（复用 http() 的 refreshAccessToken 逻辑）。
 */
export function uploadDocument(
  kbId: string,
  file: File,
  onProgress?: (percent: number) => void,
): Promise<Document> {
  return new Promise((resolve, reject) => {
    const url = `/api/kbs/${kbId}/documents`
    const form = new FormData()
    form.append('file', file)

    const doSend = (token: string | null) => {
      const xhr = new XMLHttpRequest()
      xhr.open('POST', url, true)
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) {
          onProgress(Math.round((e.loaded / e.total) * 100))
        }
      }

      xhr.onload = () => {
        if (xhr.status === 200 || xhr.status === 201) {
          try {
            resolve(JSON.parse(xhr.responseText) as Document)
          } catch {
            reject(new Error('上传响应解析失败'))
          }
          return
        }
        // 401：尝试 refresh + 重试一次
        if (xhr.status === 401) {
          void refreshAccessToken().then((newToken) => {
            if (newToken) {
              doSend(newToken)
            } else {
              handleUnauthorized()
              reject(new ApiError(401, '登录已过期'))
            }
          })
          return
        }
        reject(new ApiError(xhr.status, extractErrorMessage(xhr.status, xhr.responseText)))
      }

      xhr.onerror = () => reject(new ApiError(0, '网络错误，请检查连接'))
      xhr.send(form)
    }

    doSend(getStoredToken())
  })
}

export async function fetchJob(jobId: string): Promise<Job> {
  return http<Job>(`/api/jobs/${jobId}`)
}

// ---------- 会话管理（多轮对话历史） ----------

export type Conversation = {
  id: string
  title: string
  created_at: string
  last_at: string | null
}

export type ConversationMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  meta: { suggestions?: string[]; refused?: boolean }
  created_at: string
}

export async function fetchConversations(): Promise<Conversation[]> {
  return http<Conversation[]>('/api/conversations')
}

export async function fetchConversationMessages(convId: string): Promise<ConversationMessage[]> {
  return http<ConversationMessage[]>(`/api/conversations/${convId}/messages`)
}

export async function deleteConversation(convId: string): Promise<void> {
  return http<void>(`/api/conversations/${convId}`, { method: 'DELETE' })
}

// ---------- SSE 提问 ----------

/** 采纳回答为微调样本。重复采纳返回 already_adopted=true。 */
export async function adoptAnswer(messageId: string): Promise<{ adopted: boolean; already_adopted: boolean; sample_id: string }> {
  return http('/api/messages/' + messageId + '/adopt', { method: 'POST' })
}

export async function askChat(
  question: string,
  kbIds: string[],
  conversationId: string | null,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const body = JSON.stringify({ question, kb_ids: kbIds, conversation_id: conversationId })
  let response = await fetch('/api/chat/ask', withAuth({
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
    signal,
  }))
  // 401 先 refresh + 重试一次（refresh 失败则走 handleUnauthorized）
  if (response.status === 401) {
    const newToken = await refreshAccessToken()
    if (newToken) {
      response = await fetch('/api/chat/ask', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${newToken}`,
        },
        body,
        signal,
      })
    }
  }
  if (!response.ok || !response.body) {
    const text = await response.text().catch(() => '')
    if (response.status === 401) handleUnauthorized()
    throw new ApiError(response.status, extractErrorMessage(response.status, text))
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
  refresh_token: string
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
function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i])
  return btoa(binary)
}

/**
 * 用后端 RSA 公钥（SPKI DER base64）以 RSA-OAEP(SHA-256) 加密明文密码，返回 base64 密文。
 * 注意：crypto.subtle 仅在安全上下文（https / localhost）可用，局域网 IP（http://192.168.x.x）
 * 访问时 crypto 为 undefined，登录会报 "Cannot read properties of undefined (reading 'importKey')"。
 */
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
  sessionStorage.setItem(TOKEN_KEY, data.token)
  sessionStorage.setItem(REFRESH_TOKEN_KEY, data.refresh_token)
  sessionStorage.setItem(USER_KEY, JSON.stringify(data.user))
  return data.user
}

/** 退出登录：调后端 logout 吊销 access + refresh，再清本地凭据。 */
export async function logout(): Promise<void> {
  const refreshToken = getStoredRefreshToken()
  try {
    await http<{ revoked: boolean }>('/api/auth/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    })
  } catch (e) {
    // 后端 logout 调用失败时仍清本地（用户主观意图是退出）
    // 但远程 token 可能未吊销直至过期，控制台告警便于运维侧排查
    console.warn('后端 logout 调用失败，本地凭据已清除，远程 token 可能未吊销', e)
  }
  clearAuth()
}

// ---------- 向后兼容（ChatPage 仍引用 mockAsk） ----------

export const mockAsk = askChat

// ---------- M3 任务 7+8 部门管理 API ----------

export type DepartmentNode = {
  id: string
  name: string
  path: string
  depth: number
  sort_order: number
  visible_to_parent: boolean
  user_count: number
  children: DepartmentNode[]
}

export type DepartmentResponse = {
  id: string
  name: string
  path: string
  depth: number
  sort_order: number
  visible_to_parent: boolean
}

/** 拉取完整部门树。 */
export async function fetchDepartments(): Promise<DepartmentNode[]> {
  return http<DepartmentNode[]>('/api/departments')
}

/** 新建子部门。parent_id=null 表示根节点。 */
export async function createDepartment(payload: {
  name: string
  parent_id: string | null
  sort_order?: number
  visible_to_parent?: boolean
}): Promise<DepartmentResponse> {
  return http<DepartmentResponse>('/api/departments', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 修改部门属性（重命名 / sort_order / visible_to_parent）。 */
export async function updateDepartment(
  id: string,
  payload: {
    name?: string
    sort_order?: number
    visible_to_parent?: boolean
  },
): Promise<DepartmentResponse> {
  return http<DepartmentResponse>(`/api/departments/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 移动部门到新父下。new_parent_id=null 表示移到根。 */
export async function moveDepartment(
  id: string,
  newParentId: string | null,
): Promise<DepartmentResponse> {
  return http<DepartmentResponse>(`/api/departments/${id}/move`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ new_parent_id: newParentId }),
  })
}

/** 删除部门（必须无子部门 + 无用户）。 */
export async function deleteDepartment(id: string): Promise<void> {
  await http<{ deleted: boolean }>(`/api/departments/${id}`, { method: 'DELETE' })
}

// ---------- M3 任务 4 用户管理 API ----------

export type AdminUser = {
  id: string
  username: string
  display_name: string
  email: string | null
  dept_id: string | null
  dept_path: string | null
  clearance: number
  role_names: string[]
  status: 'active' | 'disabled'
}

/** 用户分页响应（AntD Table 服务端分页）。 */
export type UserPage = {
  items: AdminUser[]
  total: number
  page: number
  page_size: number
}

// ---------- 审计日志（M5 任务 3 提前实现） ----------

export type AuditLogItem = {
  id: number
  user_id: string | null
  user_label: string
  action: string
  object_type: string | null
  object_id: string | null
  detail: Record<string, unknown>
  ip: string | null
  created_at: string
}

export type AuditLogPage = {
  items: AuditLogItem[]
  total: number
  page: number
  page_size: number
}

/** 分页拉取审计日志。action 传前缀（如 doc）可按动作类过滤；startDate/endDate 为 YYYY-MM-DD 日期范围（含当天）。 */
export async function fetchAuditLogs(
  page = 1,
  pageSize = 20,
  action?: string,
  startDate?: string,
  endDate?: string,
): Promise<AuditLogPage> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
  if (action) params.set('action', action)
  if (startDate) params.set('start_date', startDate)
  if (endDate) params.set('end_date', endDate)
  return http<AuditLogPage>(`/api/admin/audit-logs?${params.toString()}`)
}

// ---------- 系统监控 (M6 可观测) ----------

export type MetricSummary = {
  window: string
  total_requests: number
  refused_count: number
  refused_rate: number
  total_p50: number
  total_p95: number
  total_p99: number
  total_avg: number
  retrieve_p50: number
  retrieve_p95: number
  retrieve_p99: number
  retrieve_avg: number
}

export async function fetchMetrics(hours = 24): Promise<MetricSummary> {
  return http<MetricSummary>(`/api/admin/metrics?hours=${hours}`)
}

// ---------- 评估门禁 (M6 G5) ----------

export type EvalCaseResult = {
  question: string
  expected_answerable: boolean
  actual_refused: boolean
  passed: boolean
  chunks_count: number
  refuse_reason: string | null
  elapsed_ms: number
}

export type EvalReport = {
  total: number
  passed: number
  failed: number
  answer_accuracy: number
  refuse_accuracy: number
  total_seconds: number
  cases: EvalCaseResult[]
}

export async function runEval(): Promise<EvalReport> {
  return http<EvalReport>('/api/admin/eval/run', { method: 'POST' })
}

/** 分页拉取用户（含 dept_path）。page 从 1 开始。 */
export async function fetchUsers(page = 1, pageSize = 20): Promise<UserPage> {
  return http<UserPage>(`/api/users?page=${page}&page_size=${pageSize}`)
}

/** 新建用户。 */
export async function createUser(payload: {
  username: string
  display_name: string
  email?: string | null
  password: string
  dept_id?: string | null
  clearance?: number
  role_names?: string[]
}): Promise<AdminUser> {
  return http<AdminUser>('/api/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 修改用户属性（不改密码）。dept_id 传 null 表示清空。 */
export async function updateUser(
  id: string,
  payload: {
    display_name?: string
    email?: string | null
    dept_id?: string | null
    clearance?: number
    role_names?: string[]
    status?: 'active' | 'disabled'
  },
): Promise<AdminUser> {
  return http<AdminUser>(`/api/users/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 重置密码。 */
export async function resetUserPassword(id: string, newPassword: string): Promise<AdminUser> {
  return http<AdminUser>(`/api/users/${id}/reset-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ new_password: newPassword }),
  })
}

/** 删除用户。 */
export async function deleteUser(id: string): Promise<void> {
  await http<{ deleted: boolean }>(`/api/users/${id}`, { method: 'DELETE' })
}

// ---------- M3 任务 5 用户组管理 API ----------

export type AdminGroup = {
  id: string
  name: string
  kind: 'normal' | 'external'
  member_count: number
}

export type GroupMember = {
  user_id: string
  username: string
  display_name: string
}

/** 列出所有组（含 member_count）。 */
export async function fetchGroups(): Promise<AdminGroup[]> {
  return http<AdminGroup[]>('/api/groups')
}

/** 新建组。 */
export async function createGroup(payload: {
  name: string
  kind?: 'normal' | 'external'
}): Promise<{ id: string; name: string; kind: string }> {
  return http(`/api/groups`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 修改组属性。 */
export async function updateGroup(
  id: string,
  payload: { name?: string; kind?: 'normal' | 'external' },
): Promise<{ id: string; name: string; kind: string }> {
  return http(`/api/groups/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 删除组。 */
export async function deleteGroup(id: string): Promise<void> {
  await http<{ deleted: boolean }>(`/api/groups/${id}`, { method: 'DELETE' })
}

/** 列出组成员。 */
export async function fetchGroupMembers(groupId: string): Promise<GroupMember[]> {
  const r = await http<{ group_id: string; members: GroupMember[] }>(`/api/groups/${groupId}/members`)
  return r.members
}

/** 批量加成员。返回实际新增数。 */
export async function addGroupMembers(groupId: string, userIds: string[]): Promise<number> {
  const r = await http<{ added: number }>(`/api/groups/${groupId}/members`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_ids: userIds }),
  })
  return r.added
}

/** 移除单个成员。 */
export async function removeGroupMember(groupId: string, userId: string): Promise<void> {
  await http<{ removed: number }>(`/api/groups/${groupId}/members/${userId}`, { method: 'DELETE' })
}

// ---------- M3 任务 6 角色管理 API ----------

export type AdminRole = {
  id: string
  name: string
  user_count: number
}

/** 列出所有角色（含 user_count）。 */
export async function fetchRoles(): Promise<AdminRole[]> {
  return http<AdminRole[]>('/api/roles')
}

/** 新建角色。 */
export async function createRole(name: string): Promise<{ id: string; name: string }> {
  return http(`/api/roles`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
}

/** 改角色名（同步更新所有 user.role_names 中的旧名）。 */
export async function updateRole(
  id: string,
  name: string,
): Promise<{ id: string; name: string }> {
  return http(`/api/roles/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
}

/** 删除角色（从所有 user.role_names 移除）。返回受影响用户数。 */
export async function deleteRole(id: string): Promise<{ deleted: boolean; affected_users: number }> {
  return http(`/api/roles/${id}`, { method: 'DELETE' })
}

// ---------- M6 续篇：配额管理 API ----------

export type TenantQuota = {
  tenant_id: string
  daily_token_limit: number
  daily_message_limit: number
  monthly_token_limit: number
  updated_at: string
}

export type QuotaUsage = {
  user_id: string
  user_tokens_today: number
  user_messages_today: number
  tenant_tokens_today: number
  tenant_tokens_this_month: number
  user_daily_token_limit: number
  user_daily_message_limit: number
  tenant_daily_token_limit: number
  tenant_monthly_token_limit: number
}

/** 查当前租户配额。 */
export async function fetchQuota(): Promise<TenantQuota> {
  return http<TenantQuota>('/api/admin/quota')
}

/** 修改租户配额。 */
export async function updateQuota(payload: {
  daily_token_limit: number
  daily_message_limit: number
  monthly_token_limit: number
}): Promise<TenantQuota> {
  return http<TenantQuota>('/api/admin/quota', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 查当前用户今日用量（普通用户可查自己的）。 */
export async function fetchQuotaUsage(): Promise<QuotaUsage> {
  return http<QuotaUsage>('/api/admin/quota/usage')
}

// ---------- M6 续篇：敏感词管理 API ----------

export type SensitiveWord = {
  id: string
  word: string
  category: string | null
  created_by: string | null
  created_at: string
}

export type SensitiveBatchResult = {
  added: number
  duplicates_skipped: number
}

/** 列出敏感词（分页 + 模糊搜）。 */
export async function fetchSensitiveWords(
  page = 1,
  pageSize = 50,
  keyword?: string,
  category?: string,
): Promise<SensitiveWord[]> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
  if (keyword) params.set('keyword', keyword)
  if (category) params.set('category', category)
  return http<SensitiveWord[]>(`/api/admin/sensitive-words?${params.toString()}`)
}

/** 新增单个敏感词。 */
export async function createSensitiveWord(
  word: string,
  category?: string | null,
): Promise<SensitiveWord> {
  return http<SensitiveWord>('/api/admin/sensitive-words', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ word, category: category ?? null }),
  })
}

/** 批量新增敏感词（支持传入多行或逗号分隔的整段文本）。 */
export async function batchCreateSensitiveWords(
  words: string[],
  category?: string | null,
): Promise<SensitiveBatchResult> {
  return http<SensitiveBatchResult>('/api/admin/sensitive-words/batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ words, category: category ?? null }),
  })
}

/** 删除单条敏感词。 */
export async function deleteSensitiveWord(id: string): Promise<void> {
  await http(`/api/admin/sensitive-words/${id}`, { method: 'DELETE' })
}

// ---------- M6 续篇：灰度开关管理 API ----------

export type FeatureFlag = {
  id: string
  tenant_id: string
  feature_key: string
  dept_path_pattern: string
  enabled: boolean
  rollout_percent: number
  created_at: string
  updated_at: string
}

/** 列出特性开关（可按 feature_key 过滤）。 */
export async function fetchFeatureFlags(featureKey?: string): Promise<FeatureFlag[]> {
  const params = featureKey ? `?feature_key=${encodeURIComponent(featureKey)}` : ''
  return http<FeatureFlag[]>(`/api/admin/feature-flags${params}`)
}

/** 新增特性开关规则。 */
export async function createFeatureFlag(payload: {
  feature_key: string
  dept_path_pattern: string
  enabled?: boolean
  rollout_percent?: number
}): Promise<FeatureFlag> {
  return http<FeatureFlag>('/api/admin/feature-flags', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 修改特性开关（仅传变更字段）。 */
export async function updateFeatureFlag(
  id: string,
  payload: {
    enabled?: boolean
    rollout_percent?: number
    dept_path_pattern?: string
  },
): Promise<FeatureFlag> {
  return http<FeatureFlag>(`/api/admin/feature-flags/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

/** 删除特性开关。 */
export async function deleteFeatureFlag(id: string): Promise<void> {
  await http(`/api/admin/feature-flags/${id}`, { method: 'DELETE' })
}
