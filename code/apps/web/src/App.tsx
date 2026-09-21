import { Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { useEffect, useState } from 'react'

import { AppLayout } from './components/AppLayout'
import { LoginPage } from './pages/LoginPage'
import { ChatPage } from './pages/ChatPage'
import { KnowledgeBasesPage } from './pages/KnowledgeBasesPage'
import { DocumentsPage } from './pages/DocumentsPage'
import { AdminPage } from './pages/AdminPage'
import { getStoredUser, logout, User } from './mocks/data'

export default function App() {
  // 启动时尝试从 sessionStorage 恢复登录态
  const [user, setUser] = useState<User | null>(() => getStoredUser())
  const navigate = useNavigate()

  useEffect(() => {
    // 没有 token 的残留 user 清掉
    if (user && !sessionStorage.getItem('kagent_token')) {
      logout()
      setUser(null)
    }
  }, [user])

  function handleLogin(u: User) {
    setUser(u)
  }

  // await logout：避免浏览器在 logout 请求完成前卸载页面，导致 token 未被吊销
  async function handleLogout() {
    await logout()
    setUser(null)
    navigate('/login')
  }

  if (!user) {
    return <LoginPage onLogin={handleLogin} />
  }

  return (
    <Routes>
      <Route element={<AppLayout currentUser={user} onLogout={handleLogout} />}>
        <Route path="/chat" element={<ChatPage currentUser={user} />} />
        <Route path="/knowledge-bases" element={<KnowledgeBasesPage />} />
        <Route path="/knowledge-bases/:kbId/documents" element={<DocumentsPage />} />
        <Route path="/documents" element={<DocumentsPage />} />
        {/* /admin 路由前端守卫：clearance < 40 重定向到 /chat，避免直接访问 URL 渲染 AdminPage */}
        <Route
          path="/admin"
          element={
            user.clearance >= 40 ? <AdminPage /> : <Navigate to="/chat" replace />
          }
        />
      </Route>
      <Route path="*" element={<Navigate to="/chat" replace />} />
    </Routes>
  )
}
