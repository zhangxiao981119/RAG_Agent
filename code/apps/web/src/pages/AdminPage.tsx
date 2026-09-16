const MOCK_DEPARTMENTS = [
  { path: '/总部/', userCount: 5, sensitive: false },
  { path: '/总部/技术中心/', userCount: 2, sensitive: false },
  { path: '/总部/技术中心/后端组/', userCount: 1, sensitive: false },
  { path: '/总部/技术中心/前端组/', userCount: 1, sensitive: false },
  { path: '/总部/财务部/', userCount: 1, sensitive: true },
  { path: '/总部/人力资源部/', userCount: 1, sensitive: true },
  { path: '/总部/人力资源部/薪酬组/', userCount: 0, sensitive: true },
  { path: '/总部/市场部/', userCount: 1, sensitive: false },
]

const MOCK_USERS = [
  { username: 'admin', display_name: '系统管理员', dept: '/总部/', clearance: 40, role: 'admin', status: 'active' },
  { username: 'user01', display_name: '张三', dept: '/总部/技术中心/后端组/', clearance: 20, role: 'user', status: 'active' },
  { username: 'user02', display_name: '李四', dept: '/总部/技术中心/前端组/', clearance: 20, role: 'user', status: 'active' },
  { username: 'user05', display_name: '王五', dept: '/总部/财务部/', clearance: 30, role: 'user', status: 'active' },
  { username: 'external', display_name: '外部人员', dept: '外部', clearance: 10, role: 'external', status: 'active' },
]

export function AdminPage() {
  return (
    <section className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">系统管理</h1>
        <p className="mt-1 text-sm text-slate-600">管理组织、用户、权限和系统配置</p>
      </div>

      {/* 部门树 */}
      <div className="rounded-xl border border-slate-200 bg-white">
        <div className="border-b border-slate-200 px-5 py-3 font-semibold text-slate-800">组织架构</div>
        <ul className="p-4 text-sm">
          {MOCK_DEPARTMENTS.map((d) => (
            <li key={d.path} className="flex items-center gap-2 py-1">
              <span className="text-slate-400">{'  '}</span>
              <span className="text-slate-700">{d.path}</span>
              {d.sensitive && <span className="rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-700">敏感</span>}
              <span className="text-slate-400">({d.userCount} 人)</span>
            </li>
          ))}
        </ul>
      </div>

      {/* 用户列表 */}
      <div className="rounded-xl border border-slate-200 bg-white">
        <div className="border-b border-slate-200 px-5 py-3 font-semibold text-slate-800">用户</div>
        <table className="w-full text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-slate-600">
            <tr>
              <th className="px-5 py-2.5 text-left font-medium">用户名</th>
              <th className="px-5 py-2.5 text-left font-medium">姓名</th>
              <th className="px-5 py-2.5 text-left font-medium">部门</th>
              <th className="px-5 py-2.5 text-left font-medium">密级</th>
              <th className="px-5 py-2.5 text-left font-medium">角色</th>
            </tr>
          </thead>
          <tbody>
            {MOCK_USERS.map((u) => (
              <tr key={u.username} className="border-b border-slate-100">
                <td className="px-5 py-2.5 font-medium text-slate-800">{u.username}</td>
                <td className="px-5 py-2.5 text-slate-700">{u.display_name}</td>
                <td className="px-5 py-2.5 text-slate-500">{u.dept}</td>
                <td className="px-5 py-2.5 text-slate-600">{u.clearance}</td>
                <td className="px-5 py-2.5 text-slate-600">{u.role}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
