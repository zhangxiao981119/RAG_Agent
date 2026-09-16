import { Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { useState } from 'react'

import { AppLayout } from './components/AppLayout'
import { LoginPage } from './pages/LoginPage'
import { ChatPage } from './pages/ChatPage'
import { KnowledgeBasesPage } from './pages/KnowledgeBasesPage'
import { DocumentsPage } from './pages/DocumentsPage'
import { AdminPage } from './pages/AdminPage'
import { User } from './mocks/data'

export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const navigate = useNavigate()

  function handleLogin(u: User) {
    setUser(u)
  }

  function handleLogout() {
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
