import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { fetchKbs, KnowledgeBase } from '../mocks/data'

export function KnowledgeBasesPage() {
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchKbs()
      .then(setKbs)
      .catch((e) => setError(e.message))
  }, [])

  return (
    <section>
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">知识库</h1>
          <p className="mt-1 text-sm text-slate-600">管理可访问的知识库</p>
        </div>
        <button className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700">
          新建知识库
        </button>
      </div>

      {error && (
        <div className="mb-4 rounded-lg bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {kbs.map((kb) => (
          <Link
            key={kb.id}
            to={`/knowledge-bases/${kb.id}/documents`}
            className="group rounded-xl border border-slate-200 bg-white p-5 transition hover:border-blue-400 hover:shadow-md"
          >
            <div className="flex items-start justify-between">
              <h3 className="font-semibold text-slate-900 group-hover:text-blue-700">{kb.name}</h3>
              {kb.is_public && (
                <span className="rounded bg-blue-100 px-2 py-0.5 text-xs text-blue-700">公开</span>
              )}
            </div>
            <p className="mt-2 text-sm text-slate-500">{kb.description}</p>
            <div className="mt-4 flex items-center gap-4 text-xs text-slate-500">
              <span>{kb.doc_count} 份文档</span>
              <span className="text-blue-600 group-hover:underline">查看文档 →</span>
            </div>
          </Link>
        ))}
      </div>

      {kbs.length === 0 && !error && (
        <div className="rounded-xl border border-dashed border-slate-300 bg-white p-12 text-center text-slate-500">
          暂无知识库
        </div>
      )}
    </section>
  )
}
