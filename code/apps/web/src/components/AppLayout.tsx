import { NavLink, Outlet } from 'react-router-dom'

import { User } from '../mocks/data'

const navigation = [
  { to: '/chat', label: '知识问答' },
  { to: '/knowledge-bases', label: '知识库' },
  { to: '/documents', label: '文档' },
  { to: '/admin', label: '系统管理' },
]

type Props = {
  currentUser: User | null
  onLogout: () => void
}

export function AppLayout({ currentUser, onLogout }: Props) {
  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[240px_1fr]">
      <aside className="border-b border-slate-800 bg-slate-950 px-5 py-4 text-white lg:min-h-screen lg:border-b-0 lg:border-r">
        <div className="mb-6 flex items-center gap-2 text-lg font-semibold lg:mb-10">
          <span className="inline-flex h-7 w-7 items-center justify-center rounded-md bg-blue-600 text-sm">Q</span>
          <span>知识库问答</span>
        </div>
        <nav className="flex gap-2 overflow-x-auto lg:flex-col">
          {navigation.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `whitespace-nowrap rounded-lg px-4 py-2 text-sm transition ${
                  isActive ? 'bg-blue-600 text-white' : 'text-slate-300 hover:bg-slate-800'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        {currentUser && (
          <div className="mt-auto pt-8">
            <div className="border-t border-slate-800 pt-4">
              <div className="text-sm font-medium text-white">{currentUser.display_name}</div>
              <div className="text-xs text-slate-400">{currentUser.dept_path}</div>
              <button
                onClick={onLogout}
                className="mt-2 text-xs text-slate-400 hover:text-white"
              >
                退出登录
              </button>
            </div>
          </div>
        )}
      </aside>
      <main className="min-w-0 p-5 sm:p-8 lg:p-10">
        <Outlet />
      </main>
    </div>
  )
}
