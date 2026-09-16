import { useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'

import { fetchDocuments, fetchKbs, uploadDocument, KnowledgeBase, Document } from '../mocks/data'

const STATUS_LABEL: Record<Document['status'], { text: string; cls: string }> = {
  pending: { text: '待处理', cls: 'bg-slate-100 text-slate-600' },
  parsing: { text: '解析中', cls: 'bg-blue-100 text-blue-700' },
  indexed: { text: '已索引', cls: 'bg-green-100 text-green-700' },
  failed: { text: '处理失败', cls: 'bg-red-100 text-red-700' },
}

const LEVEL_LABEL: Record<number, string> = { 10: '公开', 20: '内部', 30: '机密', 40: '绝密' }

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function DocumentsPage() {
  const { kbId } = useParams<{ kbId?: string }>()
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [selectedKb, setSelectedKb] = useState<string>(kbId || '')
  const [documents, setDocuments] = useState<Document[]>([])
  const [error, setError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)

  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    fetchKbs().then((list) => {
      setKbs(list)
      if (list.length > 0) {
        setSelectedKb((prev) => prev || list[0].id)
      }
    }).catch((e) => setError(e.message))
  }, [])

  useEffect(() => {
    if (kbId) setSelectedKb(kbId)
  }, [kbId])

  useEffect(() => {
    if (!selectedKb) return
    fetchDocuments(selectedKb).then(setDocuments).catch((e) => setError(e.message))
  }, [selectedKb])

  // 轮询：有 pending/parsing 文档时每 3s 刷新（手册 C6 验收）
  useEffect(() => {
    const hasParsing = documents.some((d) => d.status === 'pending' || d.status === 'parsing')
    if (!hasParsing || !selectedKb) return
    const timer = setInterval(() => {
      fetchDocuments(selectedKb).then(setDocuments).catch(() => {})
    }, 3000)
    return () => clearInterval(timer)
  }, [documents, selectedKb])

  async function handleUpload(file: File) {
    if (!selectedKb) return
    setUploading(true)
    setError(null)
    try {
      await uploadDocument(selectedKb, file)
      const updated = await fetchDocuments(selectedKb)
      setDocuments(updated)
    } catch (e: any) {
      setError(e.message || '上传失败')
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  return (
    <section>
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">文档</h1>
          <p className="mt-1 text-sm text-slate-600">管理知识库中的文档与索引状态</p>
        </div>
        <div className="flex items-center gap-3">
          <select
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
            value={selectedKb}
            onChange={(e) => setSelectedKb(e.target.value)}
          >
            {kbs.map((kb) => (
              <option key={kb.id} value={kb.id}>{kb.name}</option>
            ))}
          </select>
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            accept=".pdf,.md,.txt,.xls,.xlsx,.docx"
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) handleUpload(f)
            }}
            disabled={uploading || !selectedKb}
          />
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading || !selectedKb}
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-60"
          >
            {uploading ? '上传中...' : '上传文档'}
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-lg bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>
      )}

      <div className="mb-6 rounded-xl border-2 border-dashed border-slate-300 bg-white p-8 text-center">
        <button
          onClick={() => fileInputRef.current?.click()}
          disabled={uploading || !selectedKb}
          className="text-sm text-blue-600 hover:underline disabled:opacity-60"
        >
          {uploading ? '正在上传...' : '点击或拖放文件到此处'}
        </button>
        <div className="mt-1 text-xs text-slate-400">支持 PDF / Markdown / TXT / XLS / XLSX / DOCX，单文件 ≤ 100 MB</div>
      </div>

      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
            <tr>
              <th className="px-4 py-3 text-left font-medium">文件名</th>
              <th className="px-4 py-3 text-left font-medium">大小</th>
              <th className="px-4 py-3 text-left font-medium">密级</th>
              <th className="px-4 py-3 text-left font-medium">版本</th>
              <th className="px-4 py-3 text-left font-medium">状态</th>
              <th className="px-4 py-3 text-left font-medium">上传时间</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {documents.map((d) => (
              <tr key={d.id} className="hover:bg-slate-50">
                <td className="px-4 py-3 font-medium text-slate-800">{d.filename}</td>
                <td className="px-4 py-3 text-slate-600">{formatSize(d.size_bytes)}</td>
                <td className="px-4 py-3 text-slate-600">{LEVEL_LABEL[d.level_rank] || '内部'}</td>
                <td className="px-4 py-3 text-slate-600">v{d.version}</td>
                <td className="px-4 py-3">
                  <span className={`inline-flex rounded px-2 py-0.5 text-xs ${STATUS_LABEL[d.status].cls}`}>
                    {STATUS_LABEL[d.status].text}
                  </span>
                </td>
                <td className="px-4 py-3 text-slate-500">{new Date(d.uploaded_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {documents.length === 0 && !error && (
        <div className="mt-4 rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center text-slate-500">
          该知识库暂无文档
        </div>
      )}
    </section>
  )
}
