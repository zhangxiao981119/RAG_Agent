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

const TOKEN_KEY = 'kagent_token'
const REFRESH_TOKEN_KEY = 'kagent_refresh_token'
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
  localStorage.removeItem(REFRESH_TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

/** 读取 localStorage 中的 refresh token。 */
export function getStoredRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY)
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
 * 登录态失效统一处理：本地持有 token 却收到 401（过期/被拉黑），
 * 清除本地凭据并回到登录页；登录接口自身的 401 不跳转。
 */
function handleUnauthorized(): void {
  if (!getStoredToken()) return
  clearAuth()
  if (!window.location.pathname.startsWith('/login')) {
    window.location.assign('/login')
  }
}

async function http<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, withAuth(init))
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
  const resp = await fetch(`/api/documents/${docId}/raw`, withAuth())
  if (!resp.ok) {
    const text = await resp.text().catch(() => '')
    if (resp.status === 401) handleUnauthorized()
    throw new ApiError(resp.status, extractErrorMessage(resp.status, text))
  }
  return resp
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
  const response = await fetch('/api/chat/ask', withAuth({
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, kb_ids: kbIds, conversation_id: conversationId }),
    signal,
  }))
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
  localStorage.setItem(REFRESH_TOKEN_KEY, data.refresh_token)
  localStorage.setItem(USER_KEY, JSON.stringify(data.user))
  return data.user
}

/** 退出登录：调后端 logout 吊销 access + refresh，再清 localStorage。 */
export async function logout(): Promise<void> {
  const refreshToken = getStoredRefreshToken()
  try {
    await http<{ revoked: boolean }>('/api/auth/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    })
  } catch {
    // 后端登出失败也要清本地（最坏情况：token 自然过期前仍可用一段时间）
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
