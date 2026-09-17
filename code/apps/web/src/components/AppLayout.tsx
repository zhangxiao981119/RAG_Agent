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

import { User } from '../mocks/data'

const { Sider, Content } = Layout

const menuItems: MenuProps['items'] = [
  { key: '/chat', icon: <MessageOutlined />, label: '知识问答' },
  { key: '/knowledge-bases', icon: <BookOutlined />, label: '知识库' },
  { key: '/documents', icon: <FileTextOutlined />, label: '文档' },
  { key: '/admin', icon: <AppstoreOutlined />, label: '系统管理' },
]

type Props = {
  currentUser: User | null
  onLogout: () => void
}

export function AppLayout({ currentUser, onLogout }: Props) {
  const navigate = useNavigate()
  const location = useLocation()

  // 文档子路由 /knowledge-bases/:id/documents 时高亮「知识库」
  const selectedKey = location.pathname.startsWith('/knowledge-bases/')
    ? '/knowledge-bases'
    : location.pathname

  return (
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
  )
}
