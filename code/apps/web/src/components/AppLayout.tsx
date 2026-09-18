import { useEffect, useMemo, useState } from 'react'
import { Button, Layout, Menu } from 'antd'
import {
  AppstoreOutlined,
  BookOutlined,
  FileTextOutlined,
  LogoutOutlined,
  MessageOutlined,
} from '@ant-design/icons'
import type { MenuProps } from 'antd'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'

import { fetchKbs, User } from '../mocks/data'
import { OnboardingModal } from './OnboardingModal'

const { Sider, Content } = Layout

// 三个基础入口：所有登录用户可见
const BASE_MENU_ITEMS: NonNullable<MenuProps['items']> = [
  { key: '/chat', icon: <MessageOutlined />, label: '知识问答' },
  { key: '/knowledge-bases', icon: <BookOutlined />, label: '知识库' },
  { key: '/documents', icon: <FileTextOutlined />, label: '文档' },
]

type Props = {
  currentUser: User | null
  onLogout: () => void
}

export function AppLayout({ currentUser, onLogout }: Props) {
  const navigate = useNavigate()
  const location = useLocation()

  // 首次引导弹窗状态（M5 界面引导优化）
  const [showOnboarding, setShowOnboarding] = useState(false)

  // admin 首次登录且所有 KB 为空时自动弹出引导（localStorage 标记避免重复打扰）
  useEffect(() => {
    if (!currentUser) return
    if (localStorage.getItem('kagent_onboarded')) return
    if ((currentUser.clearance ?? 0) < 40) return

    let cancelled = false
    fetchKbs()
      .then((kbs) => {
        if (cancelled) return
        const allEmpty = kbs.every((kb) => kb.doc_count === 0)
        if (allEmpty) setShowOnboarding(true)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [currentUser?.clearance])

  // 系统管理入口仅 admin（clearance >= 40）可见，避免普通用户误触
  const menuItems = useMemo<MenuProps['items']>(() => {
    const items = [...BASE_MENU_ITEMS]
    if ((currentUser?.clearance ?? 0) >= 40) {
      items.push({ key: '/admin', icon: <AppstoreOutlined />, label: '系统管理' })
    }
    return items
  }, [currentUser?.clearance])

  // 文档子路由 /knowledge-bases/:id/documents 时高亮「知识库」
  const selectedKey = location.pathname.startsWith('/knowledge-bases/')
    ? '/knowledge-bases'
    : location.pathname

  // 引导关闭：无论跳走还是跳过都写标记，下次不再弹
  function handleCloseOnboarding() {
    localStorage.setItem('kagent_onboarded', 'true')
    setShowOnboarding(false)
  }

  // 引导步骤按钮：跳转目标页面（Modal 内部会调用 onClose）
  function handleOnboardingNavigate(path: string) {
    localStorage.setItem('kagent_onboarded', 'true')
    navigate(path)
  }

  return (
    <>
      <Layout style={{ minHeight: '100vh' }}>
        <Sider
        width={224}
        theme="dark"
        breakpoint="lg"
        collapsedWidth={0}
        style={{ position: 'sticky', top: 0, height: '100vh' }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            height: 56,
            margin: '8px 16px',
            color: '#fff',
            fontSize: 16,
            fontWeight: 600,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
          }}
        >
          <span
            style={{
              display: 'inline-flex',
              width: 28,
              height: 28,
              alignItems: 'center',
              justifyContent: 'center',
              borderRadius: 8,
              background: '#1677ff',
              fontSize: 14,
              flexShrink: 0,
            }}
          >
            Q
          </span>
          <span>知识库问答</span>
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
        {currentUser && (
          <div
            style={{
              position: 'absolute',
              bottom: 0,
              left: 0,
              right: 0,
              padding: 16,
              borderTop: '1px solid rgba(255,255,255,0.12)',
            }}
          >
            <div style={{ color: '#fff', fontSize: 14, fontWeight: 500 }}>
              {currentUser.display_name}
            </div>
            <div
              style={{
                color: 'rgba(255,255,255,0.45)',
                fontSize: 12,
                marginTop: 2,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {currentUser.dept_path || '未分配部门'}
            </div>
            <Button
              type="text"
              size="small"
              danger
              icon={<LogoutOutlined />}
              onClick={onLogout}
              style={{ marginTop: 8, paddingLeft: 0 }}
            >
              退出登录
            </Button>
          </div>
        )}
      </Sider>
      <Layout>
        <Content style={{ padding: 24 }}>
          <Outlet />
        </Content>
      </Layout>
      </Layout>
      <OnboardingModal
        open={showOnboarding}
        onClose={handleCloseOnboarding}
        onNavigate={handleOnboardingNavigate}
      />
    </>
  )
}
