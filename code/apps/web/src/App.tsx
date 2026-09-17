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
  // 启动时尝试从 localStorage 恢复登录态
  const [user, setUser] = useState<User | null>(() => getStoredUser())
  const navigate = useNavigate()

  useEffect(() => {
    // 没有 token 的残留 user 清掉
    if (user && !localStorage.getItem('kagent_token')) {
      logout()
      setUser(null)
    }
  }, [user])

  function handleLogin(u: User) {
    setUser(u)
  }

  function handleLogout() {
    logout()
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
        <Route path="/admin" element={<AdminPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/chat" replace />} />
    </Routes>
  )
}
