import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { mockLogin, User } from '../mocks/data'

type Props = {
  onLogin: (user: User) => void
}

export function LoginPage({ onLogin }: Props) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('ChangeMe123!')
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setLoading(true)
    try {
      const user = await mockLogin(username, password)
      onLogin(user)
      navigate('/chat')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="grid min-h-screen place-items-center p-6">
      <section className="w-full max-w-md rounded-2xl bg-white p-8 shadow-lg">
        <p className="text-sm font-medium text-blue-600">知识库问答 Agent</p>
        <h1 className="mt-2 text-2xl font-bold">登录</h1>
        <p className="mt-3 text-sm text-slate-600">M1 假登录，输入任意用户名密码即可进入演示。</p>

        <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
          <div>
            <label className="mb-1 block text-sm font-medium text-slate-700">用户名</label>
            <input
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={loading}
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-slate-700">密码</label>
            <input
              type="password"
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={loading}
            />
          </div>
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-60"
          >
            {loading ? '登录中...' : '登录'}
          </button>
        </form>

        <div className="mt-6 rounded-lg bg-slate-50 p-3 text-xs text-slate-500">
          提示：尝试问"报销流程"、"员工手册"、"考勤制度" —— 会得到引用可溯源的答案。
          问"股票"、"天气" —— 会触发拒答。
        </div>
      </section>
    </main>
  )
}
